import time
from itertools import pairwise
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import AGENT_NAMES, AGENT_SPECS, PLANNABLE_AGENTS
from .analytics import analyze
from .domain import LeaseLost, ProviderError, ResearchRequest, utcnow
from .llm import call_agent
from .planner import normalize_plan, rule_plan
from .providers import provider_for
from .settings import Settings
from .storage import Repository

REVISION_SUFFIX = "@1"
# 只有这三个角色可以返工：market 是取数、risk 是审查方、report 是汇总方，返工它们没有语义。
REVISABLE = ("technical", "news", "macro")
FINDING_AGENTS = ("market", "technical", "news", "macro")

# 节点名带 @1 后缀。后缀同时是 (job_id, name) 缓存键的一部分，
# 因此复审不会被 start_node 命中缓存而跳过，中断恢复后的轮次判定也依然正确。
REVISION_NODES = tuple(f"{key}{REVISION_SUFFIX}" for key in REVISABLE)
RISK_REVISION = f"risk{REVISION_SUFFIX}"

State = TypedDict(
    "State",
    {
        "request": dict,
        "manager": dict,
        "market": dict,
        "technical": dict,
        "news": dict,
        "macro": dict,
        "risk": dict,
        "arbiter": dict,
        "report": dict,
        "technical@1": dict,
        "news@1": dict,
        "macro@1": dict,
        "risk@1": dict,
    },
    total=False,
)

# 被计划跳过的节点仍要写出一份结构完整的输出。下游读者目前都用 `or []` / `or {}` 兜底，
# 所以这里少写几个键不会立刻变形；保留完整形状是为了让「跳过」与其它状态的输出同构，
# 前端与 REST 消费者不必为跳过分支单独做存在性判断。
SKIPPED_DEFAULTS = {
    "items": [],
    "claims": [],
    "metrics": None,
    "risks": [],
    "conflicts": [],
    "coverage": {},
    "challenges": [],
    "evidence": [],
    "warnings": [],
}


def public_error(exc: Exception) -> str:
    if isinstance(exc, (ProviderError, TimeoutError, LeaseLost)):
        return str(exc)
    return f"研究节点执行失败（{type(exc).__name__}），请查看测试与服务日志。"


def revisions_done(state: State) -> bool:
    """复审轮由节点名后缀表达，而不是 state 计数器。

    后缀是缓存键的一部分，state 由缓存重建，所以中断恢复后该判定依然正确。
    若改用 state 计数器，恢复时它会归零，risk 会读到带同一条质询的旧输出并反复触发回边，
    形成死循环。
    """
    return any(key.endswith(REVISION_SUFFIX) for key in state)


def challenged_targets(output: dict) -> list[str]:
    """取出 risk 输出中可返工的目标角色，按白名单保序去重。"""
    targets: list[str] = []
    for challenge in output.get("challenges") or []:
        target = (challenge or {}).get("target_agent")
        if target in REVISABLE and target not in targets:
            targets.append(target)
    return targets


def degrade(output: dict, reason: str) -> dict:
    """把一次模型失败降级为确定性输出：状态转 partial，原因写进 warnings。"""
    if output.get("status", "partial") == "completed":
        output["status"] = "partial"
    output.setdefault("status", "partial")
    output["warnings"] = [*output.get("warnings", []), reason]
    return output


class ResearchWorkflow:
    def __init__(self, settings: Settings, repo: Repository, job: dict, provider=None, transport=None):
        self.settings, self.repo, self.job = settings, repo, job
        self.transport = transport
        self.req = ResearchRequest.model_validate(job["request"])
        self.provider = provider or provider_for(self.req, settings)

    # ---------- 图与缓存包装 ----------

    def handler(self, name: str):
        """@1 节点到实现方法的映射。

        technical/news/macro@1 走通用的返工实现；risk@1 走 node_risk 本体——
        它要基于返工后的结论重新审查（risk_context 自带 is_revision_round 语义），
        而不是复用 node_revision：后者会原样抄下 risk 上一轮的 challenges，
        让 challenge_records 多出一条同轮次记录，且复审根本不会调用模型。
        """
        if name == RISK_REVISION:
            return self.node_risk
        if name.endswith(REVISION_SUFFIX):
            return self.node_revision
        return getattr(self, f"node_{name}")

    def wrap(self, name: str):
        def node(state: State) -> dict:
            self.repo.check_active(self.job["id"], self.job["owner"])
            cached = self.repo.start_node(self.job["id"], self.job["owner"], name)
            if cached is not None:
                return {name: cached}
            started = time.monotonic()
            try:
                output = self.handler(name)(state, name)
                self.repo.check_active(self.job["id"], self.job["owner"])
                self.repo.finish_node(
                    self.job["id"], self.job["owner"], name, output, int((time.monotonic() - started) * 1000)
                )
                return {name: output}
            except LeaseLost:
                raise
            except Exception as exc:
                self.repo.fail_node(self.job["id"], self.job["owner"], name, public_error(exc))
                raise

        return node

    def router_for(self, node_name: str):
        """risk 与 risk@1 共用的条件路由：先返工，再仲裁。"""

        def route(state: State) -> str | list[str]:
            if node_name.endswith(REVISION_SUFFIX) or revisions_done(state):
                return "arbiter"
            targets = challenged_targets(state.get(node_name) or {})
            if not targets:
                return "arbiter"
            return [f"{target}{REVISION_SUFFIX}" for target in targets]

        return route

    def run(self) -> State:
        graph = StateGraph(State)
        for name in ("manager", "market", "technical", "news", "macro", "risk", "arbiter", "report"):
            graph.add_node(name, self.wrap(name))
        for name in REVISION_NODES:
            graph.add_node(name, self.wrap(name))
        graph.add_node(RISK_REVISION, self.wrap(RISK_REVISION))

        graph.add_edge(START, "manager")
        # technical 只由 market 触发：它要读 state["market"]。若这里再补一条 manager→technical，
        # langgraph 会在 market 完成前就并行执行 technical，读不到行情。
        for name in ("market", "news", "macro"):
            graph.add_edge("manager", name)
        graph.add_edge("market", "technical")
        # 汇合屏障：三个分析角色全部触发后 risk 才执行。被计划跳过的角色也会走到这里（no-op），
        # 所以屏障不会死锁——这正是选择「节点常驻 + 计划驱动 no-op」而不是动态删边的原因。
        graph.add_edge(["technical", "news", "macro"], "risk")
        destinations = ["arbiter", *REVISION_NODES]
        for source in ("risk", RISK_REVISION):
            graph.add_conditional_edges(source, self.router_for(source), destinations)
        for name in REVISION_NODES:
            graph.add_edge(name, RISK_REVISION)
        graph.add_edge("arbiter", "report")
        graph.add_edge("report", END)
        return graph.compile().invoke(
            {"request": self.req.model_dump(mode="json")},
            {"recursion_limit": 25, "max_concurrency": 3},
        )

    # ---------- 计划与降级 ----------

    def plan_of(self, state: State) -> dict:
        return (state.get("manager") or {}).get("plan") or {}

    def enabled(self, state: State, key: str) -> bool:
        if not AGENT_SPECS[key].optional:
            return True
        enabled_agents = self.plan_of(state).get("enabled_agents")
        if enabled_agents is None:
            return True  # 计划缺失时保守启用，避免规划降级连带丢掉角色
        return key in enabled_agents

    def skipped_output(self, key: str, state: State) -> dict:
        reason = (self.plan_of(state).get("skipped_reason") or {}).get(key) or "研究计划未启用该角色。"
        return {
            **SKIPPED_DEFAULTS,
            "status": "skipped",
            "agent": key,
            "summary": f"{AGENT_SPECS[key].role}本轮未启用。",
            "reason": reason,
        }

    def run_agent(self, key: str, context: dict, deterministic: dict) -> dict:
        """在预算内调用模型；任何失败都降级为确定性输出并把原因写进 warnings。

        单个 agent 的模型失败不得让整个研究失败——这是本系统的降级原则。
        """
        output = dict(deterministic)
        if not self.req.use_llm:
            return output
        call_id = self.repo.reserve_call(self.job["id"], self.job["owner"])
        if call_id is None:
            return degrade(output, "模型调用预算已用完，该角色改用确定性结论。")
        try:
            finding, usage = call_agent(self.settings, AGENT_SPECS[key], context, self.transport)
            self.repo.finish_call(call_id, usage=usage)
        except ProviderError as exc:
            self.repo.finish_call(call_id, error=str(exc))
            return degrade(output, str(exc))
        output["finding"] = finding
        return output

    # ---------- 各角色的输入构造 ----------

    def planning_context(self) -> dict:
        return {
            "question": self.req.question,
            "mode": self.req.mode,
            "market": self.req.market,
            "symbol": self.req.symbol,
            "as_of": str(self.req.as_of),
            "lookback_days": self.req.lookback_days,
            "plannable_agents": list(PLANNABLE_AGENTS),
            "available_sources": {
                "market": "沪深京与美股已收盘日线（无需密钥）",
                "news_cn": "东方财富个股新闻与公司公告（无需密钥）",
                "news_us": "Alpha Vantage 新闻情绪"
                if self.settings.alpha_vantage_api_key
                else "未配置，美国新闻不可用",
                "macro_cn": "未接入，中国宏观不可用",
                "macro_us": "Alpha Vantage / FRED 联邦基金利率"
                if self.settings.alpha_vantage_api_key
                else "未配置，美国宏观不可用",
            },
        }

    def market_context(self, output: dict) -> dict:
        bars = output.get("bars") or []
        moves = [abs(b["close"] / a["close"] - 1) for a, b in pairwise(bars)]
        return {
            "question": self.req.question,
            "symbol": self.req.symbol,
            "market": self.req.market,
            "adjustment": output.get("adjustment"),
            "currency": output.get("currency"),
            "bars_summary": {
                "count": len(bars),
                "first_date": bars[0]["date"] if bars else None,
                "last_date": bars[-1]["date"] if bars else None,
                "last_close": bars[-1]["close"] if bars else None,
                "max_close": max((b["close"] for b in bars), default=None),
                "min_close": min((b["close"] for b in bars), default=None),
                "largest_daily_move_pct": round(max(moves, default=0.0) * 100, 2),
            },
            "warnings": output.get("warnings", []),
            "evidence": output.get("evidence", []),
        }

    def technical_context(self, output: dict) -> dict:
        return {
            "question": self.req.question,
            "symbol": self.req.symbol,
            "metrics": output.get("metrics"),
            "warnings": output.get("warnings", []),
            "evidence": output.get("evidence", []),
        }

    def news_context(self, output: dict) -> dict:
        return {
            "question": self.req.question,
            "symbol": self.req.symbol,
            "market": self.req.market,
            "as_of": str(self.req.as_of),
            "items": [
                {
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                    "source": item.get("source", ""),
                    "published_at": item.get("published_at", ""),
                    "sentiment": item.get("sentiment"),
                    "evidence_id": item.get("evidence_id"),
                }
                for item in (output.get("items") or [])[:12]
            ],
            "warnings": output.get("warnings", []),
            "evidence": output.get("evidence", []),
        }

    def macro_context(self, output: dict) -> dict:
        return {
            "question": self.req.question,
            "symbol": self.req.symbol,
            "market": self.req.market,
            "items": (output.get("items") or [])[:12],
            "warnings": output.get("warnings", []),
            "evidence": output.get("evidence", []),
        }

    def evidence_pool(self, state: State) -> list[dict]:
        """汇总本轮所有可用证据，作为引用校验的白名单。"""
        pool: dict[str, dict] = {}
        for key in (*FINDING_AGENTS, *REVISION_NODES):
            for item in (state.get(key) or {}).get("evidence") or []:
                pool[item["id"]] = item
        return list(pool.values())

    def agent_digest(self, state: State, key: str) -> dict:
        output = state.get(key) or {}
        finding = output.get("finding") or {}
        return {
            "role": AGENT_SPECS[key[: -len(REVISION_SUFFIX)] if key.endswith(REVISION_SUFFIX) else key].role,
            "status": output.get("status"),
            "summary": output.get("summary", ""),
            "headline": finding.get("headline"),
            "confidence": finding.get("confidence"),
            "claims": [c.get("text", "") for c in finding.get("findings", [])],
            "open_questions": finding.get("open_questions", []),
            "reviewed_challenges": output.get("reviewed_challenges", []),
            "warnings": output.get("warnings", []),
        }

    def detected_conflicts(self, state: State) -> list[str]:
        """确定性冲突检测：供应商情绪标签与价格趋势方向相反。

        它不依赖模型，因此 use_llm=False 时 arbiter 依然能被触发。
        """
        metrics = (state.get("technical") or {}).get("metrics") or {}
        trend = metrics.get("trend")
        labels = [
            str(item.get("sentiment", "")).lower() for item in (state.get("news") or {}).get("items") or []
        ]
        if (trend == "下行" and any("bullish" in x for x in labels)) or (
            trend == "上行" and any("bearish" in x for x in labels)
        ):
            return ["供应商新闻情绪与价格趋势存在方向分歧；事件窗口和价格窗口不同，不能据此判定任一方错误。"]
        return []

    def risk_context(self, state: State) -> dict:
        digests = {key: self.agent_digest(state, key) for key in FINDING_AGENTS}
        for key in REVISABLE:
            if state.get(f"{key}{REVISION_SUFFIX}"):
                digests[f"{key}{REVISION_SUFFIX}"] = self.agent_digest(state, f"{key}{REVISION_SUFFIX}")
        return {
            "question": self.req.question,
            "mode": self.req.mode,
            "metrics": (state.get("technical") or {}).get("metrics"),
            "conflicts": self.detected_conflicts(state),
            "revisable_agents": list(REVISABLE),
            "is_revision_round": any(state.get(name) for name in REVISION_NODES),
            "agent_outputs": digests,
            "evidence": self.evidence_pool(state),
        }

    def arbiter_context(self, state: State, conflicts: list[str]) -> dict:
        news = state.get("news") or {}
        return {
            "question": self.req.question,
            "conflicts": conflicts,
            "technical_summary": (state.get("technical") or {}).get("summary", ""),
            "news_labels": [
                {"sentiment": item.get("sentiment"), "title": item.get("title", "")}
                for item in (news.get("items") or [])[:12]
            ],
            "evidence": self.evidence_pool(state),
        }

    def report_context(self, state: State) -> dict:
        risk = state.get("risk") or {}
        return {
            "question": self.req.question,
            "mode": self.req.mode,
            "market": self.req.market,
            "symbol": self.req.symbol,
            "currency": (state.get("market") or {}).get("currency"),
            "as_of": str(self.req.as_of),
            "metrics": (state.get("technical") or {}).get("metrics"),
            "agents": {
                key: self.agent_digest(state, key) for key in (*FINDING_AGENTS, "risk") if state.get(key)
            },
            "revisions": {
                key: self.agent_digest(state, f"{key}{REVISION_SUFFIX}")
                for key in REVISABLE
                if state.get(f"{key}{REVISION_SUFFIX}")
            },
            "risks": risk.get("risks", []),
            "conflicts": risk.get("conflicts", []),
            "arbitration": (state.get("arbiter") or {}).get("ruling", ""),
            "skipped": [
                {"role": AGENT_NAMES.get(key, key), "reason": (state.get(key) or {}).get("reason")}
                for key in PLANNABLE_AGENTS
                if (state.get(key) or {}).get("status") == "skipped"
            ],
            "evidence": self.evidence_pool(state),
        }

    # ---------- 节点 ----------

    def node_manager(self, state: State, name: str) -> dict:
        plan, planner, warnings = None, "规则规划器", []
        if self.req.use_llm:
            call_id = self.repo.reserve_call(self.job["id"], self.job["owner"])
            if call_id is None:
                warnings.append("模型调用预算已用完，本轮由规则规划器制定计划。")
            else:
                try:
                    raw, usage = call_agent(
                        self.settings, AGENT_SPECS["manager"], self.planning_context(), self.transport
                    )
                    self.repo.finish_call(call_id, usage=usage)
                    plan, planner = raw, "模型规划"
                except ProviderError as exc:
                    self.repo.finish_call(call_id, error=str(exc))
                    warnings.append(str(exc))
        if plan is None:
            plan = rule_plan(self.req, self.settings)
        plan = normalize_plan(plan)
        return {
            "status": "completed",
            "summary": f"研究计划已建立（{planner}）：本轮启用 {len(plan['enabled_agents'])} 个可选角色。",
            "question": self.req.question,
            "scope": "A 股，日线，CNY" if self.req.market == "CN" else "美国上市股票 / ETF，日线，USD",
            "plan": plan,
            "planner": planner,
            "execution": "条件路由：规划器决定角色，风控可质询返工，首席仲裁裁决冲突。",
            "budget": {
                "max_llm_calls": self.settings.max_llm_calls,
                "max_output_tokens_per_call": self.settings.max_llm_output_tokens,
                "timeout_seconds": self.settings.task_timeout_seconds,
            },
            "warnings": warnings,
        }

    def node_market(self, state: State, name: str) -> dict:
        if not self.enabled(state, "market"):
            return self.skipped_output("market", state)
        output = self.provider.market(self.req)  # 行情失败是致命错误，不在此降级
        deterministic = {
            **output,
            "status": "completed",
            "agent": "market",
            "summary": f"取得 {len(output['bars'])} 条日线，已完成价格与日期校验。",
        }
        return self.run_agent("market", self.market_context(deterministic), deterministic)

    def node_technical(self, state: State, name: str) -> dict:
        if not self.enabled(state, "technical"):
            return self.skipped_output("technical", state)
        market = state["market"]
        metrics = analyze(market["bars"])
        eid = market["evidence"][0]["id"]
        claims = [
            {
                "text": f"样本最后收盘价为 {metrics['last_close']:.2f} {market['currency']}，样本区间价格变化 {metrics['period_return_pct']:+.2f}%。",
                "kind": "fact",
                "evidence_ids": [eid],
            },
            {
                "text": f"均线规则识别的趋势为「{metrics['trend']}」；该分类描述过去走势，不代表未来收益。",
                "kind": "interpretation",
                "evidence_ids": [eid],
            },
        ]
        if metrics["rsi14"] is not None:
            claims.append(
                {
                    "text": f"Wilder RSI14 为 {metrics['rsi14']:.2f}；指标本身不是买卖建议。",
                    "kind": "fact",
                    "evidence_ids": [eid],
                }
            )
        deterministic = {
            "status": "completed",
            "agent": "technical",
            "summary": f"样本趋势：{metrics['trend']}；指标由 Python 直接计算。",
            "metrics": metrics,
            "claims": claims,
            "warnings": list(market["warnings"]),
            "evidence": market["evidence"],
        }
        return self.run_agent("technical", self.technical_context(deterministic), deterministic)

    def node_news(self, state: State, name: str) -> dict:
        if not self.enabled(state, "news"):
            return self.skipped_output("news", state)
        try:
            output = self.provider.news(self.req)
            deterministic = {
                **output,
                "status": output.get("status", "completed" if output["items"] else "partial"),
                "agent": "news",
                "summary": f"整理 {len(output['items'])} 条去重事件；保留发布时间和原始来源。",
            }
        except ProviderError as exc:
            return {
                "status": "partial",
                "agent": "news",
                "items": [],
                "evidence": [],
                "warnings": [str(exc)],
                "summary": "新闻数据不可用。",
            }
        if not deterministic["items"]:
            return deterministic  # 没有素材可分析，不消耗模型预算
        return self.run_agent("news", self.news_context(deterministic), deterministic)

    def node_macro(self, state: State, name: str) -> dict:
        if not self.enabled(state, "macro"):
            return self.skipped_output("macro", state)
        try:
            output = self.provider.macro(self.req)
            latest = output["items"][0]
            deterministic = {
                **output,
                "status": "completed",
                "agent": "macro",
                "summary": f"最近观测期 {latest['date']}，利率 {latest['value']:.2f}%。",
                "interpretation": "利率变化可能通过融资成本和折现率影响估值；方向和强度依赖企业盈利与市场预期，单一利率不能确定价格方向。",
            }
        except ProviderError as exc:
            return {
                "status": "partial",
                "agent": "macro",
                "items": [],
                "evidence": [],
                "warnings": [str(exc)],
                "summary": "宏观数据不可用。",
            }
        if not deterministic["items"]:
            return deterministic
        return self.run_agent("macro", self.macro_context(deterministic), deterministic)

    def risk_signals(self, state: State) -> tuple[list[dict], list[str]]:
        metrics = (state.get("technical") or {}).get("metrics") or {}
        market = state.get("market") or {}
        risks: list[dict] = []
        if self.req.mode == "demo":
            risks.append(
                {
                    "level": "info",
                    "title": "合成演示数据",
                    "detail": "行情、新闻和宏观情景全部用于演示，不反映真实市场。",
                }
            )
        for key, label in [("news", "新闻"), ("macro", "宏观")]:
            if (state.get(key) or {}).get("status") == "partial":
                risks.append(
                    {
                        "level": "warning",
                        "title": f"{label}证据缺失",
                        "detail": "相关结论已降级，报告不能被视为完整研究。",
                    }
                )
        if market.get("adjustment") == "raw":
            risks.append(
                {
                    "level": "warning",
                    "title": "未复权行情",
                    "detail": "拆股和分红可能产生虚假跳变；本报告不计算总回报。",
                }
            )
        bars = market.get("bars") or []
        if any(abs(b["close"] / a["close"] - 1) > 0.2 for a, b in pairwise(bars)):
            risks.append(
                {
                    "level": "high",
                    "title": "显著价格跳变",
                    "detail": "存在单日超过 20% 的价格变化，应先核查公司行动或数据错误。",
                }
            )
        if metrics.get("sample_size", 0) < 50:
            risks.append(
                {
                    "level": "warning",
                    "title": "长期趋势样本不足",
                    "detail": "不足 50 条日线，不输出 SMA50 趋势判断。",
                }
            )
        rsi = metrics.get("rsi14")
        if rsi is not None and (rsi >= 70 or rsi <= 30):
            risks.append(
                {
                    "level": "warning",
                    "title": "动量处于极端区间",
                    "detail": "RSI 可能长时间停留在极端水平，不能独立作为反转信号。",
                }
            )
        volatility = metrics.get("volatility_pct")
        if volatility is not None and volatility > 40:
            risks.append(
                {
                    "level": "warning",
                    "title": "样本波动较高",
                    "detail": "年化历史波动率超过 40%；该统计值不是未来波动预测。",
                }
            )
        risks.append(
            {
                "level": "info",
                "title": "研究覆盖边界",
                "detail": "未包含财报核验、估值模型和组合风险；未进行收益预测或交易策略回测。",
            }
        )
        return risks, self.detected_conflicts(state)

    def node_risk(self, state: State, name: str) -> dict:
        risks, conflicts = self.risk_signals(state)
        coverage = {
            "market": True,
            "technical": True,
            "news": bool((state.get("news") or {}).get("items")),
            "macro": bool((state.get("macro") or {}).get("items")),
            "fundamentals": False,
        }
        deterministic = {
            "status": "completed",
            "agent": "risk",
            "summary": f"发现 {len(risks)} 项风险/限制、{len(conflicts)} 项方向分歧。",
            "risks": risks,
            "conflicts": conflicts,
            "coverage": coverage,
            "challenges": [],
            "warnings": [],
        }
        output = self.run_agent("risk", self.risk_context(state), deterministic)
        finding = output.get("finding")
        if finding:
            # 质询提升到顶层，条件路由直接读它
            output["challenges"] = finding.get("challenges") or []
        return output

    def node_arbiter(self, state: State, name: str) -> dict:
        conflicts = self.latest_conflicts(state)
        if not conflicts:
            return {
                **SKIPPED_DEFAULTS,
                "status": "skipped",
                "agent": "arbiter",
                "summary": "本轮未检测到结论冲突，无需仲裁。",
                "reason": "技术面趋势与新闻情绪方向一致，或新闻证据本轮未启用。",
                "conflict": "",
                "ruling": "",
                "rationale": "",
                "evidence_ids": [],
            }
        evidence = self.evidence_pool(state)
        deterministic = {
            "status": "completed",
            "agent": "arbiter",
            "summary": f"检测到 {len(conflicts)} 项方向分歧，已给出裁决。",
            "conflict": " ".join(conflicts),
            "ruling": "以价格与成交数据为主证据，供应商情绪标签仅作背景参考。",
            "rationale": "价格是市场参与者行为的直接观测；情绪标签是对文本的二手聚合，未经原文核验。",
            "evidence_ids": [item["id"] for item in evidence[:3]],
        }
        output = self.run_agent("arbiter", self.arbiter_context(state, conflicts), deterministic)
        finding = output.get("finding")
        if finding:
            for field in ("conflict", "ruling", "rationale", "evidence_ids"):
                if finding.get(field):
                    output[field] = finding[field]
        return output

    def latest_conflicts(self, state: State) -> list[str]:
        for key in (RISK_REVISION, "risk"):
            conflicts = (state.get(key) or {}).get("conflicts")
            if conflicts:
                return list(conflicts)
        return self.detected_conflicts(state)

    def node_revision(self, state: State, name: str) -> dict:
        """复审节点 technical@1 / news@1 / macro@1：在风控质询下重新研判。

        复用原节点的输入构造器，把质询一并送进上下文；确定性输出沿用上一轮，
        以保证即使模型不可用，返工记录也真实存在。
        """
        key = name[: -len(REVISION_SUFFIX)]
        source = state.get(key) or {}
        challenges = self.challenges_for(state, key)
        deterministic = {
            **{k: v for k, v in source.items() if k != "finding"},
            "status": source.get("status", "completed"),
            "agent": key,
            "revised": True,
            "reviewed_challenges": [c.get("reason", "") for c in challenges],
            "summary": f"已按风控质询复审「{AGENT_SPECS[key].role}」的结论。",
            "warnings": list(source.get("warnings") or []),
        }
        if not self.req.use_llm or not (source.get("evidence") or []):
            return deterministic
        builders = {
            "market": self.market_context,
            "technical": self.technical_context,
            "news": self.news_context,
            "macro": self.macro_context,
        }
        context = {**builders[key](source), "challenges": challenges}
        return self.run_agent(key, context, deterministic)

    def challenges_for(self, state: State, key: str) -> list[dict]:
        collected = []
        for source in ("risk", RISK_REVISION):
            for challenge in (state.get(source) or {}).get("challenges") or []:
                if challenge.get("target_agent") == key:
                    collected.append(challenge)
        return collected

    def node_report(self, state: State, name: str) -> dict:
        evs = self.evidence_pool(state)
        limitations: list[str] = []
        # risk 与 arbiter 同样会调用模型，它们的降级原因必须一并收进 limitations。
        # 漏掉这两个键会让「风控的模型调用失败」表现为一份没有任何提示的 completed 报告：
        # 确定性风控结论照常产出，只是模型那一层哑火，而报告对此只字不提。
        for key in (*FINDING_AGENTS, "risk", "arbiter", *REVISION_NODES):
            for warning in (state.get(key) or {}).get("warnings") or []:
                if warning not in limitations:
                    limitations.append(warning)
        for key in PLANNABLE_AGENTS:
            output = state.get(key) or {}
            if output.get("status") == "skipped" and output.get("reason"):
                limitations.append(f"{AGENT_SPECS[key].role}本轮未启用：{output['reason']}")

        metrics = state["technical"]["metrics"]
        summary = (
            f"{self.req.symbol} 在所选 {metrics['sample_size']} 条日线样本中呈{metrics['trend']}特征，"
            f"区间价格变化 {metrics['period_return_pct']:+.2f}%。取得 "
            f"{len((state.get('news') or {}).get('items') or [])} 条新闻/公告/情景；"
            f"宏观证据{'可用' if (state.get('macro') or {}).get('items') else '缺失'}，"
            "结论需结合证据覆盖范围理解。"
        )
        if self.req.mode == "demo":
            summary = "【合成数据演示】" + summary

        ai, usage, ai_failed = None, None, False
        if self.req.use_llm:
            call_id = self.repo.reserve_call(self.job["id"], self.job["owner"])
            if call_id:
                try:
                    ai, usage = call_agent(
                        self.settings, AGENT_SPECS["report"], self.report_context(state), self.transport
                    )
                    self.repo.finish_call(call_id, usage=usage)
                except ProviderError as exc:
                    self.repo.finish_call(call_id, error=str(exc))
                    limitations.append(str(exc))
                    ai_failed = True
            else:
                limitations.append(
                    "模型调用预算已用完（可能发生在中断前）；本次恢复使用确定性报告，避免重复计费。"
                )
                ai_failed = True
        if ai:
            limitations.append(
                "AI 研判已校验结构和引用 ID，但未自动证明每个推断均被原文支持；请结合来源审阅。"
            )

        witness = (*FINDING_AGENTS, "risk", "arbiter", *REVISION_NODES)
        partial = ai_failed or any((state.get(key) or {}).get("status") == "partial" for key in witness)

        result = {
            "status": "partial" if partial else "completed",
            "title": f"{self.req.symbol} 市场研究报告",
            "symbol": self.req.symbol,
            "market": self.req.market,
            "currency": state["market"]["currency"],
            "adjustment": state["market"]["adjustment"],
            "name": state["market"].get("name", self.req.symbol),
            "provider": state["market"].get("provider", "Atlas 合成演示"),
            "question": self.req.question,
            "as_of": str(self.req.as_of),
            "mode": self.req.mode,
            "generated_at": utcnow(),
            "summary": summary,
            "metrics": metrics,
            "claims": state["technical"]["claims"],
            "evidence": evs,
            "risks": state["risk"]["risks"],
            "conflicts": state["risk"]["conflicts"],
            "coverage": state["risk"]["coverage"],
            "limitations": limitations,
            "news": (state.get("news") or {}).get("items") or [],
            "macro": state.get("macro") or {},
            "chart": state["market"]["bars"],
            "ai_synthesis": ai,
            "usage": usage,
            "planner": (state.get("manager") or {}).get("planner", "规则规划器"),
            "plan": self.plan_of(state),
            "agent_findings": self.agent_findings(state),
            "challenges": self.challenge_records(state),
            "arbitration": state.get("arbiter") or None,
            "engine": "多 Agent 协作 + AI 研判" if ai else "多 Agent 协作（确定性规则）",
            "version": "0.1.0",
        }
        result["markdown"] = render_markdown(result)
        return result

    # ---------- 报告用的汇总视图 ----------

    def agent_findings(self, state: State) -> dict:
        findings = {}
        for key in (*FINDING_AGENTS, "risk"):
            finding = (state.get(key) or {}).get("finding")
            if finding:
                findings[key] = finding
        for key in REVISABLE:
            finding = (state.get(f"{key}{REVISION_SUFFIX}") or {}).get("finding")
            if finding:
                findings[f"{key}{REVISION_SUFFIX}"] = finding
        return findings

    def challenge_records(self, state: State) -> list[dict]:
        records = []
        for source, round_no in (("risk", 1), (RISK_REVISION, 2)):
            output = state.get(source)
            if not output:
                continue
            # 回边只允许一轮：只有第一轮（risk）的质询会被执行。risk@1 的质询不再触发返工，
            # 所以第二轮即便 target 的 @1 节点存在，那也是回应第一轮质询的产物，
            # 据此把第二轮记成「已返工」就是在报告里写一句不成立的话。
            triggers_rework = source == "risk"
            for challenge in output.get("challenges") or []:
                target = challenge.get("target_agent")
                accepted = target in REVISABLE
                reworked = triggers_rework and bool(state.get(f"{target}{REVISION_SUFFIX}"))
                records.append(
                    {
                        "target_agent": target,
                        "reason": challenge.get("reason", ""),
                        "request": challenge.get("request", ""),
                        "round": round_no,
                        "accepted": accepted,
                        "resolved": bool(accepted and reworked),
                    }
                )
        return records


def render_markdown(report: dict) -> str:
    # Escape external text so the downloadable Markdown cannot introduce raw HTML/images.
    def safe(value) -> str:
        value = str(value).replace("<", "&lt;").replace(">", "&gt;")
        for char in ["\\", "[", "]", "*", "_", "`", "#", "!"]:
            value = value.replace(char, "\\" + char)
        return value.replace("\r", " ").replace("\n", " ")

    plan = report.get("plan") or {}
    lines = [
        f"# {report['title']}",
        "",
        f"数据模式：{'合成演示' if report['mode'] == 'demo' else '真实数据'}",
        f"市场：{report.get('market', 'US')} · 币种：{report.get('currency', 'USD')} · 价格口径：{report.get('adjustment', 'raw')}",
        f"截止日期：{report['as_of']} · 生成时间：{report['generated_at']}",
        f"研究引擎：{report['engine']} · 完整性：{report['status']}",
        "",
        "## 研究问题",
        safe(report["question"]),
        "",
        "## 研究计划",
        f"规划方式：{safe(report.get('planner', '规则规划器'))}",
        safe(plan.get("rationale", "")),
    ]
    for key in plan.get("enabled_agents", []):
        lines.append(
            f"- 启用 {safe(AGENT_NAMES.get(key, key))}：{safe((plan.get('focus') or {}).get(key, '—'))}"
        )
    for key, reason in (plan.get("skipped_reason") or {}).items():
        lines.append(f"- 未启用 {safe(key)}：{safe(reason)}")
    lines.extend(["", "## 摘要", safe(report["summary"]), "", "## 技术观察"])
    for claim in report["claims"]:
        lines.append(f"- {safe(claim['text'])}（{', '.join(claim['evidence_ids'])}）")
    lines.extend(["", "## 新闻事件"])
    for item in report["news"]:
        lines.append(f"- {safe(item['title'])} — {safe(item['summary'])}（{item['evidence_id']}）")
    if not report["news"]:
        lines.append("没有可用新闻证据。")
    lines.extend(["", "## 宏观背景", safe(report["macro"].get("summary", ""))])
    if report["macro"].get("interpretation"):
        lines.append(safe(report["macro"]["interpretation"]))
    findings = report.get("agent_findings") or {}
    if findings:
        lines.extend(["", "## 各角色研判"])
        for key, finding in findings.items():
            base = key[: -len("@1")] if key.endswith("@1") else key
            lines.append(f"### {safe(AGENT_NAMES.get(base, base))}{'（复审）' if key.endswith('@1') else ''}")
            lines.append(safe(finding.get("headline") or ""))
            for claim in finding.get("findings") or []:
                lines.append(f"- {safe(claim.get('text'))}（{', '.join(claim.get('evidence_ids') or [])}）")
            for question in finding.get("open_questions") or []:
                lines.append(f"- 待解问题：{safe(question)}")
    if report.get("challenges"):
        lines.extend(["", "## 质询与返工"])
        for item in report["challenges"]:
            who = AGENT_NAMES.get(item.get("target_agent"), item.get("target_agent"))
            verdict = "已返工" if item.get("resolved") else ("未返工" if item.get("accepted") else "不可返工")
            lines.append(
                f"- 第 {item.get('round')} 轮 · {safe(who)}（{verdict}）：{safe(item.get('reason'))}"
            )
            lines.append(f"  要求：{safe(item.get('request'))}")
    arbitration = report.get("arbitration")
    if arbitration and arbitration.get("status") != "skipped":
        lines.extend(["", "## 仲裁结论"])
        lines.append(safe(f"冲突：{arbitration.get('conflict', '')}"))
        lines.append(safe(f"裁决：{arbitration.get('ruling', '')}"))
        lines.append(safe(f"依据：{arbitration.get('rationale', '')}"))
    if report["ai_synthesis"]:
        lines.extend(["", "## AI 研判（待人工审阅）", safe(report["ai_synthesis"]["summary"])])
        for claim in report["ai_synthesis"]["claims"]:
            lines.append(f"- {safe(claim['text'])}（{', '.join(claim['evidence_ids'])}）")
        lines.extend(f"- 不确定性：{safe(x)}" for x in report["ai_synthesis"]["uncertainties"])
    lines.extend(["", "## 风险与分歧"])
    lines.extend(f"- {safe(r['title'])}：{safe(r['detail'])}" for r in report["risks"])
    lines.extend(f"- 分歧：{safe(x)}" for x in report["conflicts"])
    lines.extend(["", "## 数据限制"])
    lines.extend(f"- {safe(x)}" for x in report["limitations"])
    lines.extend(["", "## 证据来源"])
    for ev in report["evidence"]:
        lines.append(
            f"- {ev['id']} · {safe(ev['source'])} · {safe(ev['title'])} · 观测/发布：{safe(ev['observed_at'])}"
        )
        if ev["url"]:
            lines.append(f"  来源地址：{safe(ev['url'])}")
        lines.append(f"  快照 SHA256：{ev['snapshot_hash']}")
    return "\n".join(lines) + "\n"
