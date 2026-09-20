import math
import statistics
from itertools import pairwise

from .domain import Bar


def rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in pairwise(closes)]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period
    for g, loss_value in zip(gains[period:], losses[period:], strict=True):
        gain = (gain * (period - 1) + g) / period
        loss = (loss * (period - 1) + loss_value) / period
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return round(100 - 100 / (1 + gain / loss), 2)


def analyze(bars: list[dict]) -> dict:
    clean = sorted((Bar.model_validate(b) for b in bars), key=lambda b: b.date)
    if len(clean) < 2 or len({b.date for b in clean}) != len(clean):
        raise ValueError("至少需要两条日期不重复的行情记录")
    closes = [b.close for b in clean]
    returns = [b / a - 1 for a, b in pairwise(closes)]
    peak = closes[0]
    drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        drawdown = min(drawdown, close / peak - 1)
    sma20 = statistics.mean(closes[-20:]) if len(closes) >= 20 else None
    sma50 = statistics.mean(closes[-50:]) if len(closes) >= 50 else None
    trend = "样本不足"
    if sma20 is not None and sma50 is not None:
        trend = "上行" if closes[-1] > sma20 > sma50 else "下行" if closes[-1] < sma20 < sma50 else "震荡"
    return {
        "last_close": closes[-1],
        "last_date": str(clean[-1].date),
        "sample_size": len(clean),
        "daily_change_pct": round(returns[-1] * 100, 2),
        "period_return_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
        "sma20": round(sma20, 4) if sma20 is not None else None,
        "sma50": round(sma50, 4) if sma50 is not None else None,
        "rsi14": rsi_wilder(closes),
        "volatility_pct": round(statistics.stdev(returns) * math.sqrt(252) * 100, 2)
        if len(returns) > 1
        else None,
        "max_drawdown_pct": round(drawdown * 100, 2),
        "trend": trend,
        "methodology": "SMA20/50；Wilder RSI14；日简单收益率样本标准差 × √252；收盘价峰谷回撤。",
    }
