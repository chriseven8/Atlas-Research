import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import update

from financial_research.agents import AGENT_KEYS
from financial_research.domain import LeaseLost, ProviderError, ResearchRequest
from financial_research.providers import DemoProvider, evidence
from financial_research.storage import Repository, jobs
from financial_research.worker import execute_job
from financial_research.workflow import ResearchWorkflow, challenged_targets, revisions_done

# system prompt 里的角色名，用来把 MockTransport 的响应分派给正确的 agent。
ROLE_MARKERS = {
    "manager": "你是金融研究团队的研究经理",
    "market": "你是市场数据专员",
    "technical": "你是技术分析师",
    "news": "你是财经事件分析师",
    "macro": "你是宏观分析师",
    "risk": "你是风控审查官",
    "arbiter": "你是首席研究仲裁",
    "report": "你是研究报告编辑",
}


def create_claim(repo, **kwargs):
    req = ResearchRequest(as_of="2026-06-10", **kwargs)
    job_id, _ = repo.create(req)
    return job_id, repo.claim()


def enable_llm(settings):
    settings.openai_api_key = "test-key"
    settings.openai_model = "test-model"
    return settings


def agent_statuses(result):
    return {a["name"]: a["status"] for a in result["agents"]}


def agent_names(result):
    return [a["name"] for a in result["agents"]]


def response_with(payload):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}],
        "usage": {"input_tokens": 100, "output_tokens": 40},
    }


def finding_payload(eids):
    return {
        "headline": "样本期内呈上行特征但样本有限",
        "findings": [
            {"text": "趋势由程序计算，不代表未来收益。", "kind": "interpretation", "evidence_ids": eids}
        ],
        "confidence": "medium",
        "open_questions": ["缺少成交量背景"],
    }


def risk_payload(eids, challenges):
    return {**finding_payload(eids), "challenges": challenges}


def arbiter_payload(eids):
    return {
        "conflict": "方向不一致",
        "ruling": "以价格证据为主",
        "rationale": "价格是一手观测，情绪标签是二手聚合",
        "evidence_ids": eids,
    }


def role_transport(overrides=None, journal=None):
    """按 system prompt 识别角色并返回符合该角色 schema 的响应。

    overrides: 角色 key → (context, evidence_ids) -> payload
    journal:   若提供，按调用顺序追加角色 key，用于断言调用次数与顺序
    """
    overrides = overrides or {}

    def default_payload(role, context, ids):
        if role == "manager":
            return {
                "enabled_agents": ["news"],
                "rationale": "聚焦价格与事件",
                "focus": [{"agent": "news", "note": "核对公告口径"}],
                "skipped_reason": [{"agent": "macro", "note": "本轮问题不涉及利率"}],
            }
        if role == "risk":
            return risk_payload(ids, [])
        if role == "arbiter":
            return arbiter_payload(ids)
        if role == "report":
            return {
                "summary": "样本期内呈上行特征，但证据覆盖有限。",
                "claims": [{"text": "价格与日期已完成校验。", "kind": "fact", "evidence_ids": ids[:1]}],
                "uncertainties": ["未包含财报与估值"],
            }
        return finding_payload(ids)

    def responder(request):
        body = json.loads(request.content)
        system = body["input"][0]["content"]
        for role, marker in ROLE_MARKERS.items():
            if marker not in system:
                continue
            if journal is not None:
                journal.append(role)
            context = json.loads(body["input"][1]["content"])
            ids = [item["id"] for item in context.get("evidence") or []]
            build = overrides.get(role)
            payload = build(context, ids) if build else default_payload(role, context, ids)
            return httpx.Response(200, json=response_with(payload))
        raise AssertionError(f"未预期的模型调用：{system[:120]}")

    return httpx.MockTransport(responder)


class CnProvider(DemoProvider):
    """给 CN 用例提供一份确定性的 A 股行情。

    DemoProvider 只覆盖 AAPL/MSFT/NVDA/SPY，因为演示模式本身被
    ResearchRequest.validate_scope 限定为「美股 + 这四个代码」，而 provider_for 也只在
    mode == "demo" 时返回 DemoProvider——放宽它只会得到生产中不可达的死代码。
    这里复用同一套合成曲线，只把币种、时区与证据标题换成 A 股的。
    """

    def market(self, req):
        data = super().market(req.model_copy(update={"symbol": "AAPL"}))
        return {
            **data,
            "currency": "CNY",
            "timezone": "Asia/Shanghai",
            "evidence": [{**ev, "title": f"{req.symbol} 合成日线数据"} for ev in data["evidence"]],
        }


class ReversalProvider(CnProvider):
    """价格单调下行 + 新闻情绪一致看多：用于强制触发确定性冲突检测。"""

    def market(self, req):
        output = super().market(req)
        bars = output["bars"]
        for index, bar in enumerate(bars):
            close = round(200 - index * 0.5, 2)
            bar.update(
                {
                    "close": close,
                    "open": round(close * 1.001, 2),
                    "high": round(close * 1.002, 2),
                    "low": round(close * 0.998, 2),
                }
            )
        output["evidence"] = [
            evidence(
                "market",
                f"{req.symbol} 合成日线数据",
                "Atlas 演示生成器 v1",
                bars[-1]["date"],
                bars,
                True,
                note="测试用：单调下行的合成序列。",
            )
        ]
        return output

    def news(self, req):
        output = super().news(req)
        for item in output["items"]:
            item["sentiment"] = "Bullish"
        output["evidence"] = [
            evidence("news", item["title"], item["source"], item["published_at"], item, True)
            for item in output["items"]
        ]
        for item, ev in zip(output["items"], output["evidence"], strict=True):
            item["evidence_id"] = ev["id"]
        return output


@pytest.mark.parametrize("symbol,lookback", [("AAPL", 90), ("MSFT", 60), ("NVDA", 30), ("SPY", 100)])
def test_complete_pipeline_and_evidence_integrity(repo, settings, symbol, lookback):
    job_id, job = create_claim(repo, symbol=symbol, lookback_days=lookback)
    execute_job(repo, settings, job)
    result = repo.get(job_id)
    assert result["status"] == "completed", result["error"]
    statuses = agent_statuses(result)
    # 八个角色节点始终存在；被计划排除的角色以 skipped 出现在结果里，而不是消失
    assert set(AGENT_KEYS) <= set(statuses)
    for key in ("manager", "market", "technical", "risk", "report"):
        assert statuses[key] == "completed"
    # 演示模式且未配置美国新闻源 → 规则规划器停用新闻与宏观；无方向冲突 → 不启用仲裁
    assert statuses["news"] == "skipped"
    assert statuses["macro"] == "skipped"
    assert statuses["arbiter"] == "skipped"
    report = result["report"]
    assert len(report["chart"]) == lookback
    assert report["mode"] == "demo" and "合成" in report["summary"]
    assert all(e["is_demo"] for e in report["evidence"])
    evidence_ids = {e["id"] for e in report["evidence"]}
    assert all(set(c["evidence_ids"]) <= evidence_ids for c in report["claims"])
    assert "SHA256" in report["markdown"]
    assert report["ai_synthesis"] is None
    assert report["planner"] == "规则规划器"
    assert report["plan"]["enabled_agents"] == []
    assert "## 研究计划" in report["markdown"]
    assert report["challenges"] == []


def test_optional_provider_failure_produces_partial_report(repo, settings):
    class PartialProvider(CnProvider):
        def news(self, req):
            raise ProviderError("新闻额度不足")

    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live")
    execute_job(repo, settings, job, PartialProvider())
    result = repo.get(job_id)
    assert result["status"] == "partial"
    assert result["report"]["news"] == []
    assert "新闻额度不足" in result["report"]["limitations"]


def test_plan_disabled_run_makes_no_model_calls(repo, settings):
    enable_llm(settings)
    journal = []
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=False)
    execute_job(repo, settings, job, CnProvider(), transport=role_transport(journal=journal))
    result = repo.get(job_id)
    assert journal == []
    assert result["llm_calls"] == 0
    statuses = agent_statuses(result)
    assert statuses["news"] == "completed"
    assert statuses["macro"] == "skipped"
    assert result["report"]["planner"] == "规则规划器"
    assert result["report"]["plan"]["enabled_agents"] == ["news"]


def test_model_plan_skips_unlisted_agent_and_records_reason(repo, settings):
    enable_llm(settings)
    journal = []
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=True)
    execute_job(repo, settings, job, CnProvider(), transport=role_transport(journal=journal))
    result = repo.get(job_id)
    assert "manager" in journal
    statuses = agent_statuses(result)
    assert statuses["news"] == "completed"
    assert statuses["macro"] == "skipped"
    assert any(
        a["name"] == "macro" and "利率" in (a["output"] or {}).get("reason", "") for a in result["agents"]
    )
    assert result["report"]["planner"] == "模型规划"


def test_challenge_triggers_rework_and_a_second_risk_round(repo, settings):
    enable_llm(settings)
    journal = []
    transport = role_transport(
        overrides={
            "risk": lambda context, ids: risk_payload(
                ids,
                []
                if context.get("is_revision_round")
                else [
                    {
                        "target_agent": "news",
                        "reason": "新闻证据只是检索片段，未核验发布时间",
                        "request": "核对发布时间与来源后重新归纳",
                    }
                ],
            )
        },
        journal=journal,
    )
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=True)
    execute_job(repo, settings, job, CnProvider(), transport=transport)
    result = repo.get(job_id)
    statuses = agent_statuses(result)
    assert statuses["news"] == "completed"
    assert statuses["news@1"] == "completed"
    assert statuses["risk@1"] == "completed"
    # 被质询的角色真的重跑了一次，风控也真的复审了一次
    assert journal.count("news") == 2
    assert journal.count("risk") == 2
    challenges = result["report"]["challenges"]
    assert [c["round"] for c in challenges] == [1]
    assert challenges[0]["target_agent"] == "news"
    assert challenges[0]["resolved"] is True
    assert "## 质询与返工" in result["report"]["markdown"]
    # 复审后风控收回了质询 → 不再有第二轮返工
    assert "news@1@1" not in agent_names(result)


def test_rerun_reuses_plan_and_does_not_reenter_the_revision_loop(repo, settings):
    enable_llm(settings)
    journal = []
    transport = role_transport(
        overrides={
            "risk": lambda context, ids: risk_payload(
                ids,
                []
                if context.get("is_revision_round")
                else [{"target_agent": "news", "reason": "证据缺口", "request": "重新归纳"}],
            )
        },
        journal=journal,
    )
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=True)
    workflow = ResearchWorkflow(settings, repo, job, provider=CnProvider(), transport=transport)
    first = workflow.run()
    names_after_first = agent_names(repo.get(job_id))
    calls_after_first = len(repo.get(job_id)["model_calls"])
    assert names_after_first.count("news@1") == 1

    # 第二次 run 模拟中断恢复：计划复用 manager 缓存，全图命中节点缓存，不重复计费
    second = workflow.run()
    assert agent_names(repo.get(job_id)) == names_after_first
    assert len(repo.get(job_id)["model_calls"]) == calls_after_first
    assert second["report"]["plan"] == first["report"]["plan"]
    assert second["report"]["challenges"] == first["report"]["challenges"]


def test_conflict_triggers_arbitration(repo, settings):
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live")
    execute_job(repo, settings, job, ReversalProvider())
    result = repo.get(job_id)
    report = result["report"]
    assert report["conflicts"], "单调下行 + 看多情绪必须被确定性规则识别为冲突"
    assert agent_statuses(result)["arbiter"] == "completed"
    assert report["arbitration"]["ruling"]
    assert report["arbitration"]["conflict"]
    assert "## 仲裁结论" in report["markdown"]


def test_conflict_detection_requires_opposite_directions(repo, settings):
    workflow = ResearchWorkflow(settings, repo, {"request": ResearchRequest().model_dump(mode="json")})
    down_bull = {"technical": {"metrics": {"trend": "下行"}}, "news": {"items": [{"sentiment": "Bullish"}]}}
    up_bear = {"technical": {"metrics": {"trend": "上行"}}, "news": {"items": [{"sentiment": "Bearish"}]}}
    aligned = {"technical": {"metrics": {"trend": "上行"}}, "news": {"items": [{"sentiment": "Bullish"}]}}
    assert workflow.detected_conflicts(down_bull)
    assert workflow.detected_conflicts(up_bear)
    assert workflow.detected_conflicts(aligned) == []
    assert (
        workflow.detected_conflicts({"technical": {"metrics": {"trend": "震荡"}}, "news": {"items": []}})
        == []
    )


def test_revision_round_cap_is_expressed_by_node_names():
    assert revisions_done({}) is False
    assert revisions_done({"news@1": {}}) is True
    # 只有 technical / news / macro 可以返工；质询 market / risk / report 一律被丢弃
    assert challenged_targets(
        {
            "challenges": [
                {"target_agent": "news"},
                {"target_agent": "news"},
                {"target_agent": "market"},
                {"target_agent": "risk"},
                {"target_agent": "report"},
                {"target_agent": "technical"},
            ]
        }
    ) == ["news", "technical"]


def test_budget_exhaustion_degrades_without_failing_the_job(repo, settings):
    enable_llm(settings)
    settings.max_llm_calls = 3
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=True)
    execute_job(repo, settings, job, CnProvider(), transport=role_transport())
    result = repo.get(job_id)
    assert result["status"] in {"completed", "partial"}
    assert result["llm_calls"] == 3
    assert result["report"]["summary"]
    assert any("预算" in x for x in result["report"]["limitations"])


def test_truncation_is_reported_as_budget_not_as_request_failure(repo, settings):
    enable_llm(settings)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [],
            },
        )
    )
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=True)
    execute_job(repo, settings, job, CnProvider(), transport=transport)
    result = repo.get(job_id)
    assert result["status"] == "partial"
    limitations = result["report"]["limitations"]
    assert any("输出预算不足" in x for x in limitations)
    assert not any("模型请求失败" in x for x in limitations)


def test_market_failure_is_fatal(repo, settings):
    class BrokenProvider(DemoProvider):
        def market(self, req):
            raise ProviderError("行情不可用")

    job_id, job = create_claim(repo)
    execute_job(repo, settings, job, BrokenProvider())
    result = repo.get(job_id)
    assert result["status"] == "failed"
    assert result["report"] is None
    assert result["error"] == "行情不可用"


def test_expired_lease_resumes_saved_node_without_repeating(repo, settings):
    job_id, old = create_claim(repo)
    workflow = ResearchWorkflow(settings, repo, old)
    workflow.wrap("manager")({})
    workflow.wrap("market")({})
    with repo.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(lease_until=time.time() - 10))
    fresh_repo = Repository(settings)
    new = fresh_repo.claim()
    assert new["owner"] != old["owner"]
    with pytest.raises(LeaseLost):
        repo.finish(job_id, old["owner"], "completed")

    class CachedMarketProvider(DemoProvider):
        def market(self, req):
            raise AssertionError("A completed market node must not run twice")

    execute_job(fresh_repo, settings, new, CachedMarketProvider())
    result = fresh_repo.get(job_id)
    assert result["status"] == "completed", result["error"]
    assert result["attempts"] == 2
    assert (
        len([e for e in result["events"] if e["kind"] == "agent_started" and e["message"] == "market"]) == 1
    )
    fresh_repo.close()


def test_concurrent_workers_only_claim_once(repo):
    job_id, _ = repo.create(ResearchRequest())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: repo.claim(), range(4)))
    assert len([x for x in results if x]) == 1
    assert next(x for x in results if x)["id"] == job_id


def test_cancel_fences_inflight_results(repo):
    job_id, job = create_claim(repo)
    repo.start_node(job_id, job["owner"], "manager")
    assert repo.cancel(job_id)
    assert not repo.cancel(job_id)
    with pytest.raises(LeaseLost):
        repo.finish_node(job_id, job["owner"], "manager", {"status": "completed"}, 1)
    assert repo.get(job_id)["status"] == "cancelled"


def test_timeout_does_not_continue_work(repo, settings):
    job_id, job = create_claim(repo)
    with repo.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(started_at=time.time() - 10000))
    execute_job(repo, settings, job)
    assert repo.get(job_id)["status"] == "failed"
    assert "时间预算" in repo.get(job_id)["error"]


def test_model_call_budget_is_durable(repo, settings):
    settings.max_llm_calls = 1
    job_id, job = create_claim(repo)
    assert repo.reserve_call(job_id, job["owner"])
    assert repo.reserve_call(job_id, job["owner"]) is None
    assert repo.get(job_id)["llm_calls"] == 1


def test_recovery_attempt_limit(repo, settings):
    job_id, job = create_claim(repo)
    with repo.engine.begin() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(attempts=settings.max_task_attempts, lease_until=time.time() - 10)
        )
    assert repo.claim() is None
    assert repo.get(job_id)["status"] == "failed"
