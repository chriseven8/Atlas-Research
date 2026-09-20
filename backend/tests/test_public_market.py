from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from financial_research.domain import ProviderError, ResearchRequest
from financial_research.public_market import PublicMarketProvider
from financial_research.workflow import ResearchWorkflow


@pytest.mark.parametrize(
    "code,canonical",
    [
        ("600519", "600519.SH"),
        ("sh600519", "600519.SH"),
        ("000001", "000001.SZ"),
        ("300750.sz", "300750.SZ"),
        ("688981", "688981.SH"),
        ("920992", "920992.BJ"),
    ],
)
def test_a_share_normalization(code, canonical):
    assert ResearchRequest(market="CN", symbol=code, mode="live").symbol == canonical


@pytest.mark.parametrize("code", ["600519.SZ", "SH000001", "AAPL", "900001", "200001", "../600519"])
def test_invalid_a_share_codes(code):
    with pytest.raises(ValidationError):
        ResearchRequest(market="CN", symbol=code, mode="live")


def fixture_transport(ticker, rows, cn=True):
    def respond(req):
        if "eastmoney.com" in req.url.host:
            return httpx.Response(503)
        if req.url.host == "qt.gtimg.cn":
            return httpx.Response(200, content='v_usAAPL="200~苹果~AAPL.OQ~101";'.encode("gbk"))
        assert req.url.params["param"].startswith(ticker + ",day,")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {ticker: {"day" if cn else "qfqday": rows, "qt": {ticker: ["1", "测试公司"]}}},
            },
        )

    return httpx.MockTransport(respond)


def sample_rows():
    return [[str(date(2026, 5, 1) + timedelta(days=i)), "100", "101", "102", "99", "500"] for i in range(45)]


@pytest.mark.parametrize(
    "market,symbol,ticker,currency,volume,adjustment",
    [
        ("CN", "000001", "sz000001", "CNY", 50000, "raw"),
        ("CN", "688981", "sh688981", "CNY", 500, "raw"),
        ("US", "AAPL", "usAAPL.OQ", "USD", 500, "qfq"),
    ],
)
def test_market_source_units_and_cutoff(settings, market, symbol, ticker, currency, volume, adjustment):
    rows = sample_rows()
    rows.append(rows[0])
    provider = PublicMarketProvider(settings, fixture_transport(ticker, rows, market == "CN"))
    result = provider.market(ResearchRequest(market=market, symbol=symbol, mode="live", as_of="2026-06-01"))
    assert result["bars"][-1]["date"] == "2026-06-01"
    assert len(result["bars"]) == 32
    assert result["bars"][-1]["volume"] == volume
    assert (result["currency"], result["adjustment"]) == (currency, adjustment)
    assert result["evidence"][0]["is_demo"] is False


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [["2026-06-01", "100", "101", "102", "99", "500"]],
        sample_rows() + [["2026-06-01", "100", "nan", "102", "99", "500"]],
    ],
)
def test_missing_or_invalid_market_fails(settings, rows):
    with pytest.raises(ProviderError):
        PublicMarketProvider(settings, fixture_transport("sz000001", rows)).market(
            ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01")
        )


@pytest.mark.parametrize(
    "market,symbol,ticker,zone,hour",
    [("CN", "000001", "sz000001", "Asia/Shanghai", 14), ("US", "AAPL", "usAAPL.OQ", "America/New_York", 15)],
)
def test_intraday_bar_is_excluded(settings, monkeypatch, market, symbol, ticker, zone, hour):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 1, hour, tzinfo=ZoneInfo(zone))

    monkeypatch.setattr("financial_research.public_market.datetime", Clock)
    result = PublicMarketProvider(settings, fixture_transport(ticker, sample_rows(), market == "CN")).market(
        ResearchRequest(market=market, symbol=symbol, mode="live", as_of="2026-06-01")
    )
    assert result["bars"][-1]["date"] == "2026-05-31"


def test_cn_full_workflow_preserves_currency_and_missing_feeds(repo, settings):
    req = ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01")
    repo.create(req)
    job = repo.claim()
    provider = PublicMarketProvider(settings, fixture_transport("sz000001", sample_rows()))
    report = ResearchWorkflow(settings, repo, job, provider).run()["report"]
    assert report["status"] == "partial"
    assert report["currency"] == "CNY"
    assert "CNY" in report["claims"][0]["text"]
    assert "USD" not in report["claims"][0]["text"]
    assert report["news"] == []
    assert "中国宏观" in " ".join(report["limitations"])
    assert all(not e["is_demo"] for e in report["evidence"])
