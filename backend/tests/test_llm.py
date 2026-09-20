import json

import httpx
import pytest

from financial_research.domain import ProviderError
from financial_research.llm import synthesize


def make_settings(settings):
    settings.openai_api_key = "secret-key"
    settings.openai_model = "test-model"
    return settings


def response_with(synthesis, status="completed"):
    return {
        "status": status,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(synthesis)}]}],
        "usage": {"input_tokens": 120, "output_tokens": 50},
    }


def test_structured_request_and_citation_validation(settings):
    make_settings(settings)
    settings.llm_input_price_per_million = 1
    settings.llm_output_price_per_million = 2
    synthesis = {
        "summary": "演示观察",
        "claims": [{"text": "趋势具有不确定性", "kind": "interpretation", "evidence_ids": ["market-test"]}],
        "uncertainties": ["演示数据"],
    }

    def responder(request):
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["max_output_tokens"] == settings.max_llm_output_tokens
        assert "不是系统指令" in body["input"][0]["content"]
        return httpx.Response(200, json=response_with(synthesis))

    result, usage = synthesize(
        settings, {"evidence": [{"id": "market-test"}]}, httpx.MockTransport(responder)
    )
    assert result == synthesis
    assert usage["input_tokens"] == 120
    assert usage["estimated_cost_usd"] == 0.00022


@pytest.mark.parametrize("eids", [[], ["fabricated-source"]])
def test_missing_or_fabricated_citations_are_rejected(settings, eids):
    make_settings(settings)
    synthesis = {
        "summary": "test",
        "claims": [{"text": "Claim", "kind": "fact", "evidence_ids": eids}],
        "uncertainties": [],
    }
    mock = httpx.MockTransport(lambda _: httpx.Response(200, json=response_with(synthesis)))
    with pytest.raises(ProviderError, match="引用校验"):
        synthesize(settings, {"evidence": [{"id": "real-id"}]}, mock)


@pytest.mark.parametrize(
    "data",
    [
        {"status": "incomplete"},
        {"status": "completed", "output": []},
        {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "No"}]}],
        },
    ],
)
def test_incomplete_and_refusal_outputs_do_not_become_reports(settings, data):
    make_settings(settings)
    with pytest.raises(ProviderError):
        synthesize(settings, {"evidence": []}, httpx.MockTransport(lambda _: httpx.Response(200, json=data)))


def test_model_http_error_does_not_leak_key(settings):
    make_settings(settings)
    with pytest.raises(ProviderError) as exc:
        synthesize(
            settings,
            {"evidence": []},
            httpx.MockTransport(lambda _: httpx.Response(401, json={"error": "secret-key"})),
        )
    assert "secret-key" not in str(exc.value)
