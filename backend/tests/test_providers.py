from datetime import date, timedelta

import httpx
import pytest

from financial_research.domain import ProviderError, ResearchRequest
from financial_research.providers import AlphaVantageProvider, DemoProvider, snapshot_hash


def transport(payload, code=200):
    return httpx.MockTransport(lambda req: httpx.Response(code, json=payload))


def test_demo_is_deterministic_and_honest():
    req = ResearchRequest(as_of="2026-06-10")
    a, b = DemoProvider().market(req), DemoProvider().market(req)
    assert a["bars"] == b["bars"]
    assert len(a["bars"]) == 90
    assert max(x["date"] for x in a["bars"]) <= "2026-06-10"
    assert a["evidence"][0]["is_demo"]
    assert a["evidence"][0]["snapshot_hash"] == snapshot_hash(a["bars"])
    assert all(n["url"] is None for n in DemoProvider().news(req)["items"])


def test_demo_can_supply_the_full_lookback_upper_bound():
    """日期生成窗口必须够宽：300 个交易日约合 430 个自然日，窗口窄了就会静默少给。"""
    bars = DemoProvider().market(ResearchRequest(as_of="2026-06-10", lookback_days=300))["bars"]
    assert len(bars) == 300
    assert max(x["date"] for x in bars) <= "2026-06-10"
    # 取的是序列末端的最近 N 条，放宽窗口不应改变最近日期的取值
    assert bars[-1] == DemoProvider().market(ResearchRequest(as_of="2026-06-10"))["bars"][-1]


@pytest.mark.parametrize(
    "data", [{"Information": "secret quota"}, {"Note": "secret rate"}, {"Error Message": "secret bad key"}]
)
def test_provider_errors_are_sanitized(settings, data):
    settings.alpha_vantage_api_key = "do-not-leak"
    provider = AlphaVantageProvider(settings, transport(data))
    with pytest.raises(ProviderError) as err:
        provider.market(ResearchRequest(mode="live"))
    assert "secret" not in str(err.value) and "do-not-leak" not in str(err.value)


def test_live_failure_never_substitutes_demo(settings):
    settings.alpha_vantage_api_key = "test"
    with pytest.raises(ProviderError):
        AlphaVantageProvider(settings, transport({}, 500)).market(ResearchRequest(mode="live"))


def test_news_cutoff_dedup_and_invalid_urls(settings):
    settings.alpha_vantage_api_key = "test"
    item = {
        "title": "Event",
        "time_published": "20260610T120000",
        "url": "https://example.com/a",
        "summary": "Observed",
    }
    feed = [
        item,
        item,
        {**item, "url": "https://example.com/b"},
        {**item, "title": "Future", "time_published": "20260612T120000", "url": "https://example.com/future"},
        {**item, "title": "Unsafe", "url": "javascript:alert(1)"},
    ]
    data = AlphaVantageProvider(settings, transport({"feed": feed})).news(
        ResearchRequest(mode="live", as_of="2026-06-10")
    )
    assert len(data["items"]) == 1
    assert data["items"][0]["title"] == "Event"
    assert not data["evidence"][0]["is_demo"]


def test_historical_macro_is_skipped_without_a_network_call(settings):
    def unexpected(_):
        raise AssertionError("Network must not be called")

    provider = AlphaVantageProvider(settings, httpx.MockTransport(unexpected))
    with pytest.raises(ProviderError, match="前视偏差"):
        provider.macro(ResearchRequest(mode="live", as_of=date.today() - timedelta(days=3)))


def test_market_filters_future_data_and_checks_quality(settings):
    settings.alpha_vantage_api_key = "test"
    series = {
        str(date(2026, 5, 1) + timedelta(days=i)): {
            "1. open": "100",
            "2. high": "102",
            "3. low": "99",
            "4. close": "101",
            "5. volume": "500",
        }
        for i in range(45)
    }
    provider = AlphaVantageProvider(settings, transport({"Time Series (Daily)": series}))
    data = provider.market(ResearchRequest(mode="live", as_of="2026-06-01"))
    assert data["bars"][-1]["date"] == "2026-06-01"
    assert data["adjustment"] == "raw"
    assert "未复权" in data["warnings"][0]
    series["2026-05-02"]["4. close"] = "nan"
    with pytest.raises(ProviderError, match="质量"):
        provider.market(ResearchRequest(mode="live", as_of="2026-06-01"))


@pytest.mark.parametrize("lookback,expected", [(90, "compact"), (100, "compact"), (300, "full")])
def test_market_asks_for_the_range_it_needs(settings, lookback, expected):
    """compact 只回 100 个交易日；长区间仍用 compact 会被上游静默截断成 100 条。"""
    settings.alpha_vantage_api_key = "test"
    seen = {}

    def respond(req):
        seen["outputsize"] = req.url.params["outputsize"]
        return httpx.Response(200, json={"Time Series (Daily)": {}})

    with pytest.raises(ProviderError):
        AlphaVantageProvider(settings, httpx.MockTransport(respond)).market(
            ResearchRequest(mode="live", as_of="2026-06-01", lookback_days=lookback)
        )
    assert seen["outputsize"] == expected
