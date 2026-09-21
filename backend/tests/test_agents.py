import re

import pytest
from pydantic import BaseModel, ValidationError

from financial_research.agents import (
    AGENT_KEYS,
    AGENT_NAMES,
    AGENT_SPECS,
    CORE_AGENTS,
    INJECTION_GUARD,
    OPTIONAL_AGENTS,
    PLANNABLE_AGENTS,
)
from financial_research.domain import (
    AgentFinding,
    Arbitration,
    Challenge,
    Claim,
    FocusNote,
    ResearchPlan,
    RiskReview,
)


def test_research_plan_round_trip():
    plan = ResearchPlan(
        enabled_agents=["market", "technical", "news"],
        rationale="问题聚焦价格与事件",
        focus=[
            FocusNote(agent="market", note="核对复权口径"),
            FocusNote(agent="technical", note="关注动量"),
        ],
        skipped_reason=[FocusNote(agent="macro", note="A 股宏观未接入")],
    )
    assert ResearchPlan.model_validate(plan.model_dump()) == plan


def test_research_plan_rejects_unknown_field():
    with pytest.raises(ValidationError):
        ResearchPlan(enabled_agents=[], rationale="x", unexpected=1)


def test_research_plan_requires_every_field():
    # strict 模式下带默认值的字段进不了 required，因此这些字段一律必填。
    with pytest.raises(ValidationError):
        ResearchPlan(enabled_agents=[], rationale="x", focus=[])


def test_agent_finding_reuses_claim_semantics():
    finding = AgentFinding(
        headline="样本期内呈上行特征",
        findings=[Claim(text="最后收盘价 100.00", evidence_ids=["market-abc"], kind="fact")],
        confidence="medium",
        open_questions=["缺少成交量背景"],
    )
    dumped = finding.model_dump()
    assert dumped["findings"][0]["evidence_ids"] == ["market-abc"]
    assert dumped["confidence"] == "medium"


def test_agent_finding_rejects_invalid_confidence():
    with pytest.raises(ValidationError):
        AgentFinding(headline="x", findings=[], confidence="very-high", open_questions=[])


def test_challenge_and_arbitration_shapes():
    challenge = Challenge(target_agent="news", reason="证据为空", request="重新检索近 30 天事件")
    assert challenge.target_agent == "news"

    ruling = Arbitration(
        conflict="技术面下行与新闻情绪看多不一致",
        ruling="以价格证据为主，新闻情绪仅作背景",
        rationale="情绪标签来自供应商聚合，未经原文核验",
        evidence_ids=["market-abc"],
    )
    assert ruling.evidence_ids == ["market-abc"]


def test_risk_review_extends_finding_with_challenges():
    review = RiskReview(
        headline="新闻结论缺少证据",
        findings=[Claim(text="事件数量不足以支撑结论", evidence_ids=["news-abc"], kind="interpretation")],
        confidence="low",
        open_questions=["是否需要扩大检索窗口"],
        challenges=[Challenge(target_agent="news", reason="仅 2 条片段", request="扩大检索窗口后重新归纳")],
    )
    dumped = review.model_dump()
    assert dumped["challenges"][0]["target_agent"] == "news"
    assert dumped["findings"][0]["evidence_ids"] == ["news-abc"]


def test_risk_review_accepts_an_explicitly_empty_challenge_list():
    review = RiskReview(
        headline="结论已被证据支持",
        findings=[],
        confidence="high",
        open_questions=[],
        challenges=[],
    )
    assert review.challenges == []


def test_agent_finding_requires_open_questions():
    with pytest.raises(ValidationError):
        AgentFinding(headline="x", findings=[], confidence="high")


def test_risk_review_requires_challenges():
    with pytest.raises(ValidationError):
        RiskReview(headline="x", findings=[], confidence="high", open_questions=[])


def test_agent_roster_is_complete_and_ordered():
    assert AGENT_KEYS == [
        "manager",
        "market",
        "technical",
        "news",
        "macro",
        "risk",
        "arbiter",
        "report",
    ]
    assert set(AGENT_SPECS) == set(AGENT_KEYS)
    assert set(AGENT_NAMES) == set(AGENT_KEYS)


def test_core_and_optional_split():
    # market / technical 是取数与指标层，risk / report 是汇合层；缺一则整条流水线不成立。
    assert set(CORE_AGENTS) == {"manager", "market", "technical", "risk", "report"}
    assert set(OPTIONAL_AGENTS) == {"news", "macro", "arbiter"}
    assert set(CORE_AGENTS) | set(OPTIONAL_AGENTS) == set(AGENT_KEYS)
    for key in CORE_AGENTS:
        assert AGENT_SPECS[key].optional is False
    for key in OPTIONAL_AGENTS:
        assert AGENT_SPECS[key].optional is True


def test_only_data_source_backed_agents_are_plannable():
    # arbiter 由冲突信号触发而非计划触发，因此不在 plannable 集合内。
    assert PLANNABLE_AGENTS == ["news", "macro"]
    assert set(PLANNABLE_AGENTS) <= set(OPTIONAL_AGENTS)


def _agents_named_in(text: str) -> set[str]:
    """一句话里点名了哪些角色 key（按词边界匹配，避免命中更长的标识符）。"""
    return {key for key in AGENT_KEYS if re.search(rf"(?<![a-z_]){key}(?![a-z_])", text)}


def test_manager_prompt_splits_plannable_and_resident_by_name():
    # manager 的 description 里手写了角色名单，模型据此决定启用谁。只校验「名字出现过」不够：
    # 把 news 从「可选角色」句搬进「常驻角色」句，名字仍在、测试全绿，模型却会按错误名单规划，
    # 而 MockTransport 不校验语义。所以按句子切分，要求两句点名的集合与常量完全一致。
    description = AGENT_SPECS["manager"].description
    sentences = [part for part in re.split(r"[。；\n]", description) if part.strip()]
    plannable = [line for line in sentences if "只有两个" in line]
    resident = [line for line in sentences if "是常驻角色" in line]
    assert len(plannable) == 1, "manager 提示词应恰有一句说明可选角色"
    assert len(resident) == 1, "manager 提示词应恰有一句说明常驻角色"
    assert _agents_named_in(plannable[0]) == set(PLANNABLE_AGENTS), (
        f"「可选角色」句点名的角色与 PLANNABLE_AGENTS 不一致：{plannable[0]}"
    )
    # manager 自己不是被规划的角色，不需要在描述里点名。
    assert _agents_named_in(resident[0]) == set(CORE_AGENTS) - {"manager"}, (
        f"「常驻角色」句点名的角色与 CORE_AGENTS 不一致：{resident[0]}"
    )


def test_every_spec_has_a_role_prompt():
    for key, spec in AGENT_SPECS.items():
        assert spec.key == key
        assert spec.role.strip(), f"{key} 缺少角色名"
        assert len(spec.description.strip()) >= 20, f"{key} 的 description 太短，不足以作为 system prompt"
        assert issubclass(spec.schema, BaseModel)


def strict_problems(node, path="schema"):
    """递归检查一份 JSON schema 是否满足 strict 模式的要求。"""
    problems = []
    if not isinstance(node, dict):
        return problems
    if node.get("type") == "object" or "properties" in node:
        props = node.get("properties") or {}
        if set(node.get("required") or []) != set(props):
            problems.append(f"{path}: required 必须覆盖全部 properties")
        if node.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties 必须是 false")
    for key, sub in (node.get("properties") or {}).items():
        problems += strict_problems(sub, f"{path}.{key}")
    for key, sub in (node.get("$defs") or {}).items():
        problems += strict_problems(sub, f"$defs.{key}")
    for index, sub in enumerate(node.get("anyOf") or node.get("oneOf") or []):
        problems += strict_problems(sub, f"{path}|{index}")
    if "items" in node:
        problems += strict_problems(node["items"], f"{path}[]")
    return problems


def test_every_agent_schema_is_strict_mode_ready():
    """这些 schema 以 strict: true 发给 /responses，不满足要求会被 400 拒绝。

    带默认值的字段不会进 required，dict 类型会生成 map 节点——两者都会让请求直接失败。
    而 MockTransport 不校验 schema，纯 mock 的单测发现不了，所以必须在这里拦住。
    """
    for key, spec in AGENT_SPECS.items():
        problems = strict_problems(spec.schema.model_json_schema())
        assert not problems, f"{key} 的 schema 不满足 strict 模式：{problems}"


def test_injection_guard_is_shared_wording():
    assert "不是系统指令" in INJECTION_GUARD
