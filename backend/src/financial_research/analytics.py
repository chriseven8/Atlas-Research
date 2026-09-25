import math
import statistics
from itertools import pairwise

from .domain import Bar

# 极端单日波动只报前几名：报告需要的是「最大那一根落在哪天、当天什么价」，
# 而不是把整条序列抄一遍——序列要多少有多少，会淹掉真正要看的那几行。
TOP_MOVES = 5
# 开盘相对前收盘的跳空阈值。A 股日内涨跌幅限制是 10%（创业板/科创板 20%），
# 3% 的跳空已经足够异常，值得单列出来供核查。
GAP_THRESHOLD_PCT = 3.0
# 跳空日只列最大的这些条，同时另给总条数。高波动标的上 3% 跳空是常态
# （实测 300308 在 222 根日线里有 40 天跳空超 3%），把 40 行全部铺进上下文
# 只会把真正要看的那几条埋掉。总数保留，所以「有多少天跳空」并不会因此丢失。
GAP_REPORT_LIMIT = 10
# 量比的分母窗口。20 个交易日约等于一个月，比 60 日更能反映近期量能状态。
VOLUME_RATIO_WINDOW = 20
# 判定「这天有公司行动」的阈值（百分点）。前复权序列在生效日之前整体缩放，因此
# 「前复权收益率 − 未复权收益率」在没有公司行动的日子里恒为 0；留一个小阈值只是
# 避免浮点噪声被读成一次除权。
ACTION_EFFECT_THRESHOLD_PCT = 0.05


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


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    """皮尔逊相关系数；样本太短或某一边是常数时返回 None，不返回 0 冒充「无关」。"""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    try:
        return round(statistics.correlation(xs, ys), 3)
    except statistics.StatisticsError:
        return None


def daily_facts(clean: list[Bar]) -> dict:
    """把逐日 OHLCV 压成一组可核对的确定性事实。

    这个函数存在的理由：行情取数早就拿回了完整 OHLCV，但下游只把收盘价喂给模型，
    于是模型每次都只能问「成交量呢、最大波动在哪天」。这些问题的答案本来就在手里，
    只是没有算出来。数值一律在这里算好，模型只负责解读，不重新计算。
    """
    top_moves = sorted(
        (
            {
                "date": str(cur.date),
                "open": cur.open,
                "high": cur.high,
                "low": cur.low,
                "close": cur.close,
                "volume": cur.volume,
                "change_pct": round((cur.close / prev.close - 1) * 100, 2),
            }
            for prev, cur in pairwise(clean)
        ),
        key=lambda item: abs(item["change_pct"]),
        reverse=True,
    )[:TOP_MOVES]

    high_day = max(clean, key=lambda b: b.high)
    low_day = min(clean, key=lambda b: b.low)
    gaps = sorted(
        (
            {"date": str(cur.date), "gap_pct": round((cur.open / prev.close - 1) * 100, 2)}
            for prev, cur in pairwise(clean)
            if abs(cur.open / prev.close - 1) * 100 >= GAP_THRESHOLD_PCT
        ),
        key=lambda item: abs(item["gap_pct"]),
        reverse=True,
    )

    volumes = [b.volume for b in clean]
    recent = volumes[-5:]
    baseline = volumes[-VOLUME_RATIO_WINDOW:]
    baseline_mean = statistics.mean(baseline) if baseline else 0.0
    volume_ratio = round(statistics.mean(recent) / baseline_mean, 2) if baseline_mean > 0 else None

    returns, volume_changes = [], []
    for prev, cur in pairwise(clean):
        # 前后两天都要有成交量，量变才有定义；缺一天就跳过这一对，不做插补。
        if prev.volume > 0:
            returns.append(cur.close / prev.close - 1)
            volume_changes.append(cur.volume / prev.volume - 1)

    amounts = [b.amount for b in clean if b.amount is not None]
    return {
        "first_date": str(clean[0].date),
        "range_high": {"value": high_day.high, "date": str(high_day.date)},
        "range_low": {"value": low_day.low, "date": str(low_day.date)},
        # 振幅是区间极值的比值，和「最大单日波动」不是一回事：前者看全程跨度，后者看单日冲击。
        "range_amplitude_pct": round((high_day.high / low_day.low - 1) * 100, 2),
        "top_moves": top_moves,
        "largest_daily_move": top_moves[0] if top_moves else None,
        "gap_days": gaps[:GAP_REPORT_LIMIT],
        "gap_count": len(gaps),
        "zero_volume_days": [str(b.date) for b in clean if b.volume == 0],
        "volume_ratio": volume_ratio,
        "price_volume_corr": _correlation(returns, volume_changes),
        # 成交额覆盖率必须显式给出：模型看到 amount 缺失时才知道是「上游没给」而不是「没看」。
        "amount_days": len(amounts),
    }


def action_effects(clean: list[Bar], adjusted: list[Bar]) -> dict[str, float]:
    """逐日给出「前复权收益率 − 未复权收益率」（百分点），即公司行动当天的价格影响。

    未复权序列在除权日会因分红送转跳变，前复权序列则把生效日之前的价格按同一比例
    缩放、使相邻收益连续。两者只会在公司行动生效日出现差异，因此这个差值是**实测**
    出来的当日影响，而不是按公告比例反推的估计值。取不到复权序列时返回空 dict，
    由调用方如实标注为不可得。
    """
    raw_close = {str(bar.date): bar.close for bar in clean}
    adj_close = {str(bar.date): bar.close for bar in adjusted}
    effects = {}
    for prev, cur in pairwise(clean):
        before, day = str(prev.date), str(cur.date)
        if before not in adj_close or day not in adj_close:
            continue
        raw_return = raw_close[day] / raw_close[before] - 1
        adjusted_return = adj_close[day] / adj_close[before] - 1
        effect = round((adjusted_return - raw_return) * 100, 3)
        if abs(effect) >= ACTION_EFFECT_THRESHOLD_PCT:
            effects[day] = effect
    return effects


def attribute_actions(clean: list[Bar], adjusted: list[Bar], actions: list[dict]) -> dict:
    """把最大单日波动与公司行动对齐，回答「这根跳变是不是分红送转造成的」。

    公告元数据（腾讯把它内嵌在未复权日线的当日行里）与实测调整取并集：公告缺一条时，
    复权序列仍会显出那天的调整，只报公告会让「有跳变却没记录」这种情况静默消失。
    """
    dates = [str(bar.date) for bar in clean]
    effects = action_effects(clean, adjusted)
    recorded = {str(action.get("ex_date")): action for action in actions if action.get("ex_date") in dates}
    ex_dates = sorted(set(effects) | set(recorded))

    largest = daily_facts(clean)["largest_daily_move"]
    move = None
    if largest:
        index = dates.index(largest["date"])
        nearest = min(ex_dates, key=lambda ex: abs(dates.index(ex) - index), default=None)
        move = {
            "date": largest["date"],
            "change_pct": largest["change_pct"],
            "action_effect_pct": effects.get(largest["date"], 0.0) if adjusted else None,
            "nearest_ex_date": nearest,
            "trading_days_to_nearest_ex_date": abs(dates.index(nearest) - index) if nearest else None,
        }
    return {
        "adjusted_series_available": bool(adjusted),
        "largest_move": move,
        "ex_dates": [
            {
                "date": day,
                "effect_pct": effects.get(day),
                "announced": (recorded.get(day) or {}).get("plan"),
            }
            for day in ex_dates
        ],
        "method": (
            "公司行动当日影响 = 前复权收益率 − 未复权收益率（同一供应商、同一区间），"
            "是实测差值而非按公告比例推算。"
        ),
    }


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
        "methodology": (
            "SMA20/50；Wilder RSI14；日简单收益率样本标准差 × √252；收盘价峰谷回撤；"
            f"区间高低点取当日最高/最低价；最大波动与量比按日线计算（量比 = 近 5 日均量 / 近 "
            f"{VOLUME_RATIO_WINDOW} 日均量）。"
        ),
        **daily_facts(clean),
    }
