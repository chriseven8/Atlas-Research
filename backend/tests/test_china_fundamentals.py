import httpx
import pytest

from financial_research.china_fundamentals import fetch_china_fundamentals
from financial_research.domain import ProviderError, ResearchRequest


def row(
    report_date="2026-06-30",
    report_name="2026中报",
    notice_date="2026-08-22",
    **overrides,
):
    base = {
        "REPORT_DATE": f"{report_date} 00:00:00",
        "REPORT_DATE_NAME": report_name,
        "REPORT_TYPE": report_name[-2:],
        "NOTICE_DATE": f"{notice_date} 00:00:00",
        "CURRENCY": "CNY",
        "TOTALOPERATEREVE": 41777861795.03,
        "TOTALOPERATEREVETZ": 182.491381328657,
        "MLR": 19323173654.59,
        "XSMLL": 46.2521843492,
        "XSJLL": 35.26611016,
        "PARENTNETPROFIT": 13651149693.27,
        "PARENTNETPROFITTZ": 241.696005716117,
        "KCFJCXSYJLR": 13091597476.79,
        "KCFJCXSYJLRTZ": 229.316750212959,
        "ROEJQ": 37.62,
        "ROEKCJQ": 36.08,
        "NETCASH_OPERATE_PK": 1799674832.02,
        "TOTAL_ASSETS_PK": 68942203736.77,
        "LIABILITY": 24875450361.61,
        "TOTAL_EQUITY_PK": 44066753375.16,
        "ZCFZL": 36.0816002584,
        "INTEREST_COVERAGE_RATIO": 56.875232908671,
        "EPSJB": 12.31,
        "BPS": 35.740605779103,
    }
    return {**base, **overrides}


def transport(rows, seen=None):
    def respond(req):
        if seen is not None:
            seen["filter"] = req.url.params["filter"]
            seen["columns"] = req.url.params["columns"]
        return httpx.Response(200, json={"success": True, "result": {"count": len(rows), "data": rows}})

    return httpx.MockTransport(respond)


def fetch(settings, rows, as_of="2026-09-23", seen=None, symbol="300308.SZ"):
    return fetch_china_fundamentals(
        ResearchRequest(market="CN", symbol=symbol, mode="live", as_of=as_of),
        settings,
        transport(rows, seen),
    )


def test_point_in_time_cutoff_uses_notice_date_not_report_date(settings):
    """2026 中报报告期末是 6-30，公告日却是 8-22。

    按报告期末收口会把 8 月下旬才公开的数据拿去解释 6-7 月的行情；按公告日收口才对。
    """
    rows = [row(), row(report_date="2026-03-31", report_name="2026一季报", notice_date="2026-04-17")]
    seen = {}
    # 截止 2026-07-01：中报（公告 8-22）当时尚未公布，只有一季报可得
    out = fetch(settings, rows, as_of="2026-07-01", seen=seen)
    assert [p["report_name"] for p in out["items"]] == ["2026一季报"]
    assert out["latest"]["notice_date"] == "2026-04-17"
    assert "2026-07-01" in seen["filter"]


def test_reported_values_are_passed_through_without_estimation(settings):
    out = fetch(settings, [row()])
    latest = out["latest"]
    assert latest["revenue"] == 41777861795.03
    assert latest["revenue_yoy_pct"] == 182.49
    assert latest["gross_margin_pct"] == 46.25
    assert latest["net_profit_yoy_pct"] == 241.7
    assert latest["roe_weighted_pct"] == 37.62
    assert latest["debt_ratio_pct"] == 36.08
    assert latest["interest_coverage"] == 56.88
    # 派生比率由程序算，不是上游字段
    assert latest["cash_to_profit_pct"] == round(1799674832.02 / 13651149693.27 * 100, 2)


def test_missing_values_stay_none_instead_of_becoming_zero(settings):
    """上游把没有的项目写成 null；报 0 会被读成「这一项真的等于零」。"""
    rows = [
        row(
            ROEJQ=None,
            MLR=None,
            XSMLL="--",
            LIABILITY=None,
            TOTAL_EQUITY_PK=None,
        )
    ]
    latest = fetch(settings, rows)["latest"]
    assert latest["roe_weighted_pct"] is None
    assert latest["gross_profit"] is None
    assert latest["gross_margin_pct"] is None
    assert latest["liability"] is None
    assert latest["total_equity"] is None


def test_derived_ratio_is_unknown_when_the_denominator_is_unusable(settings):
    assert fetch(settings, [row(PARENTNETPROFIT=0)])["latest"]["cash_to_profit_pct"] is None
    assert fetch(settings, [row(NETCASH_OPERATE_PK=None)])["latest"]["cash_to_profit_pct"] is None


def test_broken_dates_are_dropped_not_guessed(settings):
    rows = [row(), row(report_date="2026-03-31", report_name="2026一季报", notice_date="not-a-date")]
    assert [p["report_name"] for p in fetch(settings, rows)["items"]] == ["2026中报"]


def test_latest_notice_wins_when_a_period_is_restated(settings):
    rows = [
        row(REPORT_DATE_NAME="2026中报", NOTICE_DATE="2026-08-22 00:00:00", PARENTNETPROFIT=1.0e10),
        row(REPORT_DATE_NAME="2026中报（更正）", NOTICE_DATE="2026-09-01 00:00:00", PARENTNETPROFIT=9.0e9),
    ]
    out = fetch(settings, rows)
    assert len(out["items"]) == 1
    assert out["latest"]["notice_date"] == "2026-09-01"
    assert out["latest"]["net_profit"] == 9.0e9


def test_periods_are_capped_but_ordered_by_notice_date(settings):
    quarters = [(1, 2), (4, 5), (7, 8), (10, 11)]
    rows = [
        row(
            report_date=f"{2022 + i // 4}-{quarters[i % 4][0]:02d}-01",
            report_name=f"第{i}期",
            notice_date=f"{2022 + i // 4}-{quarters[i % 4][1]:02d}-15",
        )
        for i in range(14)
    ]
    out = fetch(settings, rows)
    assert len(out["items"]) == 8
    notices = [p["notice_date"] for p in out["items"]]
    assert notices == sorted(notices, reverse=True)


def test_no_records_raises_instead_of_reporting_an_empty_shell(settings):
    with pytest.raises(ProviderError, match="无可用报告期记录"):
        fetch(settings, [])


def test_evidence_is_a_fundamental_and_its_note_states_the_actual_coverage(settings):
    rows = [row(), row(report_date="2026-03-31", report_name="2026一季报", notice_date="2026-04-17")]
    out = fetch(settings, rows)
    ev = out["evidence"][0]
    assert ev["kind"] == "fundamental"
    assert ev["is_demo"] is False
    # note 要写清单实际覆盖的范围与最新一期的关键数字，而不是请求窗口
    assert "2026一季报" in ev["note"] and "2026中报" in ev["note"]
    assert "417.78 亿元" in ev["note"]
    assert "报告期累计值" in ev["note"]
    assert "NOTICE_DATE <= 2026-09-23" in ev["note"]


def test_failed_payload_raises_a_public_safe_error(settings):
    def respond(_):
        return httpx.Response(200, json={"success": False, "message": "boom"})

    with pytest.raises(ProviderError, match="数据源返回失败"):
        fetch_china_fundamentals(
            ResearchRequest(market="CN", symbol="300308.SZ", mode="live", as_of="2026-09-23"),
            settings,
            httpx.MockTransport(respond),
        )
