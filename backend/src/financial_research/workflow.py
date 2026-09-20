import time
from itertools import pairwise
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .analytics import analyze
from .domain import LeaseLost, ProviderError, ResearchRequest, utcnow
from .llm import synthesize
from .providers import provider_for
from .settings import Settings
from .storage import Repository


class State(TypedDict, total=False):
    request: dict
    manager: dict
    market: dict
    technical: dict
    news: dict
    macro: dict
    risk: dict
    report: dict


def public_error(exc: Exception) -> str:
    if isinstance(exc, (ProviderError, TimeoutError, LeaseLost)):
        return str(exc)
    return f"研究节点执行失败（{type(exc).__name__}），请查看测试与服务日志。"


class ResearchWorkflow:
    def __init__(self, settings: Settings, repo: Repository, job: dict, provider=None):
        self.settings, self.repo, self.job = settings, repo, job
        self.req = ResearchRequest.model_validate(job["request"])
        self.provider = provider or provider_for(self.req, settings)

    def wrap(self, name: str):
        def node(state: State) -> dict:
            self.repo.check_active(self.job["id"], self.job["owner"])
            cached = self.repo.start_node(self.job["id"], self.job["owner"], name)
            if cached is not None:
                return {name: cached}
            started = time.monotonic()
            try:
                output = getattr(self, name)(state)
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

    def run(self) -> State:
        graph = StateGraph(State)
        for name in ["manager", "market", "technical", "news", "macro", "risk", "report"]:
            graph.add_node(name, self.wrap(name))
        graph.add_edge(START, "manager")
        for name in ["market", "news", "macro"]:
            graph.add_edge("manager", name)
        graph.add_edge("market", "technical")
        graph.add_edge(["technical", "news", "macro"], "risk")
        graph.add_edge("risk", "report")
        graph.add_edge("report", END)
        return graph.compile().invoke(
            {"request": self.req.model_dump(mode="json")}, {"recursion_limit": 15, "max_concurrency": 3}
        )

    def manager(self, state: State) -> dict:
        return {
            "status": "completed",
            "summary": "研究计划已建立：先取证，再分析，最后审查并生成报告。",
            "question": self.req.question,
            "scope": "A 股，日线，CNY" if self.req.market == "CN" else "美国上市股票 / ETF，日线，USD",
            "plan": [
                "获取并校验日线数据",
                "并行收集新闻与宏观背景",
                "计算技术指标",
                "审查数据缺口和观点分歧",
                "汇总证据与研究结论",
            ],
            "execution": "固定有向图；经理采用规则调度；不自动扩大研究范围。",
            "budget": {
                "max_llm_calls": self.settings.max_llm_calls,
                "max_output_tokens_per_call": self.settings.max_llm_output_tokens,
                "timeout_seconds": self.settings.task_timeout_seconds,
            },
        }

    def market(self, state: State) -> dict:
        output = self.provider.market(self.req)
        return {
            **output,
            "status": "completed",
            "summary": f"取得 {len(output['bars'])} 条日线，已完成价格与日期校验。",
        }

    def technical(self, state: State) -> dict:
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
        return {
            "status": "completed",
            "summary": f"样本趋势：{metrics['trend']}；指标由 Python 直接计算。",
            "metrics": metrics,
            "claims": claims,
            "warnings": market["warnings"],
        }

    def news(self, state: State) -> dict:
        try:
            output = self.provider.news(self.req)
            count = len(output["items"])
            return {
                **output,
                "status": output.get("status", "completed" if count else "partial"),
                "summary": f"整理 {count} 条去重事件；保留发布时间和原始来源。",
            }
        except ProviderError as exc:
            return {
                "status": "partial",
                "items": [],
                "evidence": [],
                "warnings": [str(exc)],
                "summary": "新闻数据不可用。",
            }

    def macro(self, state: State) -> dict:
        try:
            output = self.provider.macro(self.req)
            latest = output["items"][0]
            return {
                **output,
                "status": "completed",
                "summary": f"最近观测期 {latest['date']}，利率 {latest['value']:.2f}%。",
                "interpretation": "利率变化可能通过融资成本和折现率影响估值；方向和强度依赖企业盈利与市场预期，单一利率不能确定价格方向。",
            }
        except ProviderError as exc:
            return {
                "status": "partial",
                "items": [],
                "evidence": [],
                "warnings": [str(exc)],
                "summary": "宏观数据不可用。",
            }

    def risk(self, state: State) -> dict:
        metrics, market = state["technical"]["metrics"], state["market"]
        risks, conflicts = [], []
        if self.req.mode == "demo":
            risks.append(
                {
                    "level": "info",
                    "title": "合成演示数据",
                    "detail": "行情、新闻和宏观情景全部用于演示，不反映真实市场。",
                }
            )
        for name, label in [("news", "新闻"), ("macro", "宏观")]:
            if state[name]["status"] == "partial":
                risks.append(
                    {
                        "level": "warning",
                        "title": f"{label}证据缺失",
                        "detail": "相关结论已降级，报告不能被视为完整研究。",
                    }
                )
        if market["adjustment"] == "raw":
            risks.append(
                {
                    "level": "warning",
                    "title": "未复权行情",
                    "detail": "拆股和分红可能产生虚假跳变；本报告不计算总回报。",
                }
            )
        bars = market["bars"]
        if any(abs(b["close"] / a["close"] - 1) > 0.2 for a, b in pairwise(bars)):
            risks.append(
                {
                    "level": "high",
                    "title": "显著价格跳变",
                    "detail": "存在单日超过 20% 的价格变化，应先核查公司行动或数据错误。",
                }
            )
        if metrics["sample_size"] < 50:
            risks.append(
                {
                    "level": "warning",
                    "title": "长期趋势样本不足",
                    "detail": "不足 50 条日线，不输出 SMA50 趋势判断。",
                }
            )
        if metrics["rsi14"] is not None and (metrics["rsi14"] >= 70 or metrics["rsi14"] <= 30):
            risks.append(
                {
                    "level": "warning",
                    "title": "动量处于极端区间",
                    "detail": "RSI 可能长时间停留在极端水平，不能独立作为反转信号。",
                }
            )
        if metrics["volatility_pct"] is not None and metrics["volatility_pct"] > 40:
            risks.append(
                {
                    "level": "warning",
                    "title": "样本波动较高",
                    "detail": "年化历史波动率超过 40%；该统计值不是未来波动预测。",
                }
            )
        labels = [str(x.get("sentiment", "")).lower() for x in state["news"]["items"]]
        if (metrics["trend"] == "下行" and any("bullish" in x for x in labels)) or (
            metrics["trend"] == "上行" and any("bearish" in x for x in labels)
        ):
            conflicts.append(
                "供应商新闻情绪与价格趋势存在方向分歧；事件窗口和价格窗口不同，不能据此判定任一方错误。"
            )
        risks.append(
            {
                "level": "info",
                "title": "研究覆盖边界",
                "detail": "未包含财报核验、估值模型和组合风险；未进行收益预测或交易策略回测。",
            }
        )
        return {
            "status": "completed",
            "summary": f"发现 {len(risks)} 项风险/限制、{len(conflicts)} 项方向分歧。",
            "risks": risks,
            "conflicts": conflicts,
            "coverage": {
                "market": True,
                "technical": True,
                "news": bool(state["news"]["items"]),
                "macro": bool(state["macro"]["items"]),
                "fundamentals": False,
            },
        }

    def report(self, state: State) -> dict:
        evs = [e for key in ["market", "news", "macro"] for e in state[key]["evidence"]]
        evs = list({e["id"]: e for e in evs}.values())
        limitations = list(
            dict.fromkeys(w for key in ["market", "news", "macro"] for w in state[key]["warnings"])
        )
        metrics = state["technical"]["metrics"]
        summary = (
            f"{self.req.symbol} 在所选 {metrics['sample_size']} 条日线样本中呈{metrics['trend']}特征，"
            f"区间价格变化 {metrics['period_return_pct']:+.2f}%。取得 {len(state['news']['items'])} 条新闻/公告/情景；宏观证据{'可用' if state['macro']['items'] else '缺失'}，"
            "结论需结合证据覆盖范围理解。"
        )
        if self.req.mode == "demo":
            summary = "【合成数据演示】" + summary
        ai, usage = None, None
        ai_failed = False
        if self.req.use_llm:
            call_id = self.repo.reserve_call(self.job["id"], self.job["owner"])
            if call_id:
                try:
                    ai, usage = synthesize(
                        self.settings,
                        {
                            "question": self.req.question,
                            "mode": self.req.mode,
                            "market": self.req.market,
                            "currency": state["market"]["currency"],
                            "as_of": str(self.req.as_of),
                            "metrics": metrics,
                            "news": state["news"]["items"],
                            "macro": state["macro"]["items"],
                            "risks": state["risk"]["risks"],
                            "evidence": evs,
                        },
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
        partial = ai_failed or any(state[k]["status"] == "partial" for k in ["news", "macro"])
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
            "news": state["news"]["items"],
            "macro": state["macro"],
            "chart": state["market"]["bars"],
            "ai_synthesis": ai,
            "usage": usage,
            "engine": "规则分析 + AI 研判" if ai else "确定性规则分析",
            "version": "0.1.0",
        }
        result["markdown"] = render_markdown(result)
        return result


def render_markdown(report: dict) -> str:
    # Escape external text so the downloadable Markdown cannot introduce raw HTML/images.
    def safe(value) -> str:
        value = str(value).replace("<", "&lt;").replace(">", "&gt;")
        for char in ["\\", "[", "]", "*", "_", "`", "#", "!"]:
            value = value.replace(char, "\\" + char)
        return value.replace("\r", " ").replace("\n", " ")

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
        "## 摘要",
        safe(report["summary"]),
        "",
        "## 技术观察",
    ]
    for claim in report["claims"]:
        lines.append(f"- {safe(claim['text'])}（{', '.join(claim['evidence_ids'])}）")
    lines.extend(["", "## 新闻事件"])
    for item in report["news"]:
        lines.append(f"- {safe(item['title'])} — {safe(item['summary'])}（{item['evidence_id']}）")
    if not report["news"]:
        lines.append("没有可用新闻证据。")
    lines.extend(["", "## 宏观背景", safe(report["macro"]["summary"])])
    if report["macro"].get("interpretation"):
        lines.append(safe(report["macro"]["interpretation"]))
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
