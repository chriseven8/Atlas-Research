from financial_research.agents import PLANNABLE_AGENTS
from financial_research.domain import ResearchRequest
from financial_research.planner import normalize_plan, rule_plan


def make_req(**kwargs):
    return ResearchRequest(as_of="2026-06-10", **kwargs)


def test_cn_plan_enables_news_but_not_macro(settings):
    plan = rule_plan(make_req(market="CN", symbol="600519", mode="live"), settings)
    assert plan["enabled_agents"] == ["news"]
    assert {item["agent"] for item in plan["focus"]} == {"news"}
    assert all(item["note"] for item in plan["focus"])
    assert {item["agent"] for item in plan["skipped_reason"]} == {"macro"}


def test_us_plan_without_data_sources_skips_both_optional_agents(settings):
    plan = rule_plan(make_req(symbol="AAPL"), settings)
    assert plan["enabled_agents"] == []
    assert {item["agent"] for item in plan["skipped_reason"]} == {"news", "macro"}


def test_us_plan_with_api_key_enables_news_and_macro(settings):
    settings.alpha_vantage_api_key = "demo-key"
    plan = rule_plan(make_req(symbol="AAPL"), settings)
    assert plan["enabled_agents"] == ["news", "macro"]
    assert plan["skipped_reason"] == []


def test_normalize_filters_unknown_and_non_plannable_agents():
    plan = normalize_plan(
        {
            "enabled_agents": ["news", "market", "fundamentals", "news"],
            "rationale": "聚焦事件",
            "focus": [
                {"agent": "news", "note": "关注公告"},
                {"agent": "market", "note": "不该出现"},
            ],
            "skipped_reason": [
                {"agent": "macro", "note": "本轮不需要"},
                {"agent": "risk", "note": "不该出现"},
            ],
        }
    )
    assert plan["enabled_agents"] == ["news"]
    # 规范化的输出是按角色索引的 dict，供报告与前端按键查找。
    assert plan["focus"] == {"news": "关注公告"}
    assert plan["skipped_reason"]["macro"] == "本轮不需要"
    assert "市场数据专员" in plan["skipped_reason"]
    assert "fundamentals" in plan["skipped_reason"]
    assert "风险审查官" not in plan["skipped_reason"]


def test_normalize_fills_a_reason_for_every_plannable_agent():
    plan = normalize_plan({"enabled_agents": [], "rationale": "最小计划"})
    for key in PLANNABLE_AGENTS:
        assert plan["skipped_reason"][key]


def test_normalize_never_returns_an_empty_rationale():
    assert normalize_plan({"enabled_agents": []})["rationale"] == "未提供规划理由。"
