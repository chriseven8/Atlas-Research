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


def fixture_transport(ticker, rows, cn=True, second=None, adjusted=None):
    """second 为第二行情源（新浪）的应答；None 表示该源本轮不可用。

    adjusted 为前复权序列；不传则与未复权相同，即区间内没有公司行动。
    """

    def respond(req):
        if "eastmoney.com" in req.url.host:
            return httpx.Response(503)
        if req.url.host == "qt.gtimg.cn":
            return httpx.Response(200, content='v_usAAPL="200~苹果~AAPL.OQ~101";'.encode("gbk"))
        if "sina.com.cn" in req.url.host:
            if second is None:
                return httpx.Response(503)
            # 新浪美股走 JSONP 文本、A 股走 JSON 数组，两者都不能解析成空。
            return (
                httpx.Response(200, text=second)
                if isinstance(second, str)
                else httpx.Response(200, json=second)
            )
        param = req.url.params["param"]
        assert param.startswith(ticker + ",day,")
        # 末位空为未复权、'qfq' 为前复权；两者要放在各自的键下，否则「复权序列取不到」
        # 这条降级路径会被同一份应答掩盖。
        forward = param.endswith(",qfq")
        key = "qfqday" if forward or not cn else "day"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    ticker: {
                        key: (adjusted if forward and adjusted is not None else rows),
                        "qt": {ticker: ["1", "测试公司"]},
                    }
                },
            },
        )

    return httpx.MockTransport(respond)


def sample_rows():
    return [[str(date(2026, 5, 1) + timedelta(days=i)), "100", "101", "102", "99", "500"] for i in range(45)]


def sina_rows(days=("2026-05-25", "2026-05-26", "2026-05-27")):
    return [
        {"day": day, "open": "100", "high": "102", "low": "99", "close": "101", "volume": "50000"}
        for day in days
    ]


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


@pytest.mark.parametrize("lookback,min_days", [(90, 400), (300, 430)])
def test_fetch_window_is_wide_enough_for_the_requested_bars(settings, lookback, min_days):
    """取数窗口窄于所需自然日数时，上游不会报错，只会少给——必须在请求侧挡住。"""
    seen = {}

    def respond(req):
        if "sina.com.cn" in req.url.host or "eastmoney.com" in req.url.host:
            return httpx.Response(503)
        param = req.url.params["param"]
        if param.endswith(",qfq"):
            return httpx.Response(200, json={"code": 0, "data": {"sz000001": {}}})
        seen["start"] = param.split(",")[2]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"sz000001": {"day": sample_rows(), "qt": {"sz000001": ["1", "测试公司"]}}},
            },
        )

    provider = PublicMarketProvider(settings, httpx.MockTransport(respond))
    provider.market(
        ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01", lookback_days=lookback)
    )
    span = (date(2026, 6, 1) - date.fromisoformat(seen["start"])).days
    assert span >= min_days


def test_corporate_actions_come_from_the_inline_ex_date_metadata(settings):
    """腾讯把除权信息内嵌在生效日那一行末尾，与日线同源同区间，不必再引公告接口。"""
    rows = sample_rows()
    rows[5] = [
        *rows[5],
        {"nd": "2025", "fh_sh": "10", "djr": "2026-05-05", "cqr": "2026-05-06", "FHcontent": "10派10元"},
    ]
    result = PublicMarketProvider(settings, fixture_transport("sz000001", rows)).market(
        ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01")
    )
    assert result["corporate_actions"] == [
        {"ex_date": "2026-05-06", "record_date": "2026-05-05", "plan": "10派10元", "fiscal_year": "2025"}
    ]
    assert result["action_attribution"]["adjusted_series_available"] is True


def test_missing_adjusted_series_degrades_attribution_instead_of_failing(settings):
    """前复权对照序列取不到时，主序列照常出报告，归因如实降级为不可得。"""
    rows = [[str(date(2026, 5, 1) + timedelta(days=i)), "100", "101", "102", "99", "500"] for i in range(45)]

    def respond(req):
        if "sina.com.cn" in req.url.host or "eastmoney.com" in req.url.host:
            return httpx.Response(503)
        param = req.url.params["param"]
        if param.endswith(",qfq"):
            return httpx.Response(200, json={"code": 0, "data": {"sz000001": {}}})
        return httpx.Response(
            200,
            json={"code": 0, "data": {"sz000001": {"day": rows, "qt": {"sz000001": ["1", "测试公司"]}}}},
        )

    result = PublicMarketProvider(settings, httpx.MockTransport(respond)).market(
        ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01")
    )
    assert result["action_attribution"]["adjusted_series_available"] is False
    assert any("归因降级为不可得" in w for w in result["warnings"])


def test_misaligned_adjusted_series_is_discarded(settings):
    """复权序列与主序列日期不一致时必须丢弃。

    留着它算差值，会把「少了一天」误报成一次公司行动。
    """
    rows = sample_rows()
    # 去掉区间内的一天（2026-05-11），而不是区间外的那几天——区间外的日期本来就会被
    # 截断丢掉，用它做用例根本走不到「日期对不上」这个分支。
    adjusted = [row for index, row in enumerate(sample_rows()) if index != 10]
    result = PublicMarketProvider(settings, fixture_transport("sz000001", rows, adjusted=adjusted)).market(
        ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01")
    )
    assert result["action_attribution"]["adjusted_series_available"] is False


def test_consistent_second_source_is_recorded(settings):
    provider = PublicMarketProvider(
        settings, fixture_transport("sz000001", sample_rows(), second=sina_rows())
    )
    result = provider.market(ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01"))
    cross = result["cross_check"]
    assert cross["status"] == "consistent"
    assert cross["compared_days"] == 3
    assert cross["date_range"] == ["2026-05-25", "2026-05-27"]
    assert cross["close_ratio_median"] == 1.0
    assert not any("交叉校验" in w for w in result["warnings"])


def test_disagreeing_second_source_is_flagged(settings):
    second = sina_rows()
    second[-1]["close"] = "200"
    provider = PublicMarketProvider(settings, fixture_transport("sz000001", sample_rows(), second=second))
    result = provider.market(ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01"))
    assert result["cross_check"]["status"] == "mismatch"
    assert result["cross_check"]["close_mismatches"][0]["date"] == "2026-05-27"
    assert any("交叉校验发现不一致" in w for w in result["warnings"])


def test_tiny_volume_difference_is_not_a_mismatch(settings):
    """实测两源成交量差几十股（万分之几），不能被报成不一致。"""
    second = sina_rows()
    second[0]["volume"] = "50010"
    provider = PublicMarketProvider(settings, fixture_transport("sz000001", sample_rows(), second=second))
    result = provider.market(ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01"))
    assert result["cross_check"]["status"] == "consistent"


def test_unavailable_second_source_is_marked_not_silently_passed(settings):
    """「没查出问题」与「没去查」必须能区分。"""
    provider = PublicMarketProvider(settings, fixture_transport("sz000001", sample_rows()))
    result = provider.market(ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01"))
    assert result["cross_check"]["status"] == "unavailable"
    assert any("未完成第二行情源交叉校验" in w for w in result["warnings"])


def evidence_titled(result, needle):
    hits = [e for e in result["evidence"] if needle in e["title"]]
    assert len(hits) == 1, [e["title"] for e in result["evidence"]]
    return hits[0]


def test_attribution_and_cross_check_are_citable_on_their_own(settings):
    """只写在市场角色的上下文里时，模型拿不到 evidence_id，只能当成「未提供的数据」。

    所以归因与交叉校验必须各占一条证据：标题可检索、note 自解释、id 与行情主证据不重。
    """
    rows = sample_rows()
    rows[5] = [
        *rows[5],
        {"nd": "2025", "fh_sh": "10", "djr": "2026-05-05", "cqr": "2026-05-06", "FHcontent": "10派10元"},
    ]
    provider = PublicMarketProvider(settings, fixture_transport("sz000001", rows, second=sina_rows()))
    result = provider.market(ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01"))

    bars_ev = evidence_titled(result, "真实日线")
    attribution_ev = evidence_titled(result, "除权归因")
    cross_ev = evidence_titled(result, "第二行情源交叉校验")
    assert len({bars_ev["id"], attribution_ev["id"], cross_ev["id"]}) == 3
    assert {e["kind"] for e in result["evidence"]} == {"market"}
    assert all(e["is_demo"] is False for e in result["evidence"])
    # note 必须自带结论，模型不该为了知道「降级了没」去翻 payload。
    assert "2026-05-06 实测影响" in attribution_ev["note"]
    assert "10派10元" in attribution_ev["note"]
    assert "3 个重叠交易日" in cross_ev["note"]
    assert cross_ev["observed_at"] == "2026-05-27"


def test_degraded_attribution_evidence_says_it_is_unavailable(settings):
    """对照序列取不到时仍要留证据：否则「归因不可得」与「没做归因」在报告里长得一样。"""

    def respond(req):
        if "sina.com.cn" in req.url.host or "eastmoney.com" in req.url.host:
            return httpx.Response(503)
        param = req.url.params["param"]
        if param.endswith(",qfq"):
            return httpx.Response(200, json={"code": 0, "data": {"sz000001": {}}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"sz000001": {"day": sample_rows(), "qt": {"sz000001": ["1", "测试公司"]}}},
            },
        )

    result = PublicMarketProvider(settings, httpx.MockTransport(respond)).market(
        ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01")
    )
    attribution_ev = evidence_titled(result, "除权归因")
    assert "降级为不可得" in attribution_ev["note"]
    assert "未按公告比例反推" in attribution_ev["note"]


def test_unavailable_cross_check_evidence_does_not_claim_it_passed(settings):
    provider = PublicMarketProvider(settings, fixture_transport("sz000001", sample_rows()))
    result = provider.market(ResearchRequest(market="CN", symbol="000001", mode="live", as_of="2026-06-01"))
    note = evidence_titled(result, "第二行情源交叉校验")["note"]
    assert "未完成第二行情源交叉校验" in note
    assert "不等于已校验通过" in note
    assert "未发现超出容差的差异" not in note


def test_us_run_has_no_attribution_evidence(settings):
    """美股主序列本身就是前复权，除权归因没有适用对象，不该留一条永久空证据。"""
    result = PublicMarketProvider(settings, fixture_transport("usAAPL.OQ", sample_rows(), cn=False)).market(
        ResearchRequest(market="US", symbol="AAPL", mode="live", as_of="2026-06-01")
    )
    titles = [e["title"] for e in result["evidence"]]
    assert len(titles) == 2
    assert not any("除权归因" in title for title in titles)
    assert any("第二行情源交叉校验" in title for title in titles)


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
    # 新闻与宏观都被计划启用（A 股两个源都无需密钥），但本用例的上游全部 503：
    # 报告必须如实标记为部分完成并说明是哪一路失败，而不是回退到 4.25% 的演示利率。
    limitations = " ".join(report["limitations"])
    assert "中国宏观数据源" in limitations
    assert report["macro"]["items"] == [] and report["coverage"]["macro"] is False
    assert "4.25" not in limitations
    assert all(not e["is_demo"] for e in report["evidence"])
    # 归因与交叉校验的证据要真的进报告（也就是进了引用白名单），
    # 停在 provider 的返回值里没用——模型引不到就等于没提供。
    titles = [e["title"] for e in report["evidence"]]
    assert any("真实日线" in t for t in titles)
    assert any("除权归因" in t for t in titles)
    assert any("第二行情源交叉校验" in t for t in titles)
