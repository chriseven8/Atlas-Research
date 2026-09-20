import math
import statistics
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from financial_research.analytics import analyze, rsi_wilder
from financial_research.domain import Bar


def bars(closes):
    return [
        Bar(
            date=date(2026, 1, 1) + timedelta(days=i), open=c, high=c + 1, low=c - 1, close=c, volume=100
        ).model_dump(mode="json")
        for i, c in enumerate(closes)
    ]


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
