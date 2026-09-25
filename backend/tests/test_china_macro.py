from datetime import date

import httpx
import pytest

from financial_research.china_macro import (
    INDICATORS,
    MAX_ITEMS,
    TENOR_FIELD,
    _change_marks,
    _monthly_marks,
    _observations,
    fetch_china_macro,
)
from financial_research.domain import ProviderError, ResearchRequest

CPI_NAME = "中国 CPI 同比（全国）"


def payload(rows, success=True):
    return httpx.Response(200, json={"success": success, "result": {"data": rows, "count": len(rows)}})


def rows_for(days):
    """生成倒序的日频行，数值随日期单调变化，便于断言取到的是哪一天。"""
    return [
        {"SOLAR_DATE": f"2026-06-{day:02d} 00:00:00", TENOR_FIELD: 1.70 + day / 1000}
        for day in sorted(days, reverse=True)
    ]


def monthly(name, value_field, rows):
    """按 reportName 分派应答：只有被请求的那张报表才回数据，其余回空。

    真实数据源对每张报表各自应答，一个「不管问什么都回同一份」的 mock 会让
    「指标各自独立降级」这件事无法验证。
    """

    def handler(request):
        report = request.url.params["reportName"]
        return payload(rows if report == name else [], success=report == name)

    return handler


def only_tenor(response):
    """除国债收益率外一律返回同一份应答，用于整体失败场景。"""
    return lambda request: response


def test_filters_by_as_of_and_samples_monthly(settings):
    seen = {}

    def handler(r):
        report = r.url.params["reportName"]
        if report != "RPTA_WEB_TREASURYYIELD":
            return payload([], success=False)
        seen["filter"] = r.url.params["filter"]
        # 截止日之后的数据源本来就会返回，这里故意混进来验证是本地过滤兜住的
        rows = rows_for(range(1, 31)) + [
            {"SOLAR_DATE": "2026-07-15 00:00:00", TENOR_FIELD: 9.99},
            {"SOLAR_DATE": "2026-05-31 00:00:00", TENOR_FIELD: 1.65},
            {"SOLAR_DATE": "2026-04-30 00:00:00", TENOR_FIELD: 1.64},
        ]
        return payload(rows)

    result = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    # 请求侧就把截止日下推到数据源，而不是拉回来再丢
    assert "SOLAR_DATE<='2026-06-10'" in seen["filter"]
    tenor = next(i for i in result["indicators"] if i["key"] == "cn_10y")
    assert [i["date"] for i in tenor["items"]] == ["2026-06-10", "2026-05-31", "2026-04-30"]
    assert tenor["items"][0]["name"] == "中国 10 年期国债到期收益率"
    assert tenor["items"][0]["unit"] == "%"
    # 其它指标在本应答下取不到，须降级为 warning 而不是让整轮宏观失败
    assert result["complete"] is False
    assert any("CPI" in w for w in result["warnings"])


def test_evidence_is_a_real_snapshot_not_demo(settings):
    def handler(r):
        report = r.url.params["reportName"]
        return payload(rows_for(range(1, 11)) if report == "RPTA_WEB_TREASURYYIELD" else [], success=True)

    result = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    assert len(result["evidence"]) == 1
    ev = result["evidence"][0]
    assert ev["kind"] == "macro" and ev["is_demo"] is False
    assert ev["observed_at"] == "2026-06-10"
    assert ev["snapshot_hash"] and ev["id"] == f"macro-{ev['snapshot_hash'][:12]}"
    # 同样的观测必须得到同样的快照哈希：否则「可追溯性」只是每跑一次换个编号
    again = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    assert again["evidence"][0]["snapshot_hash"] == ev["snapshot_hash"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, content=b"<html>not json</html>"),
        payload([], success=False),
        httpx.Response(200, json={"success": True, "result": {"data": "nope"}}),
    ],
    ids=["http-error", "bad-json", "success-false", "result-not-list"],
)
def test_upstream_failures_raise_instead_of_faking_rates(settings, response):
    """全部指标都取不到时必须整体失败：一份「宏观缺失」的报告好过一份编造利率的报告。"""
    req = ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10")
    with pytest.raises(ProviderError):
        fetch_china_macro(req, settings, httpx.MockTransport(only_tenor(response)))


def test_one_failing_indicator_degrades_only_itself(settings):
    """单项限流不能让整轮宏观失败，否则 PPI 掉线会被读成「利率环境不可用」。"""

    def handler(r):
        if r.url.params["reportName"] == "RPT_ECONOMY_CPI":
            return httpx.Response(503)
        return payload(
            rows_for(range(1, 11)) if r.url.params["reportName"] == "RPTA_WEB_TREASURYYIELD" else []
        )

    result = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    assert [i["key"] for i in result["indicators"]] == ["cn_10y"]
    assert any(CPI_NAME in w and "不可用" in w for w in result["warnings"])


def test_no_observation_before_as_of_is_an_error(settings):
    def handler(r):
        # 全部落在截止日之后 → 过滤后一条不剩，不能拿未来的利率充当历史
        return payload([{"SOLAR_DATE": "2026-08-01 00:00:00", TENOR_FIELD: 1.8}])

    with pytest.raises(ProviderError, match="无有效宏观观测值"):
        fetch_china_macro(
            ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
            settings,
            httpx.MockTransport(handler),
        )


def test_statistical_indicators_are_lagged_to_avoid_look_ahead(settings):
    """CPI 的 REPORT_DATE 是统计期起点，实际发布要晚一个多月。

    as_of 2026-06-10 时，2026-05-01 那期（5 月 CPI，6 月中旬才发布）绝不能被放进来；
    只有 4 月及以前的期次当时已经公布。
    """
    seen = {}

    def handler(r):
        if r.url.params["reportName"] != "RPT_ECONOMY_CPI":
            return payload([], success=True)
        seen["filter"] = r.url.params["filter"]
        return payload(
            [
                {"REPORT_DATE": "2026-05-01 00:00:00", "NATIONAL_SAME": 0.1},
                {"REPORT_DATE": "2026-04-01 00:00:00", "NATIONAL_SAME": 0.5},
                {"REPORT_DATE": "2026-03-01 00:00:00", "NATIONAL_SAME": 0.7},
            ]
        )

    result = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    # 请求侧就按时滞收口，而不是把未公布的期次拉回来再丢
    assert "REPORT_DATE<='2026-04-26'" in seen["filter"]
    assert "REPORT_DATE<='2026-06-10'" not in seen["filter"]
    cpi = next(i for i in result["indicators"] if i["key"] == "cn_cpi")
    assert [i["date"] for i in cpi["items"]] == ["2026-04-01", "2026-03-01"]
    assert cpi["stats"]["n"] == 2


def test_note_reports_the_actual_coverage_not_the_requested_window(settings):
    """note 必须写清单实际覆盖的范围。

    旧版本写「取 {start} 至 {as_of} 的观测」而清单只到最近 12 个月，模型据此质疑
    「note 说从更早的日期起、清单却从更晚的日期起」——那是文案与数据不一致。
    """

    def handler(r):
        if r.url.params["reportName"] != "RPT_ECONOMY_CPI":
            return payload([], success=True)
        return payload(
            [
                {"REPORT_DATE": f"20{year:02d}-0{month}-01 00:00:00", "NATIONAL_SAME": 0.5}
                for year in (25, 24)
                for month in (6, 5, 4, 3, 2, 1)
            ]
        )

    result = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    cpi = next(i for i in result["indicators"] if i["key"] == "cn_cpi")
    note = next(e for e in result["evidence"] if e["title"] == CPI_NAME)["note"]
    # 请求窗口从 2024-06-11 起，但清单只到 2024-01-01 至 2025-06-01；note 必须写后者
    assert cpi["items"][-1]["date"] == "2024-01-01"
    assert "2024-06-11" not in note
    assert "2024-01-01 至 2025-06-01" in note
    assert "共 12 条" in note
    assert "45 天" in note


def test_stats_cover_the_whole_window_not_just_the_sampled_marks(settings):
    """极值必须取自全窗口：只在取样结果上求极值，等于用取样去证明取样没掩盖信息。"""

    def handler(r):
        if r.url.params["reportName"] != "RPT_ECONOMY_PPI":
            return payload([], success=True)
        # 2 月的尖峰会在按月取样后仍然保留，但 5 月的日内尖峰只存在于原始观测里
        return payload(
            [
                {"REPORT_DATE": "2024-12-01 00:00:00", "BASE_SAME": -1.8},
                {"REPORT_DATE": "2024-11-01 00:00:00", "BASE_SAME": 9.9},
                {"REPORT_DATE": "2024-10-01 00:00:00", "BASE_SAME": 0.0},
            ]
        )

    result = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    ppi = next(i for i in result["indicators"] if i["key"] == "cn_ppi")
    assert ppi["stats"] == {
        "n": 3,
        "first_date": "2024-10-01",
        "last_date": "2024-12-01",
        "max": {"value": 9.9, "date": "2024-11-01"},
        "min": {"value": -1.8, "date": "2024-12-01"},
    }


def test_every_indicator_carries_its_own_evidence(settings):
    def handler(r):
        # 按请求的 columns 应答：M2 与 M1 共用一张报表但各取各的列，mock 也必须分开。
        date_field, value_field = r.url.params["columns"].split(",")
        return payload([{date_field: "2026-04-01 00:00:00", value_field: 1.5}])

    result = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    assert result["complete"] is True
    assert len(result["evidence"]) == len(INDICATORS) == len(result["indicators"])
    assert len({e["id"] for e in result["evidence"]}) == len(INDICATORS)
    # 每个指标都能从自己的清单指回自己的证据，报告引用才不会指错来源
    assert (
        (result["indicators"][0]["evidence_id"])
        == (next(e for e in result["evidence"] if e["title"] == result["indicators"][0]["name"])["id"])
    )


def test_observations_drop_unparsable_rows_and_keep_last_per_day(settings):
    rows = [
        {"SOLAR_DATE": "2026-06-10 00:00:00", TENOR_FIELD: 1.75},
        {"SOLAR_DATE": "2026-06-10 00:00:00", TENOR_FIELD: 1.76},
        {"SOLAR_DATE": "2026-06-09 00:00:00", TENOR_FIELD: None},
        {"SOLAR_DATE": "2026-06-08 00:00:00", TENOR_FIELD: "1.74"},
        {"SOLAR_DATE": "2026-06-07 00:00:00", TENOR_FIELD: float("nan")},
        {"TENOR_FIELD": 1.0},
        "not-a-row",
    ]
    out = _observations(rows, date(2026, 6, 10))
    # 同日保留最后一条；无法解析、非有限、缺字段的行一律丢弃而不是当成 0
    assert [i["date"] for i in out] == ["2026-06-10", "2026-06-08"]
    assert out[0]["value"] == 1.76
    assert out[1]["value"] == 1.74


def test_observations_honour_the_indicator_date_and_value_field():
    """同一套取数逻辑要能读 REPORT_DATE 报表，字段名不再是写死的 SOLAR_DATE。"""
    rows = [
        {"REPORT_DATE": "2026-04-01 00:00:00", "NATIONAL_SAME": 0.5},
        {"REPORT_DATE": "2026-03-01 00:00:00", "NATIONAL_SAME": None},
    ]
    out = _observations(
        rows,
        date(2026, 6, 10),
        value_field="NATIONAL_SAME",
        name=CPI_NAME,
        unit="%",
        date_field="REPORT_DATE",
    )
    assert [i["date"] for i in out] == ["2026-04-01"]
    assert out[0]["name"] == CPI_NAME


def test_observations_drop_days_after_the_cutoff():
    """数据源忽略 filter 时，本地这一层必须兜住，否则未来利率会解历史。"""
    rows = [
        {"SOLAR_DATE": "2026-06-11 00:00:00", TENOR_FIELD: 1.80},
        {"SOLAR_DATE": "2026-06-10 00:00:00", TENOR_FIELD: 1.75},
    ]
    assert [i["date"] for i in _observations(rows, date(2026, 6, 10))] == ["2026-06-10"]


def test_monthly_marks_take_last_observation_of_each_month():
    obs = (
        [{"date": f"2026-06-{d:02d}", "value": float(d)} for d in (10, 9, 1)]
        + [{"date": f"2026-05-{d:02d}", "value": float(d)} for d in (30, 2)]
        + [
            {"date": "2026-01-15", "value": 1.0},
        ]
    )
    marks = _monthly_marks(obs)
    # 每个自然月只留该月最后一个可得观测；列表已按日期倒序，因此就是每月首次出现的那条
    assert [m["date"] for m in marks] == ["2026-06-10", "2026-05-30", "2026-01-15"]


def test_monthly_marks_cap_the_number_of_marks():
    obs = [{"date": f"20{year:02d}-12-31", "value": 1.0} for year in range(30, 0, -1)]
    assert len(_monthly_marks(obs)) == MAX_ITEMS


def test_monthly_marks_keep_the_most_recent_observation_even_mid_month():
    """最新一条未必是月末：它代表「当下利率水平」，不能被月末取样规则丢掉。"""
    obs = [{"date": "2026-06-10", "value": 1.75}, {"date": "2026-05-31", "value": 1.65}]
    marks = _monthly_marks(obs)
    assert marks[0]["date"] == "2026-06-10"


def test_change_marks_collapse_repeated_values():
    """准备金率常年不动：按月铺 12 行会把「哪几次调整」埋掉。"""
    obs = [
        {"date": "2026-06-01", "value": 9.5},
        {"date": "2026-05-01", "value": 9.5},
        {"date": "2025-05-07", "value": 9.5},
        {"date": "2024-09-27", "value": 10.0},
        {"date": "2024-01-01", "value": 10.0},
    ]
    assert [m["date"] for m in _change_marks(obs)] == ["2026-06-01", "2024-09-27"]


def test_change_marks_cap_the_number_of_marks():
    obs = [{"date": f"20{year:02d}-12-31", "value": float(year)} for year in range(30, 0, -1)]
    assert len(_change_marks(obs)) == MAX_ITEMS


def test_pages_are_followed_when_a_series_exceeds_one_page(settings):
    """数据源单页硬上限 500，24 个月的日频收益率约 530 条，不翻页会静默丢掉最早那段。"""
    calls = []

    def handler(r):
        if r.url.params["reportName"] != "RPTA_WEB_TREASURYYIELD":
            return payload([], success=True)
        page = int(r.url.params["pageNumber"])
        calls.append(page)
        if page == 1:
            rows = [
                {"SOLAR_DATE": f"2026-0{month}-01 00:00:00", TENOR_FIELD: 1.5} for month in (6, 5, 4, 3, 2, 1)
            ]
            return httpx.Response(200, json={"success": True, "result": {"data": rows, "count": 8}})
        return payload([{"SOLAR_DATE": "2025-12-01 00:00:00", TENOR_FIELD: 1.4}])

    result = fetch_china_macro(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
        settings,
        httpx.MockTransport(handler),
    )
    tenor = next(i for i in result["indicators"] if i["key"] == "cn_10y")
    # 第一页未满页但 count 说还有 2 条，说明早期观测确实被翻页取回来了
    assert calls == [1, 2]
    assert tenor["items"][-1]["date"] == "2025-12-01"
    assert tenor["stats"]["n"] == 7
    assert tenor["stats"]["first_date"] == "2025-12-01"
