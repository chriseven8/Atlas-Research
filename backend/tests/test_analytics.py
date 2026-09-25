import math
import statistics
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from financial_research.analytics import action_effects, analyze, attribute_actions, daily_facts, rsi_wilder
from financial_research.domain import Bar


def bars(closes):
    return [
        Bar(
            date=date(2026, 1, 1) + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c, volume=100
        ).model_dump(mode="json")
        for i, c in enumerate(closes)
    ]


def series(rows):
    """rows: (date, open, high, low, close, volume[, amount])"""
    return [
        Bar(
            date=date.fromisoformat(row[0]),
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
            volume=row[5],
            amount=row[6] if len(row) > 6 else None,
        ).model_dump(mode="json")
        for row in rows
    ]


# 一天零成交、一天大跌、一天向上跳空，用来验证 daily_facts 能把这三类事实分别定位到时点。
SPIKY = series(
    [
        ("2026-01-05", 100, 102, 99, 100, 1000),
        ("2026-01-06", 100, 101, 98, 99, 0),
        ("2026-01-07", 99, 100, 79, 80, 2000),
        ("2026-01-08", 84, 90, 83, 88, 5000),
        ("2026-01-09", 88, 95, 87, 94, 3000, 123456),
    ]
)


def test_largest_daily_move_is_located_to_date_and_ohlc():
    facts = daily_facts([Bar.model_validate(b) for b in SPIKY])
    move = facts["largest_daily_move"]
    assert move["date"] == "2026-01-07"
    assert move["change_pct"] == round((80 / 99 - 1) * 100, 2)
    # 定位到日期还不够，要能报出当天的开高低收，才谈得上核查这根日线本身。
    assert (move["open"], move["high"], move["low"], move["close"]) == (99, 100, 79, 80)


def test_range_extremes_carry_dates_and_amplitude():
    facts = daily_facts([Bar.model_validate(b) for b in SPIKY])
    assert facts["range_high"] == {"value": 102, "date": "2026-01-05"}
    assert facts["range_low"] == {"value": 79, "date": "2026-01-07"}
    assert facts["range_amplitude_pct"] == round((102 / 79 - 1) * 100, 2)
    assert facts["first_date"] == "2026-01-05"


def test_continuity_facts_flag_zero_volume_and_gaps():
    facts = daily_facts([Bar.model_validate(b) for b in SPIKY])
    assert facts["zero_volume_days"] == ["2026-01-06"]
    assert facts["gap_days"] == [{"date": "2026-01-08", "gap_pct": 5.0}]
    assert facts["gap_count"] == 1


def test_gap_days_are_capped_but_count_is_kept():
    # 高波动标的上跳空会很多（实测 300308 有 40 天），逐条铺开只会把真正要看的那几条埋掉。
    # 截断的是明细，总数必须原样保留，否则「有多少天跳空」这个事实就丢了。
    rows = [("2026-01-05", 100, 102, 99, 100, 1000)]
    for i in range(1, 15):
        prev = rows[-1][4]
        open_price = prev * 1.05
        rows.append((f"2026-01-{5 + i:02d}", open_price, open_price + 2, open_price - 1, open_price, 1000))
    facts = daily_facts([Bar.model_validate(b) for b in series(rows)])
    assert facts["gap_count"] == 14
    assert len(facts["gap_days"]) == 10
    magnitudes = [abs(g["gap_pct"]) for g in facts["gap_days"]]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_volume_ratio_and_amount_coverage():
    facts = daily_facts([Bar.model_validate(b) for b in SPIKY])
    assert facts["volume_ratio"] == round(2200 / 2200, 2)
    assert facts["amount_days"] == 1


def test_volume_ratio_is_none_without_baseline_volume():
    facts = daily_facts([Bar.model_validate(b) for b in series([("2026-01-05", 100, 102, 99, 100, 0)])])
    assert facts["volume_ratio"] is None


def test_price_volume_corr_is_none_for_constant_volume_not_zero():
    # 成交量恒定意味着「量变」这一列没有方差，相关系数无定义。
    # 返回 0 会被读成「价量无关」，那是把一个未定义的量当成了观测结论。
    facts = daily_facts([Bar.model_validate(b) for b in bars([100, 110, 99, 105])])
    assert facts["price_volume_corr"] is None


def test_price_volume_corr_computed_when_volume_varies():
    facts = daily_facts([Bar.model_validate(b) for b in SPIKY])
    assert isinstance(facts["price_volume_corr"], float)
    assert -1 <= facts["price_volume_corr"] <= 1


def test_top_moves_are_capped_and_ordered_by_absolute_move():
    facts = daily_facts(
        [
            Bar.model_validate(b)
            for b in series(
                [
                    ("2026-01-05", 100, 101, 99, 100, 1000),
                    ("2026-01-06", 100, 121, 99, 120, 1000),
                    ("2026-01-07", 120, 121, 95, 96, 1000),
                    ("2026-01-08", 96, 97, 95, 96, 1000),
                    ("2026-01-09", 96, 111, 95, 110, 1000),
                    ("2026-01-12", 110, 121, 109, 120, 1000),
                    ("2026-01-13", 120, 121, 119, 120, 1000),
                ]
            )
        ]
    )
    assert len(facts["top_moves"]) == 5
    magnitudes = [abs(m["change_pct"]) for m in facts["top_moves"]]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_known_price_returns_drawdown_and_volatility():
    result = analyze(bars([100, 110, 99]))
    assert result["period_return_pct"] == -1
    assert result["daily_change_pct"] == -10
    assert result["max_drawdown_pct"] == -10
    assert result["volatility_pct"] == round(statistics.stdev([0.1, -0.1]) * math.sqrt(252) * 100, 2)
    assert result["sma20"] is None
    assert result["rsi14"] is None
    assert result["trend"] == "样本不足"


def test_wilder_reference_initial_rsi_and_boundaries():
    closes = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
    ]
    assert rsi_wilder(closes) == 70.46
    assert rsi_wilder(list(range(1, 40))) == 100
    assert rsi_wilder(list(range(40, 1, -1))) == 0
    assert rsi_wilder([20] * 40) == 50


def test_sma_and_trend_require_full_windows():
    result = analyze(bars(list(range(100, 160))))
    assert result["sma20"] == 149.5
    assert result["sma50"] == 134.5
    assert result["trend"] == "上行"
    assert analyze(bars([100] * 30))["sma50"] is None


@pytest.mark.parametrize("change", [{"close": float("nan")}, {"high": 1}, {"volume": -1}, {"low": 0}])
def test_invalid_bars_are_rejected(change):
    sample = bars([100])[0]
    with pytest.raises(ValidationError):
        Bar.model_validate({**sample, **change})


def test_duplicate_dates_are_rejected():
    sample = bars([100])[0]
    with pytest.raises(ValueError, match="不重复"):
        analyze([sample, sample])


# 未复权序列在除权日按原值走，前复权序列把生效日之前的价格整体缩放到 99，
# 等价于每股派现 1 元。两组行情只差这一次缩放，用来隔离出「公司行动当日影响」。
RAW_AROUND_EX = series(
    [
        ("2026-05-01", 100, 102, 99, 100, 1000),
        ("2026-05-04", 100, 102, 99, 100, 1000),
        ("2026-05-05", 100, 102, 99, 100, 1000),
        ("2026-05-06", 100, 102, 99, 100, 1000),
        ("2026-05-07", 100, 102, 99, 100, 1000),
    ]
)
ADJ_AROUND_EX = series(
    [
        ("2026-05-01", 99, 101, 98, 99, 1000),
        ("2026-05-04", 99, 101, 98, 99, 1000),
        ("2026-05-05", 100, 102, 99, 100, 1000),
        ("2026-05-06", 100, 102, 99, 100, 1000),
        ("2026-05-07", 100, 102, 99, 100, 1000),
    ]
)
EX_ACTION = {
    "ex_date": "2026-05-05",
    "record_date": "2026-05-01",
    "plan": "10派1元",
    "fiscal_year": "2025",
}


def test_action_effect_is_measured_only_on_the_ex_date():
    clean = [Bar.model_validate(b) for b in RAW_AROUND_EX]
    adjusted = [Bar.model_validate(b) for b in ADJ_AROUND_EX]
    effects = action_effects(clean, adjusted)
    # 前复权只缩放生效日之前的价格，因此差异只应出现在 05-05 这一天
    assert list(effects) == ["2026-05-05"]
    assert effects["2026-05-05"] == round((100 / 99 - 1) * 100, 3)


def test_action_effect_is_empty_without_an_adjusted_series():
    clean = [Bar.model_validate(b) for b in RAW_AROUND_EX]
    assert action_effects(clean, []) == {}


def test_action_effect_ignores_rounding_noise_below_the_threshold():
    clean = [Bar.model_validate(b) for b in RAW_AROUND_EX]
    # 完全相同的一组序列差值恒为 0，不该被读成一次除权
    assert action_effects(clean, clean) == {}


def test_attribution_aligns_the_largest_move_with_ex_dates():
    clean = [Bar.model_validate(b) for b in RAW_AROUND_EX]
    adjusted = [Bar.model_validate(b) for b in ADJ_AROUND_EX]
    out = attribute_actions(clean, adjusted, [EX_ACTION])
    assert out["adjusted_series_available"] is True
    # 这段序列里最大单日波动是 0%（价格不动），它落在除权日之后的最近一天
    assert out["largest_move"]["date"] == "2026-05-04"
    assert out["largest_move"]["action_effect_pct"] == 0.0
    assert out["largest_move"]["nearest_ex_date"] == "2026-05-05"
    assert out["largest_move"]["trading_days_to_nearest_ex_date"] == 1
    assert out["ex_dates"] == [
        {"date": "2026-05-05", "effect_pct": round((100 / 99 - 1) * 100, 3), "announced": "10派1元"}
    ]


def test_attribution_keeps_dates_that_only_the_adjusted_series_shows():
    """公告元数据缺一条时，复权序列仍会显出那天的调整。

    只报公告会让「有跳变却没记录」这种情况静默消失，所以两边取并集，
    认不出来的那天 announced 为 None。
    """
    clean = [Bar.model_validate(b) for b in RAW_AROUND_EX]
    adjusted = [Bar.model_validate(b) for b in ADJ_AROUND_EX]
    out = attribute_actions(clean, adjusted, [])
    assert out["ex_dates"] == [
        {"date": "2026-05-05", "effect_pct": round((100 / 99 - 1) * 100, 3), "announced": None}
    ]


def test_attribution_marks_the_effect_unknown_without_an_adjusted_series():
    clean = [Bar.model_validate(b) for b in RAW_AROUND_EX]
    out = attribute_actions(clean, [], [EX_ACTION])
    # 取不到复权序列时归因是「不可得」，不是 0——0 会被读成「查过了，与除权无关」
    assert out["adjusted_series_available"] is False
    assert out["largest_move"]["action_effect_pct"] is None
    assert out["ex_dates"] == [{"date": "2026-05-05", "effect_pct": None, "announced": "10派1元"}]
