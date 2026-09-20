import hashlib
import json
import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from .domain import Bar, Evidence, ProviderError, ResearchRequest
from .settings import Settings


def snapshot_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def evidence(kind: str, title: str, source: str, observed_at: str, payload, demo: bool, **kwargs) -> dict:
    digest = snapshot_hash(payload)
    return Evidence(
        id=f"{kind}-{digest[:12]}",
        title=title,
        source=source,
        observed_at=observed_at,
        kind=kind,
        is_demo=demo,
        snapshot_hash=digest,
        **kwargs,
    ).model_dump(mode="json")


class DemoProvider:
    """Synthetic data only. No historical prices or fabricated publisher attribution."""

    def market(self, req: ResearchRequest) -> dict:
        base = {"AAPL": 170, "MSFT": 350, "NVDA": 110, "SPY": 470}[req.symbol]
        phase = int(hashlib.sha256(req.symbol.encode()).hexdigest()[:4], 16) / 1000
        dates = [req.as_of - timedelta(days=i) for i in range(220, -1, -1)]
        dates = [d for d in dates if d.weekday() < 5][-req.lookback_days :]
        bars = []
        for d in dates:
            t = (d - date(2024, 1, 1)).days
            close = round(
                base * math.exp(t * 0.00008 + 0.05 * math.sin(t / 23 + phase) + 0.013 * math.cos(t / 3.2)), 2
            )
            opening = round(close * (1 + 0.004 * math.sin(t)), 2)
            bars.append(
                Bar(
                    date=d,
                    open=opening,
                    high=round(max(opening, close) * 1.009, 2),
                    low=round(min(opening, close) * 0.992, 2),
                    close=close,
                    volume=int(22_000_000 + 9_000_000 * (1 + math.sin(t / 5))),
                ).model_dump(mode="json")
            )
        ev = evidence(
            "market",
            f"{req.symbol} 合成日线数据",
            "Atlas 演示生成器 v1",
            str(dates[-1]),
            bars,
            True,
            note="完全合成，不代表真实价格；仅跳过周末，未模拟交易所节假日。",
        )
        return {
            "bars": bars,
            "currency": "USD",
            "timezone": "America/New_York",
            "adjustment": "synthetic",
            "evidence": [ev],
            "warnings": ["所有行情均为合成演示数据，不可用于投资判断。"],
        }

    def news(self, req: ResearchRequest) -> dict:
        items = []
        evs = []
        for i, (title, summary) in enumerate(
            [
                ("演示情景：市场关注业务增长的持续性", "虚构事件，用于展示增长预期与估值风险的研究流程。"),
                ("演示情景：行业需求出现分化", "虚构事件，用于展示正面催化因素与不确定性并存的情况。"),
                (
                    "演示情景：投资者等待下一次业绩披露",
                    "虚构事件，不代表真实披露日程，也不构成对未来业绩的预测。",
                ),
            ]
        ):
            published = f"{req.as_of - timedelta(days=i + 1)}T12:00:00+00:00"
            item = {
                "title": title,
                "summary": summary,
                "published_at": published,
                "url": None,
                "source": "Atlas 虚构情景",
                "sentiment": None,
            }
            ev = evidence("news", title, item["source"], published, item, True)
            item["evidence_id"] = ev["id"]
            items.append(item)
            evs.append(ev)
        return {"items": items, "evidence": evs, "warnings": ["新闻为明确标记的虚构情景，未检索真实媒体。"]}

    def macro(self, req: ResearchRequest) -> dict:
        items = [
            {
                "date": str(req.as_of.replace(day=1) - timedelta(days=1)),
                "value": 4.25,
                "name": "演示联邦基金利率",
                "unit": "%",
            }
        ]
        ev = evidence(
            "macro",
            "合成利率情景",
            "Atlas 演示生成器 v1",
            items[0]["date"],
            items,
            True,
            note="4.25% 为演示设定，不是真实利率。",
        )
        return {"items": items, "evidence": [ev], "warnings": ["宏观数值为演示设定。"]}


class AlphaVantageProvider:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    def fetch(self, function: str, **params) -> dict:
        if not self.settings.live_ready:
            raise ProviderError("真实数据未配置：请设置 ALPHA_VANTAGE_API_KEY。")
        try:
            with httpx.Client(timeout=self.settings.http_timeout_seconds, transport=self.transport) as client:
                response = client.get(
                    "https://www.alphavantage.co/query",
                    params={
                        "function": function,
                        "apikey": self.settings.alpha_vantage_api_key,
                        **params,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderError("数据服务请求失败或响应无效，请检查网络及服务额度。") from None
        if not isinstance(data, dict):
            raise ProviderError("数据服务返回了不支持的格式。")
        if "Information" in data or "Note" in data:
            raise ProviderError("数据服务提示访问受限：可能达到频率/额度上限，或该接口需要付费权限。")
        if "Error Message" in data:
            raise ProviderError("数据服务拒绝了请求，请确认标的代码及接口权限。")
        return data

    def market(self, req: ResearchRequest) -> dict:
        data = self.fetch("TIME_SERIES_DAILY", symbol=req.symbol, outputsize="compact")
        series = data.get("Time Series (Daily)")
        if not isinstance(series, dict) or not series:
            raise ProviderError("未取得有效的日线行情。")
        now_ny = datetime.now(ZoneInfo("America/New_York"))
        last_allowed = min(req.as_of, now_ny.date())
        if last_allowed == now_ny.date() and now_ny.time() < time(16, 15):
            last_allowed -= timedelta(days=1)
        bars = []
        try:
            for day, row in sorted(series.items()):
                if date.fromisoformat(day) <= last_allowed:
                    bars.append(
                        Bar(
                            date=day,
                            open=float(row["1. open"]),
                            high=float(row["2. high"]),
                            low=float(row["3. low"]),
                            close=float(row["4. close"]),
                            volume=int(row["5. volume"]),
                        ).model_dump(mode="json")
                    )
        except (KeyError, TypeError, ValueError):
            raise ProviderError("行情质量检查失败：存在非法价格、成交量或日期。") from None
        bars = bars[-req.lookback_days :]
        if len(bars) < 20:
            raise ProviderError("截止日期前不足 20 条日线。当前接口只提供最近 100 个交易日，请选择近期日期。")
        warnings = ["行情为未复权价格，分红和拆股可能扭曲收益率与技术指标；不提供总回报。"]
        if len(bars) < req.lookback_days:
            warnings.append(f"请求 {req.lookback_days} 条日线，实际可用 {len(bars)} 条。")
        if (req.as_of - date.fromisoformat(bars[-1]["date"])).days > 5:
            warnings.append("最新行情距截止日期超过 5 个自然日，可能存在停牌或数据延迟。")
        ev = evidence(
            "market",
            f"{req.symbol} 未复权日线",
            "Alpha Vantage",
            bars[-1]["date"],
            bars,
            False,
            url="https://www.alphavantage.co/documentation/#daily",
            note="美国市场；USD；未复权；最新 100 条范围内筛选。",
        )
        return {
            "bars": bars,
            "currency": "USD",
            "timezone": "America/New_York",
            "adjustment": "raw",
            "evidence": [ev],
            "warnings": warnings,
        }

    def news(self, req: ResearchRequest) -> dict:
        start = datetime.combine(req.as_of - timedelta(days=30), time.min, tzinfo=UTC)
        cutoff = min(
            datetime.combine(req.as_of, time.max, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC),
            datetime.now(UTC),
        )
        data = self.fetch(
            "NEWS_SENTIMENT",
            tickers=req.symbol,
            time_from=start.strftime("%Y%m%dT%H%M"),
            time_to=cutoff.strftime("%Y%m%dT%H%M"),
            sort="LATEST",
            limit=30,
        )
        if not isinstance(data.get("feed"), list):
            raise ProviderError("未取得有效新闻列表。")
        items, evs, seen = [], [], set()
        skipped = 0
        for row in data["feed"]:
            try:
                published = datetime.strptime(row["time_published"], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
                title, url = str(row["title"]).strip(), str(row["url"])
                if not title or not url.startswith(("https://", "http://")):
                    skipped += 1
                    continue
                if published > cutoff or published < start or url in seen or title.casefold() in seen:
                    continue
                seen.update([url, title.casefold()])
                item = {
                    "title": title[:500],
                    "summary": str(row.get("summary", ""))[:1600],
                    "url": url,
                    "source": str(row.get("source", "未知来源"))[:200],
                    "published_at": published.isoformat(),
                    "sentiment": row.get("overall_sentiment_label"),
                }
                ev = evidence(
                    "news",
                    item["title"],
                    item["source"],
                    published.isoformat(),
                    item,
                    False,
                    url=url,
                    note="经 Alpha Vantage 聚合；未抓取或验证原文全文。",
                )
                item["evidence_id"] = ev["id"]
                items.append(item)
                evs.append(ev)
            except (KeyError, TypeError, ValueError):
                skipped += 1
        warnings = ["新闻摘要和情绪标签来自数据供应商；不是对原文事实的独立核验。"]
        if skipped:
            warnings.append(f"已跳过 {skipped} 条格式无效的新闻。")
        if not items:
            warnings.append("所选时间区间未找到可用新闻，不代表没有重大事件。")
        return {"items": items[:12], "evidence": evs[:12], "warnings": warnings}

    def macro(self, req: ResearchRequest) -> dict:
        # This feed supplies reference dates, not publication/vintage timestamps.
        if req.as_of < date.today():
            raise ProviderError(
                "历史截止日期的宏观分析已跳过：该数据源不提供当时可得的历史版本，无法避免前视偏差。"
            )
        data = self.fetch("FEDERAL_FUNDS_RATE", interval="monthly")
        if not isinstance(data.get("data"), list):
            raise ProviderError("未取得有效宏观数据。")
        items = []
        for row in data["data"]:
            try:
                d, value = date.fromisoformat(row["date"]), float(row["value"])
                if d <= req.as_of and math.isfinite(value):
                    items.append(
                        {"date": str(d), "value": value, "name": "月度有效联邦基金利率", "unit": "%"}
                    )
            except (ValueError, KeyError, TypeError):
                continue
        items = sorted(items, key=lambda x: x["date"], reverse=True)[:12]
        if not items:
            raise ProviderError("截止日期前无有效宏观观测值。")
        ev = evidence(
            "macro",
            "月度有效联邦基金利率",
            "Alpha Vantage / FRED",
            items[0]["date"],
            items,
            False,
            url="https://www.alphavantage.co/documentation/#interest-rate",
            note="观测期不是发布时间；当前版本可能包含历史修订。",
        )
        return {
            "items": items,
            "evidence": [ev],
            "warnings": [
                "宏观覆盖仅含月度有效联邦基金利率；不是央行目标利率，也不是完整宏观模型。",
                "采用当前可取得的修订版本，不能作为严格时点回测数据。",
            ],
        }


def provider_for(req: ResearchRequest, settings: Settings):
    from .public_market import PublicMarketProvider

    return DemoProvider() if req.mode == "demo" else PublicMarketProvider(settings)
