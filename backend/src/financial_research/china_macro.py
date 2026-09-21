"""中国国债收益率（东方财富数据中心）。市场观测值，按截止日取数，不需要跳过历史日期。"""

import math
from datetime import date, timedelta

import httpx

from .domain import ProviderError
from .providers import evidence

YIELD_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
# 10 年期国债到期收益率是市场对长期无风险利率的直接观测，用作折现率代理。
TENOR_FIELD = "EMM00166466"
TENOR_NAME = "中国 10 年期国债到期收益率"
SOURCE_NAME = "东方财富数据中心（中债国债收益率曲线）"
SOURCE_URL = "https://data.eastmoney.com/cjsj/zmgzsyl.html"
SPAN_DAYS = 400
MAX_ITEMS = 12


def _observations(rows, cutoff: date):
    """把接口返回的日频行整理成按日期倒序的观测列表，丢弃无法解析的数值。

    cutoff 在本地再过滤一次，不把截止日的判断完全托给数据源：请求里的 filter
    只是减少传输量，一旦上游忽略它、或应答里混入截止日之后的观测，报告就会用
    未来数据解释历史，而这恰恰是本项目最不能出的错。cutoff 是必填参数，好让
    「忘了按截止日收口」在调用点就暴露，而不是变成一个静默的前视偏差。
    """
    seen = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row["SOLAR_DATE"])[:10])
            value = float(row[TENOR_FIELD])
        except (KeyError, TypeError, ValueError):
            continue
        if day > cutoff or not math.isfinite(value):
            continue
        # 同一天出现多行时保留最后一次，避免重复日期让下游把同一天算两次。
        seen[day] = {"date": day.isoformat(), "value": value, "name": TENOR_NAME, "unit": "%"}
    return [seen[day] for day in sorted(seen, reverse=True)]


def _monthly_marks(observations):
    """每个月取最后一个可得观测，并保留最近一次观测。

    日频序列直接截前 12 条只会覆盖两三个交易周，「利率环境」读不出方向；
    按月取样等价于 Alpha Vantage 那条月度序列，两边的宏观口径才可比。
    列表本身按日期倒序，因此每月第一次出现的就是当月最后一个观测。
    """
    marks = [observations[0]]
    months = {observations[0]["date"][:7]}
    for item in observations[1:]:
        month = item["date"][:7]
        if month in months:
            continue
        months.add(month)
        marks.append(item)
        if len(marks) >= MAX_ITEMS:
            break
    return marks


def fetch_china_macro(req, settings, transport=None):
    start = req.as_of - timedelta(days=SPAN_DAYS)
    params = {
        "reportName": "RPTA_WEB_TREASURYYIELD",
        "columns": f"SOLAR_DATE,{TENOR_FIELD}",
        "pageSize": 400,
        "sortColumns": "SOLAR_DATE",
        "sortTypes": "-1",
        # 该序列按日发布、不在事后改写，因此可以老老实实按截止日取数；
        # 不像 Alpha Vantage 的联邦基金利率那样必须整个跳过历史日期来避免前视偏差。
        "filter": f"(SOLAR_DATE>='{start}')(SOLAR_DATE<='{req.as_of}')",
    }
    try:
        with httpx.Client(
            timeout=settings.http_timeout_seconds,
            transport=transport,
            headers={"User-Agent": "AtlasResearch/0.1", "Referer": "https://data.eastmoney.com/"},
        ) as client:
            response = client.get(YIELD_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("中国利率数据源暂时不可用；未以虚构利率替代。") from exc
    if payload.get("success") is not True:
        raise ProviderError("中国利率数据源返回失败；未以虚构利率替代。")
    rows = (payload.get("result") or {}).get("data")
    if not isinstance(rows, list):
        raise ProviderError("中国利率数据源未返回有效列表；未以虚构利率替代。")

    observations = _observations(rows, req.as_of)
    if not observations:
        raise ProviderError(f"截止 {req.as_of} 前无有效利率观测值。")
    items = _monthly_marks(observations)

    note = (
        f"取 {TENOR_NAME} 在 {start} 至 {req.as_of} 的观测，每月取当月最后一个可得观测；"
        "收益率是市场成交观测，不是统计口径的修订值。"
    )
    return {
        "items": items,
        "evidence": [
            evidence(
                "macro",
                TENOR_NAME,
                SOURCE_NAME,
                items[0]["date"],
                items,
                False,
                url=SOURCE_URL,
                note=note,
            )
        ],
        "warnings": [
            "宏观覆盖仅含 10 年期国债到期收益率；不含货币政策立场、通胀与盈利预期，不是完整宏观模型。",
            "月度取样只保留每月最后一个可得观测，不等于月末交易日，也不覆盖当月内的路径。",
            "该序列为长期无风险利率的市场观测，可作为折现率代理；单一利率不能确定股价方向。",
        ],
    }
