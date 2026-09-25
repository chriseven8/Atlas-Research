"""第二行情源（新浪财经）与交叉校验。

主序列只有一家供应商，取数出错、口径变更或漏条都无从发现——这正是「无法交叉校验」
那条待解问题的成因。这里取同一段区间的独立第二源，逐日比对收盘价与成交量。

比对结论分三类，任何一类都如实写进报告：一致、不一致、不可用。「没查出问题」与
「没查」必须能被区分，否则交叉校验本身就成了装饰。
"""

import json
import re
import statistics
from datetime import date, timedelta

import httpx

from .domain import ProviderError, ResearchRequest
from .settings import Settings

SINA_CN_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SINA_US_URL = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/x/US_MinKService.getDailyK"
SOURCE_NAME = "新浪财经"
# 比对最近这些个重叠交易日。窗口放长会把复权基准不同造成的差异也算进来，
# 而那种差异在最近几天之外必然出现（前复权只缩放生效日之前的价格）。
CROSS_CHECK_DAYS = 10
# 相对容差。实测两源在同一交易日上收盘价完全相同、成交量相差万分之几
# （2026-09-23 300308：腾讯 14,763,300 股 vs 新浪 14,763,319 股），
# 因此 0.5% 足够宽松到不误报，又足够严到能抓住真正的口径分歧。
CLOSE_TOLERANCE_PCT = 0.5
VOLUME_TOLERANCE_PCT = 0.5
# 两源成交量单位均为股：腾讯 A 股返回的是手（主序列已按 100 归一），新浪直接给股。
CN_SPAN_DAYS = 400


def _sina_cn(req: ResearchRequest, client: httpx.Client) -> list[dict]:
    code, exchange = req.symbol.split(".")
    # datalen 从**今天**往前数，而研究截止日可能在几个月前，所以要按
    # 「截止日往前 400 天」到今天的天数折算，否则历史截止日会取不到重叠区间。
    span = (date.today() - (req.as_of - timedelta(days=CN_SPAN_DAYS))).days
    datalen = min(1000, max(60, int(span * 0.75)))
    response = client.get(
        SINA_CN_URL,
        params={"symbol": exchange.lower() + code, "scale": 240, "ma": "no", "datalen": datalen},
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ProviderError("第二行情源未返回有效列表。")
    return [
        {"date": str(row["day"]), "close": float(row["close"]), "volume": float(row["volume"])}
        for row in rows
        if isinstance(row, dict) and row.get("day") and row.get("close")
    ]


def _sina_us(req: ResearchRequest, client: httpx.Client) -> list[dict]:
    # 该端点无区间参数，一次返回全历史（实测约 940KB），必须按截止日收口后再留尾部。
    response = client.get(SINA_US_URL, params={"symbol": req.symbol})
    response.raise_for_status()
    match = re.search(r"x\((\[.*\])\)", response.text, re.S)
    if not match:
        raise ProviderError("第二行情源未返回可解析的 JSONP。")
    rows = json.loads(match.group(1))
    out = []
    for row in rows:
        try:
            day = date.fromisoformat(str(row["d"])[:10])
            if day <= req.as_of:
                out.append({"date": day.isoformat(), "close": float(row["c"]), "volume": float(row["v"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_sina_bars(req: ResearchRequest, settings: Settings, transport=None) -> dict[str, dict]:
    """取第二源日线，返回 {日期: {close, volume}}。取不到就抛错，由调用方降级。"""
    try:
        with httpx.Client(
            timeout=settings.http_timeout_seconds,
            transport=transport,
            headers={"User-Agent": "AtlasResearch/0.1"},
        ) as client:
            rows = _sina_cn(req, client) if req.market == "CN" else _sina_us(req, client)
    except (httpx.HTTPError, ValueError, ProviderError) as exc:
        raise ProviderError(f"第二行情源（{SOURCE_NAME}）本轮不可用，未完成交叉校验。") from exc
    by_date = {}
    for row in rows:
        by_date[row["date"]] = {"close": row["close"], "volume": row["volume"]}
    if not by_date:
        raise ProviderError(f"第二行情源（{SOURCE_NAME}）未返回截止日前的有效日线。")
    return by_date


def _relative_diff_pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def cross_check(primary_bars: list[dict], second: dict[str, dict], only_after: str | None = None) -> dict:
    """逐日比对两源。only_after 之前的日子一律不比。

    前复权把生效日之前的历史价格整体缩放过，未复权没有，因此同期两源在除权日
    之前必然成比例地错开——那是复权口径不同，不是数据出错。把比对起点放到最近
    一次公司行动之后，差异才只可能来自数据本身。
    """
    primary = {bar["date"]: bar for bar in primary_bars}
    dates = sorted(day for day in primary if day in second and (only_after is None or day > only_after))[
        -CROSS_CHECK_DAYS:
    ]
    if not dates:
        return {
            "status": "unavailable",
            "source": SOURCE_NAME,
            "compared_days": 0,
            "reason": "两源无重叠交易日，或重叠部分全部落在最近一次公司行动之前，无法比对。",
        }
    closes, volumes, ratios = [], [], []
    for day in dates:
        mine, theirs = primary[day], second[day]
        ratios.append(mine["close"] / theirs["close"])
        if abs(_relative_diff_pct(mine["close"], theirs["close"])) > CLOSE_TOLERANCE_PCT:
            closes.append(
                {
                    "date": day,
                    "primary": mine["close"],
                    "second": theirs["close"],
                    "diff_pct": round(_relative_diff_pct(mine["close"], theirs["close"]), 4),
                }
            )
        if abs(_relative_diff_pct(mine["volume"], theirs["volume"])) > VOLUME_TOLERANCE_PCT:
            volumes.append(
                {
                    "date": day,
                    "primary": mine["volume"],
                    "second": theirs["volume"],
                    "diff_pct": round(_relative_diff_pct(mine["volume"], theirs["volume"]), 4),
                }
            )
    mismatched = bool(closes or volumes)
    return {
        "status": "mismatch" if mismatched else "consistent",
        "source": SOURCE_NAME,
        "compared_days": len(dates),
        "date_range": [dates[0], dates[-1]],
        "tolerance_pct": CLOSE_TOLERANCE_PCT,
        "close_mismatches": closes[:5],
        "volume_mismatches": volumes[:5],
        # 两源收盘价之比的中位数：明显偏离 1 说明两边复权基准不同，而不是某一天错。
        "close_ratio_median": round(statistics.median(ratios), 6),
        "note": (
            f"与独立第二源（{SOURCE_NAME}）在 {dates[0]} 至 {dates[-1]} 的 {len(dates)} 个重叠交易日"
            f"逐日比对收盘价与成交量，相对容差 {CLOSE_TOLERANCE_PCT}%。"
            + ("发现不一致，需人工核查。" if mismatched else "未发现超出容差的差异。")
        ),
    }
