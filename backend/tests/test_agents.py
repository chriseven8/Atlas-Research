import pytest
from pydantic import ValidationError

from financial_research.domain import (
    AgentFinding,
    Arbitration,
    Challenge,
    Claim,
    ResearchPlan,
    RiskReview,
)


def test_research_plan_round_trip():
    plan = ResearchPlan(
        enabled_agents=["market", "technical", "news"],
        rationale="问题聚焦价格与事件",
        focus={"market": "核对复权口径", "technical": "关注动量"},
        skipped_reason={"macro": "A 股宏观未接入"},
    )
    assert ResearchPlan.model_validate(plan.model_dump()) == plan


def test_research_plan_rejects_unknown_field():
    with pytest.raises(ValidationError):
        ResearchPlan(enabled_agents=[], rationale="x", unexpected=1)


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


def test_risk_review_defaults_to_no_challenges():
    review = RiskReview(headline="结论已被证据支持", findings=[], confidence="high")
    assert review.challenges == []
