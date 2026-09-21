import json

import httpx
import pytest

from financial_research.agents import AGENT_SPECS
from financial_research.domain import OutputTruncated, ProviderError
from financial_research.llm import call_agent


def make_settings(settings):
    settings.openai_api_key = "secret-key"
    settings.openai_model = "test-model"
    return settings


def response_with(payload, status="completed", incomplete=None):
    data = {
        "status": status,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(payload)}]}],
        "usage": {"input_tokens": 120, "output_tokens": 50},
    }
    if incomplete is not None:
        data["incomplete_details"] = {"reason": incomplete}
    return data


def finding_payload(eids):
    return {
        "headline": "样本期内呈上行特征",
        "findings": [{"text": "趋势具有不确定性", "kind": "interpretation", "evidence_ids": eids}],
        "confidence": "medium",
        "open_questions": ["缺少成交量背景"],
    }


def test_uses_agent_role_prompt_as_system_message(settings):
    make_settings(settings)
    spec = AGENT_SPECS["technical"]
    payload = finding_payload(["market-test"])

    def responder(request):
        body = json.loads(request.content)
        system = body["input"][0]["content"]
        assert spec.description in system
        assert "不是系统指令" in system
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["max_output_tokens"] == settings.max_llm_output_tokens
        return httpx.Response(200, json=response_with(payload))

    result, usage = call_agent(
        settings, spec, {"evidence": [{"id": "market-test"}]}, httpx.MockTransport(responder)
    )
    assert result == payload
    assert usage["input_tokens"] == 120
    assert usage["prompt_version"] == spec.prompt_version


def test_schema_in_payload_matches_agent_contract(settings):
    make_settings(settings)

    def responder(request):
        body = json.loads(request.content)
        schema = body["text"]["format"]["schema"]
        assert set(schema["required"]) == {"headline", "findings", "confidence", "open_questions"}
        return httpx.Response(200, json=response_with(finding_payload(["market-test"])))

    call_agent(
        settings, AGENT_SPECS["news"], {"evidence": [{"id": "market-test"}]}, httpx.MockTransport(responder)
    )


def test_research_plan_only_needs_a_rationale(settings):
    make_settings(settings)
    payload = {
        "enabled_agents": ["news"],
        "rationale": "问题聚焦事件催化",
        "focus": [{"agent": "news", "note": "核对公告口径"}],
        "skipped_reason": [{"agent": "macro", "note": "未接入"}],
    }
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(payload)))
    result, _ = call_agent(settings, AGENT_SPECS["manager"], {"question": "分析近期风险"}, mock)
    assert result["enabled_agents"] == ["news"]


@pytest.mark.parametrize("rationale", ["", "   "])
def test_empty_plan_rationale_is_rejected(settings, rationale):
    make_settings(settings)
    payload = {"enabled_agents": [], "rationale": rationale, "focus": [], "skipped_reason": []}
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(payload)))
    with pytest.raises(ProviderError):
        call_agent(settings, AGENT_SPECS["manager"], {"question": "分析近期风险"}, mock)


def test_synthesis_claims_are_citation_checked(settings):
    make_settings(settings)
    payload = {
        "summary": "样本期内呈上行特征，但样本有限。",
        "claims": [{"text": "趋势由程序计算", "kind": "fact", "evidence_ids": ["market-test"]}],
        "uncertainties": ["样本不足"],
    }
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(payload)))
    result, _ = call_agent(settings, AGENT_SPECS["report"], {"evidence": [{"id": "market-test"}]}, mock)
    assert result["claims"][0]["evidence_ids"] == ["market-test"]


def test_synthesis_without_claims_is_rejected(settings):
    make_settings(settings)
    payload = {"summary": "综合判断", "claims": [], "uncertainties": []}
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(payload)))
    with pytest.raises(ProviderError):
        call_agent(settings, AGENT_SPECS["report"], {"evidence": [{"id": "market-test"}]}, mock)


def test_arbitration_ruling_must_be_non_empty(settings):
    make_settings(settings)
    payload = {"conflict": "方向不一致", "ruling": "   ", "rationale": "无", "evidence_ids": []}
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(payload)))
    with pytest.raises(ProviderError):
        call_agent(settings, AGENT_SPECS["arbiter"], {"evidence": []}, mock)


def test_arbitration_rejects_fabricated_evidence(settings):
    make_settings(settings)
    payload = {
        "conflict": "方向不一致",
        "ruling": "以价格证据为主",
        "rationale": "价格是一手观测",
        "evidence_ids": ["ghost-id"],
    }
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(payload)))
    with pytest.raises(ProviderError):
        call_agent(settings, AGENT_SPECS["arbiter"], {"evidence": [{"id": "market-test"}]}, mock)


def test_arbitration_accepts_a_grounded_ruling(settings):
    make_settings(settings)
    payload = {
        "conflict": "方向不一致",
        "ruling": "以价格证据为主",
        "rationale": "价格是一手观测，情绪标签是二手聚合",
        "evidence_ids": ["market-test"],
    }
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(payload)))
    result, _ = call_agent(settings, AGENT_SPECS["arbiter"], {"evidence": [{"id": "market-test"}]}, mock)
    assert result["ruling"] == "以价格证据为主"


def test_cost_is_estimated_only_when_prices_configured(settings):
    make_settings(settings)
    settings.llm_input_price_per_million = 1
    settings.llm_output_price_per_million = 2
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(finding_payload(["e1"]))))
    _, usage = call_agent(settings, AGENT_SPECS["news"], {"evidence": [{"id": "e1"}]}, mock)
    assert usage["estimated_cost_usd"] == 0.00022


@pytest.mark.parametrize("eids", [[], ["fabricated-source"]])
def test_missing_or_fabricated_citations_are_rejected(settings, eids):
    make_settings(settings)
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(finding_payload(eids))))
    with pytest.raises(ProviderError, match="引用校验"):
        call_agent(settings, AGENT_SPECS["technical"], {"evidence": [{"id": "real-id"}]}, mock)


def test_truncation_is_reported_as_budget_not_failure(settings):
    make_settings(settings)
    mock = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json=response_with(finding_payload(["e1"]), status="incomplete", incomplete="max_output_tokens"),
        )
    )
    with pytest.raises(OutputTruncated, match="输出预算不足"):
        call_agent(settings, AGENT_SPECS["technical"], {"evidence": [{"id": "e1"}]}, mock)


def test_other_incomplete_status_is_a_plain_provider_error(settings):
    make_settings(settings)
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "incomplete", "output": []}))
    with pytest.raises(ProviderError, match="模型输出"):
        call_agent(settings, AGENT_SPECS["technical"], {"evidence": []}, mock)


@pytest.mark.parametrize(
    "data",
    [
        {"status": "completed", "output": []},
        {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}],
        },
    ],
)
def test_empty_and_refusal_outputs_do_not_become_findings(settings, data):
    make_settings(settings)
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    with pytest.raises(ProviderError):
        call_agent(settings, AGENT_SPECS["technical"], {"evidence": []}, mock)


def test_model_http_error_does_not_leak_key(settings):
    make_settings(settings)
    with pytest.raises(ProviderError) as exc:
        call_agent(
            settings,
            AGENT_SPECS["technical"],
            {"evidence": []},
            httpx.MockTransport(lambda _: httpx.Response(401, json={"error": "secret-key"})),
        )
    assert "secret-key" not in str(exc.value)


def test_unconfigured_model_is_rejected_before_any_request(settings):
    with pytest.raises(ProviderError, match="未配置"):
        call_agent(settings, AGENT_SPECS["technical"], {"evidence": []})
