from datetime import date

import httpx
import pytest

from financial_research.china_macro import (
    MAX_ITEMS,
    TENOR_FIELD,
    _monthly_marks,
    _observations,
    fetch_china_macro,
)
from financial_research.domain import ProviderError, ResearchRequest


def payload(rows, success=True):
    return httpx.Response(200, json={"success": success, "result": {"data": rows}})


def rows_for(days):
    """生成倒序的日频行，数值随日期单调变化，便于断言取到的是哪一天。"""
    return [
        {"SOLAR_DATE": f"2026-06-{day:02d} 00:00:00", TENOR_FIELD: 1.70 + day / 1000}
        for day in sorted(days, reverse=True)
    ]


def test_filters_by_as_of_and_samples_monthly(settings):
    seen = {}

    def handler(r):
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
    assert [i["date"] for i in result["items"]] == ["2026-06-10", "2026-05-31", "2026-04-30"]
    assert result["items"][0]["name"] == "中国 10 年期国债到期收益率"
    assert result["items"][0]["unit"] == "%"


def test_evidence_is_a_real_snapshot_not_demo(settings):
    def handler(r):
        return payload(rows_for(range(1, 11)))

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
    req = ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10")
    with pytest.raises(ProviderError):
        fetch_china_macro(req, settings, httpx.MockTransport(lambda r: response))


def test_no_observation_before_as_of_is_an_error(settings):
    def handler(r):
        # 全部落在截止日之后 → 过滤后一条不剩，不能拿未来的利率充当历史
        return payload([{"SOLAR_DATE": "2026-08-01 00:00:00", TENOR_FIELD: 1.8}])

    with pytest.raises(ProviderError, match="无有效利率观测值"):
        fetch_china_macro(
            ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-10"),
            settings,
            httpx.MockTransport(handler),
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
