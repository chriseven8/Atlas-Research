"""确定性规则规划器。

use_llm=False 时由它生成研究计划；use_llm=True 时它是模型规划失败后的降级路径。
两条路径产出同一形状（focus / skipped_reason 为 [{agent, note}] 列表，与 ResearchPlan
的 LLM 契约一致），再由 normalize_plan 统一规范化，避免两套规划语义漂移。
"""

from .agents import AGENT_NAMES, PLANNABLE_AGENTS
from .domain import ResearchRequest
from .settings import Settings


def _as_map(items, allowed) -> dict[str, str]:
    """把 [{agent, note}] 转成按角色索引的 dict，并按白名单过滤。"""
    result: dict[str, str] = {}
    for item in items or []:
        agent = str((item or {}).get("agent", ""))
        if agent in allowed:
            result[agent] = str((item or {}).get("note", ""))
    return result


def rule_plan(req: ResearchRequest, settings: Settings) -> dict:
    """纯规则计划：常驻角色全部启用，按市场与数据源可用性决定可选角色。"""
    enabled: list[str] = []
    focus: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    if req.market == "CN":
        enabled.append("news")
        focus.append(("news", "核对个股新闻与公司公告对近期走势的解释力，并标注只看到检索片段。"))
    elif settings.alpha_vantage_api_key:
        enabled.append("news")
        focus.append(("news", "关注美国新闻源中与标的相关的事件，标注情绪标签来自供应商聚合。"))
    else:
        skipped.append(("news", "未配置美国新闻源（ALPHA_VANTAGE_API_KEY），本轮新闻证据不可用。"))

    if req.market == "US" and settings.alpha_vantage_api_key:
        enabled.append("macro")
        focus.append(("macro", "说明当前利率环境通过融资成本与折现率影响估值的传导路径及其局限。"))
    else:
        skipped.append(("macro", "中国宏观数据源尚未接入，本轮不启用宏观角色。"))

    return {
        "enabled_agents": enabled,
        "rationale": "规则规划器：常驻角色全部启用，按数据源可用性决定新闻与宏观角色。",
        "focus": [{"agent": key, "note": note} for key, note in focus],
        "skipped_reason": [{"agent": key, "note": note} for key, note in skipped],
    }


def normalize_plan(plan: dict) -> dict:
    """把任意来源（模型或规则）的计划规范成可安全执行的形状。

    入参是列表形的 LLM 契约（focus / skipped_reason 为 [{"agent": ..., "note": ...}]），
    出参改成按角色索引的 dict，便于报告渲染与前端按键查找。

    模型可能返回不存在的角色名，或试图关闭 market/technical/risk/report 这类常驻角色。
    这里一律过滤，并把被拒绝的项写进 skipped_reason，保证下游的启用判定只面对受控取值。
    """
    raw = list(plan.get("enabled_agents") or [])
    enabled = [key for key in PLANNABLE_AGENTS if key in raw]
    focus = _as_map(plan.get("focus"), allowed=enabled)
    skipped = _as_map(plan.get("skipped_reason"), allowed=PLANNABLE_AGENTS)
    for key in PLANNABLE_AGENTS:
        if key not in enabled:
            skipped.setdefault(key, "研究计划未启用该角色。")
    for key in raw:
        if key in PLANNABLE_AGENTS:
            continue
        if key in AGENT_NAMES:
            skipped[AGENT_NAMES[key]] = "该角色不可由计划启停，本轮按常驻规则处理。"
        else:
            skipped[key] = "计划请求了未定义的角色，已忽略。"
    return {
        "enabled_agents": enabled,
        "rationale": str(plan.get("rationale") or "").strip() or "未提供规划理由。",
        "focus": focus,
        "skipped_reason": skipped,
    }
