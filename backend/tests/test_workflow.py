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


# 财务接口的最小可用应答：两期报告，公告日都早于本文件统一的 as_of(2026-06-10)。
FUNDAMENTAL_ROWS = [
    {
        "REPORT_DATE": "2026-03-31 00:00:00",
        "REPORT_DATE_NAME": "2026一季报",
        "REPORT_TYPE": "一季报",
        "NOTICE_DATE": "2026-04-17 00:00:00",
        "CURRENCY": "CNY",
        "TOTALOPERATEREVE": 1.2e10,
        "TOTALOPERATEREVETZ": 20.0,
        "XSMLL": 40.0,
        "XSJLL": 22.0,
        "PARENTNETPROFIT": 2.6e9,
        "PARENTNETPROFITTZ": 25.0,
        "KCFJCXSYJLR": 2.5e9,
        "KCFJCXSYJLRTZ": 24.0,
        "ROEJQ": 6.5,
        "ROEKCJQ": 6.2,
        "NETCASH_OPERATE_PK": 3.0e9,
        "TOTAL_ASSETS_PK": 9.0e10,
        "LIABILITY": 4.0e10,
        "TOTAL_EQUITY_PK": 5.0e10,
        "ZCFZL": 44.4,
        "INTEREST_COVERAGE_RATIO": 30.0,
        "EPSJB": 1.1,
        "BPS": 20.0,
    },
    {
        "REPORT_DATE": "2025-12-31 00:00:00",
        "REPORT_DATE_NAME": "2025年报",
        "REPORT_TYPE": "年报",
        "NOTICE_DATE": "2026-03-31 00:00:00",
        "CURRENCY": "CNY",
        "TOTALOPERATEREVE": 4.5e10,
        "TOTALOPERATEREVETZ": 15.0,
        "XSMLL": 39.0,
        "XSJLL": 21.0,
        "PARENTNETPROFIT": 9.4e9,
        "PARENTNETPROFITTZ": 18.0,
        "ROEJQ": 25.0,
        "NETCASH_OPERATE_PK": 1.0e10,
        "TOTAL_ASSETS_PK": 8.6e10,
        "LIABILITY": 3.9e10,
        "ZCFZL": 45.3,
        "INTEREST_COVERAGE_RATIO": 28.0,
    },
]


def role_transport(overrides=None, journal=None):
    """按 system prompt 识别角色并返回符合该角色 schema 的响应。

    overrides: 角色 key → (context, evidence_ids) -> payload
    journal:   若提供，按调用顺序追加角色 key，用于断言调用次数与顺序

    财务数据域与模型调用共用同一个注入点（都是 httpx transport），所以这里也要认得出
    财务接口那条 URL——否则本文件里所有 CN 用例都会因为「取数打到模型 mock」而失败。
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
        if "datacenter-web.eastmoney.com" in request.url.host:
            return httpx.Response(
                200,
                json={"success": True, "result": {"count": len(FUNDAMENTAL_ROWS), "data": FUNDAMENTAL_ROWS}},
            )
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
    # 演示模式且未配置美国新闻源 → 规则规划器停用新闻与宏观
    assert statuses["news"] == "skipped"
    assert statuses["macro"] == "skipped"
    report = result["report"]
    # 仲裁只由确定性背离触发，而演示序列里哪只标的会背离取决于合成本身
    # （MSFT 60 条就是均线下行 + RSI14 18.07 超卖），所以这里断言的是不变量：
    # 要么没背离而跳过，要么裁决措辞与本轮真正命中的那条判据一致。
    assert statuses["arbiter"] in {"skipped", "completed"}
    arbitration = report["arbitration"]
    if statuses["arbiter"] == "completed":
        # 演示新闻没有情绪标签，能命中的只可能是均线/RSI 那条判据；
        # 裁决必须跟着它走，不能套用情绪分歧那句「以价格为主证据」。
        assert "均线排列" in arbitration["conflict"]
        assert "趋势排列" in arbitration["ruling"]
        assert "一手观测" not in arbitration["ruling"]
    else:
        assert arbitration["ruling"] == ""
        assert arbitration["conflict"] == ""
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
    # A 股两个可选角色都无需密钥，规则规划器一律启用；关键是在 use_llm=False 下
    # 它们跑了，却一次模型都没调——确定性取数与模型研判是分开的两件事。
    assert statuses["macro"] == "completed"
    assert result["report"]["planner"] == "规则规划器"
    assert result["report"]["plan"]["enabled_agents"] == ["news", "macro"]


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
    # 模型产出的引用也必须落在报告真正渲染的证据池里，否则前端「点引用跳证据」是死链。
    # 上面那条 claims 断言只覆盖 Python 构造的确定性结论；风险在于模型结论，
    # 而 MockTransport 不校验引用，所以这里直接查 agent_findings。
    report = result["report"]
    pool = {e["id"] for e in report["evidence"]}
    assert report["agent_findings"], "本用例启用了模型，应当留下各角色研判"
    assert all(
        set(claim["evidence_ids"]) <= pool
        for finding in report["agent_findings"].values()
        for claim in finding["findings"]
    )


def test_revision_cap_stops_a_rechallenging_risk(repo, settings):
    """risk@1 再次质询也不会触发第二轮返工。

    回边的唯一终止条件就是这一轮上限，但其它用例里的 risk 在复审轮都主动收回了质询，
    于是把路由守卫整个删掉，测试套件依然全绿。这里让 risk 两轮都提质询，把上限钉住：
    一旦 risk@1 能再次路由回 news@1，图只会靠 GraphRecursionError 停下来，
    表现为任务失败，而不是「返工恰好只有一轮」。
    """
    enable_llm(settings)
    journal = []
    transport = role_transport(
        overrides={
            "risk": lambda context, ids: risk_payload(
                ids, [{"target_agent": "news", "reason": "证据缺口", "request": "重新归纳"}]
            )
        },
        journal=journal,
    )
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=True)
    execute_job(repo, settings, job, CnProvider(), transport=transport)
    result = repo.get(job_id)
    assert result["status"] in {"completed", "partial"}, result["error"]
    # news 只返工一次、risk 只复审一次
    assert journal.count("news") == 2
    assert journal.count("risk") == 2
    assert "news@1@1" not in agent_names(result)
    # 第二轮质询确实被提出来了，但它没有得到任何返工 → 不能记成「已返工」
    challenges = result["report"]["challenges"]
    assert [c["round"] for c in challenges] == [1, 2]
    assert [c["resolved"] for c in challenges] == [True, False]


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


def arbiter_workflow(settings, repo, metrics, items=()):
    workflow = ResearchWorkflow(settings, repo, {"request": ResearchRequest().model_dump(mode="json")})
    return workflow, {"technical": {"metrics": metrics}, "news": {"items": list(items)}}


@pytest.mark.parametrize(
    "trend,rsi,fires",
    [
        ("上行", 70.0, True),
        ("下行", 30.0, True),
        ("上行", 69.99, False),
        ("下行", 30.01, False),
        # 均线本身没排出方向就谈不上「排列与 RSI 背离」
        ("震荡", 95.0, False),
        # SMA50 未成形时 trend 是震荡、RSI 照样算得出来，不能据此判背离
        ("上行", None, False),
    ],
)
def test_momentum_divergence_needs_a_trend_and_an_extreme_rsi(settings, repo, trend, rsi, fires):
    workflow, state = arbiter_workflow(settings, repo, {"trend": trend, "rsi14": rsi})
    assert [f["kind"] for f in workflow.conflict_findings(state)] == (["momentum"] if fires else [])


def test_momentum_divergence_fires_without_news_or_a_model(settings, repo):
    """A 股与演示模式的新闻没有情绪标签，仲裁只能靠这条价内判据才跑得起来。"""
    workflow, state = arbiter_workflow(settings, repo, {"trend": "下行", "rsi14": 12.0}, items=[])
    assert workflow.detected_conflicts(state), "无新闻时也必须能识别背离"
    assert workflow.node_arbiter(state, "arbiter")["status"] == "completed"


def test_arbiter_ruling_wording_follows_the_conflict_kinds(settings, repo):
    momentum, state = arbiter_workflow(settings, repo, {"trend": "上行", "rsi14": 78.0})
    ruling = momentum.node_arbiter(state, "arbiter")["ruling"]
    assert "趋势排列" in ruling
    # 均线/RSI 两边都源自同一段价格，「价格是一手观测」那句在这里不成立
    assert "一手观测" not in ruling

    both, mixed = arbiter_workflow(
        settings, repo, {"trend": "下行", "rsi14": 12.0}, items=[{"sentiment": "Bullish"}]
    )
    assert {f["kind"] for f in both.conflict_findings(mixed)} == {"sentiment", "momentum"}
    out = both.node_arbiter(mixed, "arbiter")
    assert "供应商情绪标签仅作背景参考" in out["ruling"] and "趋势排列" in out["ruling"]
    # 两类冲突各自的理由都要出现在同一条裁决里，而不是只留最后算出来的那一类
    assert "直接观测" in out["rationale"] and "均值回归" in out["rationale"]


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


def test_cn_run_carries_fundamentals_without_spending_a_role(repo, settings):
    """财务数据域取到了：进覆盖率、进证据、进证据池，但不占八个角色里的任何一个。"""
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live")
    execute_job(repo, settings, job, CnProvider(), transport=role_transport())
    result = repo.get(job_id)
    report = result["report"]
    assert report["coverage"]["fundamentals"] is True
    fundamental = [e for e in report["evidence"] if e["kind"] == "fundamental"]
    assert len(fundamental) == 1
    assert "2026一季报" in fundamental[0]["title"]
    # 财务数据域不是角色：八个角色的花名册原样不动，它既不在启用/停用清单里，
    # 也不进 agent_findings——它只做确定性取数，不调用模型。
    names = {a["name"] for a in result["agents"]}
    assert set(AGENT_KEYS) <= names
    assert "fundamentals" not in AGENT_KEYS
    assert "fundamentals" not in report["plan"]["enabled_agents"]
    assert "fundamentals" not in report["plan"]["skipped_reason"]
    assert all(key in ROLE_MARKERS for key in report["agent_findings"])


def test_us_run_declares_fundamentals_unavailable_without_failing_the_report(repo, settings):
    """美股取不到财报是「按设计不提供」，不该把每份美股报告都拖成 partial。"""
    job_id, job = create_claim(repo, market="US", symbol="AAPL")
    execute_job(repo, settings, job)
    result = repo.get(job_id)
    assert result["status"] == "completed"
    report = result["report"]
    assert report["coverage"]["fundamentals"] is False
    assert any("财务数据未启用" in x for x in report["limitations"])
    assert not [e for e in report["evidence"] if e["kind"] == "fundamental"]


def test_fundamentals_failure_degrades_to_partial_not_fatal(repo, settings):
    def respond(req):
        if "datacenter-web.eastmoney.com" in req.url.host:
            return httpx.Response(200, json={"success": True, "result": {"count": 0, "data": []}})
        raise AssertionError("本用例不该调用模型")

    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live")
    execute_job(repo, settings, job, CnProvider(), transport=httpx.MockTransport(respond))
    result = repo.get(job_id)
    assert result["status"] == "partial"
    report = result["report"]
    assert report["coverage"]["fundamentals"] is False
    assert any("无可用报告期记录" in x for x in report["limitations"])
    # 行情与新闻照常出报告：一个数据域掉线不该让整轮研究失败
    assert report["news"]


@pytest.mark.parametrize(
    "question,matched",
    [
        ("需要补充浮动利率与固定利率债务的拆分", True),
        ("缺少分析师一致预期，无法判断预期差", True),
        ("无法量化折现率变动到目标价的传导", True),
        ("需要 2026 年逐日成交量明细以核对量价配合", False),
        ("缺少分部收入与毛利率拆解", False),
    ],
)
def test_design_limit_matching_does_not_swallow_real_questions(question, matched):
    from financial_research.workflow import design_limit_of

    assert bool(design_limit_of(question)) is matched


def test_design_limits_are_rendered_apart_from_open_questions(repo, settings):
    """设计边界要从「待解问题」里分出去，但真实问题必须原样留下。"""
    enable_llm(settings)
    real = "缺少 2026 年逐日成交量明细以核对量价配合"
    boundary = "需要补充浮动利率与固定利率债务的拆分"
    transport = role_transport(
        overrides={
            "technical": lambda context, ids: {
                **finding_payload(ids),
                "open_questions": [real, boundary],
            }
        }
    )
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=True)
    execute_job(repo, settings, job, CnProvider(), transport=transport)
    markdown = repo.get(job_id)["report"]["markdown"]
    assert f"- 待解问题：{real}" in markdown
    assert boundary not in markdown.split("## 设计边界（非数据缺口）")[0]
    assert "## 设计边界（非数据缺口）" in markdown
    assert "浮动/固定利率债务拆分" in markdown
    # 未经匹配的原始文本仍留在 JSON 报告里，多角色可观测性不受渲染分流影响
    findings = repo.get(job_id)["report"]["agent_findings"]
    assert findings["technical"]["open_questions"] == [real, boundary]


def test_user_facing_prose_carries_readable_citations_not_evidence_ids(repo, settings):
    """market-3f2a… 这种标识只有程序认得，用户既读不懂也回指不到东西。

    正文与引用一律换算成「来源 NN」，编号即「证据来源」小节的序号；原始 ID 只留在
    「证据来源」小节与结构化字段里（引用校验靠后者，不能被渲染改写）。
    """
    enable_llm(settings)

    def with_ids(context, ids):
        eid = ids[0]
        return {
            "summary": f"覆盖范围见（{eid}），行情口径需结合证据理解。",
            "claims": [{"text": f"价格与日期已完成校验（{eid}）。", "kind": "fact", "evidence_ids": [eid]}],
            "uncertainties": [f"极端波动段未经第二源校准（{eid}）"],
        }

    transport = role_transport(overrides={"report": with_ids})
    job_id, job = create_claim(repo, market="CN", symbol="600519", mode="live", use_llm=True)
    execute_job(repo, settings, job, CnProvider(), transport=transport)
    report = repo.get(job_id)["report"]
    raw = report["ai_synthesis"]["claims"][0]["evidence_ids"][0]
    number = f"{[ev['id'] for ev in report['evidence']].index(raw) + 1:02d}"
    body, sources = report["markdown"].split("## 证据来源")
    assert raw not in body
    assert f"覆盖范围见（来源 {number}）" in body
    assert f"价格与日期已完成校验（来源 {number}）" in body
    assert f"极端波动段未经第二源校准（来源 {number}）" in body
    # 「证据来源」小节保留原始 ID，编号才对得上正文里的「来源 NN」
    assert f"- {number} · {raw} · " in sources
    # 机器层不受渲染影响：结构化引用仍是原始 ID。
    assert report["ai_synthesis"]["claims"][0]["evidence_ids"] == [raw]
