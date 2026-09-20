"""Tencent public daily prices. No synthetic fallback; optional research feeds remain separate."""

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from .domain import Bar, ProviderError, ResearchRequest
from .providers import AlphaVantageProvider, evidence
from .settings import Settings


class PublicMarketProvider:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    def market(self, req: ResearchRequest) -> dict:
        cn = req.market == "CN"
        currency, timezone = ("CNY", "Asia/Shanghai") if cn else ("USD", "America/New_York")
        try:
            with httpx.Client(timeout=self.settings.http_timeout_seconds, transport=self.transport) as client:
                if cn:
                    code, exchange = req.symbol.split(".")
                    ticker = exchange.lower() + code
                    name = req.symbol
                else:
                    # Quote metadata resolves exchange suffixes (e.g. AAPL.OQ, IBM.N).
                    quote = client.get("https://qt.gtimg.cn/q=us" + req.symbol)
                    quote.raise_for_status()
                    quote.encoding = "gbk"
                    match = re.search(r'="([^"\r\n]+)"', quote.text)
                    fields = match.group(1).split("~") if match else []
                    if len(fields) < 4 or not re.fullmatch(r"[A-Z0-9.\-]+", fields[2]):
                        raise ProviderError("未找到美股代码，请检查股票代码及上市状态。")
                    ticker, name = "us" + fields[2], fields[1]
                endpoint = "https://web.ifzq.gtimg.cn/appstock/app/" + (
                    "fqkline/get" if cn else "usfqkline/get"
                )
                start = req.as_of - timedelta(days=400)
                response = client.get(
                    endpoint, params={"param": f"{ticker},day,{start},{req.as_of},640,{'' if cn else 'qfq'}"}
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 0:
                    raise ProviderError("行情供应商未返回有效数据，请稍后重试。")
                data = payload.get("data", {}).get(ticker, {})
                rows = data.get("day" if cn else "qfqday", [])
                if cn:
                    metadata = data.get("qt", {}).get(ticker, [])
                    if len(metadata) > 1:
                        name = metadata[1]
            now = datetime.now(ZoneInfo(timezone))
            cutoff = min(req.as_of, now.date())
            if cutoff == now.date() and now.time() < (time(15, 15) if cn else time(16, 15)):
                cutoff -= timedelta(days=1)
            by_date = {}
            for row in rows:
                # Tencent A shares normally use lots; STAR market and US use shares.
                multiplier = 100 if cn and not req.symbol.startswith(("688", "689")) else 1
                bar = Bar(
                    date=row[0],
                    open=row[1],
                    close=row[2],
                    high=row[3],
                    low=row[4],
                    volume=round(float(row[5]) * multiplier),
                )
                if bar.date <= cutoff:
                    by_date[bar.date] = bar.model_dump(mode="json")
            bars = [by_date[d] for d in sorted(by_date)][-req.lookback_days :]
            if len(bars) < 20:
                raise ProviderError(
                    "有效日线不足 20 条，可能是新上市、停牌或供应商历史覆盖不足；未使用演示数据替代。"
                )
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("真实行情获取或校验失败，请检查网络并稍后重试。") from None
        adjustment = "raw" if cn else "qfq"
        warnings = [
            "公共行情可能延迟；仅分析已收盘日线，不提供逐笔实时行情。",
            "成交量已统一为股；不计算含分红再投资的总回报。",
        ]
        warnings.append(
            "A 股日线未复权，除权除息可能造成价格跳变。"
            if cn
            else "美股采用供应商当前版本前复权日线，历史值可能随公司行动修订，不适用于严格时点回测。"
        )
        if (req.as_of - datetime.fromisoformat(bars[-1]["date"]).date()).days > 7:
            warnings.append("最近行情距截止日超过 7 天，可能停牌、休市或数据陈旧。")
        return {
            "bars": bars,
            "name": name,
            "market": req.market,
            "currency": currency,
            "timezone": timezone,
            "adjustment": adjustment,
            "provider": "腾讯财经",
            "warnings": warnings,
            "evidence": [
                evidence(
                    "market",
                    f"{req.symbol} 真实日线（{adjustment}）",
                    "腾讯财经公共行情",
                    bars[-1]["date"],
                    bars,
                    False,
                    url=endpoint,
                    note="；".join(warnings),
                )
            ],
        }

    def news(self, req: ResearchRequest) -> dict:
        if req.market == "CN":
            from .china_news import fetch_china_news

            return fetch_china_news(req, self.settings, self.transport)
        return AlphaVantageProvider(self.settings, self.transport).news(req)

    def macro(self, req: ResearchRequest) -> dict:
        if req.market == "CN":
            raise ProviderError("中国宏观数据源尚未接入；不以美国利率替代中国宏观背景。")
        return AlphaVantageProvider(self.settings, self.transport).macro(req)
