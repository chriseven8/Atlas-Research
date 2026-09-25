"""Tencent public daily prices. No synthetic fallback; optional research feeds remain separate."""

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from .analytics import attribute_actions
from .cross_source import SINA_CN_URL, SINA_US_URL, SOURCE_NAME, cross_check, fetch_sina_bars
from .domain import Bar, ProviderError, ResearchRequest
from .providers import AlphaVantageProvider, evidence
from .settings import Settings


def _pct_text(value) -> str:
    return "不可得" if value is None else f"{value:+.2f}%"


def _attribution_note(attribution: dict) -> str:
    """把归因结论写成一句自解释的话：模型不该为了知道「降级了没」去翻 payload。"""
    if not attribution["adjusted_series_available"]:
        return "同源前复权对照序列本轮不可得，除权归因降级为不可得；未按公告比例反推当日影响。"
    parts = [
        f"{item['date']} 实测影响 {_pct_text(item['effect_pct'])}"
        + (f"（公告：{item['announced']}）" if item["announced"] else "（无对应公告记录）")
        for item in attribution["ex_dates"]
    ]
    if not parts:
        return "分析窗口内无公司行动，除权归因无适用对象。"
    move = attribution["largest_move"]
    if move:
        parts.append(
            f"窗口最大单日波动 {_pct_text(move['change_pct'])}（{move['date']}）"
            f"当日实测除权影响 {_pct_text(move['action_effect_pct'])}"
            + (f"，最近除权日 {move['nearest_ex_date']}" if move["nearest_ex_date"] else "，窗口内无除权日")
        )
    return "；".join(parts) + "。影响=前复权收益率−未复权收益率，为实测差值。"


def _cross_note(cross: dict) -> str:
    if cross["status"] == "unavailable":
        # 「没去查」与「查了没问题」必须在 note 里就分开，不能只靠 status 字段。
        return f"未完成第二行情源交叉校验：{cross.get('reason', '不可用')}。这不等于已校验通过。"
    return str(cross.get("note", ""))


def _bars_from_rows(rows, cutoff, lookback_days: int, multiplier: int) -> list[dict]:
    by_date = {}
    for row in rows:
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
    return [by_date[d] for d in sorted(by_date)][-lookback_days:]


def _corporate_actions(rows) -> list[dict]:
    """从未复权日线里抽出公司行动元数据。

    腾讯把除权信息直接内嵌在生效日那一行末尾（第 7 个元素起），形如
    {"cqr": "2026-04-30", "djr": "2026-04-29", "FHcontent": "10派10元"}，送转会写成
    「10派39.74元送8股转12股」。它和日线同源同区间，因此不必再引一个第三方公告接口，
    也不会出现两边区间对不上的情况。
    """
    actions = []
    for row in rows:
        extra = row[6] if len(row) > 6 else None
        if not isinstance(extra, dict) or not extra.get("cqr"):
            continue
        actions.append(
            {
                "ex_date": str(extra["cqr"]),
                "record_date": extra.get("djr"),
                "plan": extra.get("FHcontent"),
                "fiscal_year": extra.get("nd"),
            }
        )
    return actions


def _tencent_data(client, endpoint: str, ticker: str, start, as_of, adjust: str) -> dict:
    """取一只标的的日线节点（含 qt 元数据）。adjust 为空取未复权，'qfq' 取前复权。"""
    response = client.get(endpoint, params={"param": f"{ticker},day,{start},{as_of},640,{adjust}"})
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise ProviderError("行情供应商未返回有效数据，请稍后重试。")
    return payload.get("data", {}).get(ticker, {})


class PublicMarketProvider:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport

    def market(self, req: ResearchRequest) -> dict:
        cn = req.market == "CN"
        currency, timezone = ("CNY", "Asia/Shanghai") if cn else ("USD", "America/New_York")
        adjusted, adjusted_rows, actions = [], [], []
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
                # 取数区间必须比展示区间长：300 个交易日约合 440 个自然日。留 1.6 倍再加
                # 缓冲，同时不低于原来的 400 天，避免短区间标的因缩窗而少取到交易日。
                start = req.as_of - timedelta(days=max(400, int(req.lookback_days * 1.6)))
                data = _tencent_data(client, endpoint, ticker, start, req.as_of, "" if cn else "qfq")
                rows = data.get("day" if cn else "qfqday", [])
                if cn:
                    metadata = data.get("qt", {}).get(ticker, [])
                    if len(metadata) > 1:
                        name = metadata[1]
                    # 同源同区间再取一份前复权序列。它与未复权序列只差公司行动的缩放，
                    # 相减即可实测出除权当天的价格影响，不必依赖公告比例反推。
                    adjusted_rows = _tencent_data(client, endpoint, ticker, start, req.as_of, "qfq").get(
                        "qfqday", []
                    )
            now = datetime.now(ZoneInfo(timezone))
            cutoff = min(req.as_of, now.date())
            if cutoff == now.date() and now.time() < (time(15, 15) if cn else time(16, 15)):
                cutoff -= timedelta(days=1)
            # Tencent A shares normally use lots; STAR market and US use shares.
            multiplier = 100 if cn and not req.symbol.startswith(("688", "689")) else 1
            bars = _bars_from_rows(rows, cutoff, req.lookback_days, multiplier)
            if len(bars) < 20:
                raise ProviderError(
                    "有效日线不足 20 条，可能是新上市、停牌或供应商历史覆盖不足；未使用演示数据替代。"
                )
            if adjusted_rows:
                candidate = _bars_from_rows(adjusted_rows, cutoff, req.lookback_days, multiplier)
                # 对照序列必须与主序列落在同一批日期上，否则差值会把「缺了某天」
                # 误报成一次公司行动。
                adjusted = candidate if [b["date"] for b in candidate] == [b["date"] for b in bars] else []
            # 只保留落在被分析窗口内的除权日。取数区间比展示区间长（400 天 vs
            # lookback_days），不过滤就会列出窗口外的分红，让模型拿窗口外的除权日
            # 去解释窗口内的跳变。
            window = {bar["date"] for bar in bars}
            actions = [a for a in _corporate_actions(rows) if a["ex_date"] in window] if cn else []
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("真实行情获取或校验失败，请检查网络并稍后重试。") from None
        adjustment = "raw" if cn else "qfq"
        warnings = [
            "公共行情可能延迟；仅分析已收盘日线，不提供逐笔实时行情。",
            "成交量已统一为股；不计算含分红再投资的总回报。",
        ]
        if not cn:
            warnings.append(
                "美股采用供应商当前版本前复权日线，历史值可能随公司行动修订，不适用于严格时点回测。"
            )
        elif adjusted:
            warnings.append("A 股日线未复权；已取同源前复权序列做除权归因对照，除权日与当日影响见除权归因。")
        else:
            # 复权序列取不到不构成失败：归因降级为不可得，主序列照常出报告。
            warnings.append("A 股日线未复权，同源前复权对照序列本轮不可得，除权归因降级为不可得。")
        if (req.as_of - datetime.fromisoformat(bars[-1]["date"]).date()).days > 7:
            warnings.append("最近行情距截止日超过 7 天，可能停牌、休市或数据陈旧。")

        try:
            # 比对起点放到最近一次公司行动之后：前复权把生效日之前的历史价格整体
            # 缩放过，两源在除权日之前必然成比例错开，那是口径不同而非数据出错。
            cross = cross_check(
                bars,
                fetch_sina_bars(req, self.settings, self.transport),
                only_after=actions[-1]["ex_date"] if actions else None,
            )
        except ProviderError as exc:
            cross = {"status": "unavailable", "reason": str(exc)}
        if cross["status"] == "mismatch":
            warnings.append("第二行情源交叉校验发现不一致，需人工核查后再使用行情结论。")
        elif cross["status"] == "unavailable":
            warnings.append(f"未完成第二行情源交叉校验：{cross.get('reason', '不可用')}")
        # 除权日公告（取自未复权日线内嵌的除权信息）与逐日归因。归因的数值计算
        # 在 analytics.attribute_actions，这里只负责取数与把两份序列对齐。
        attribution = attribute_actions(
            [Bar.model_validate(b) for b in bars],
            [Bar.model_validate(b) for b in adjusted],
            actions,
        )
        data_evidence = [
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
        ]
        # 除权归因与交叉校验各给一条独立证据：只写在 market 节点的上下文里时，
        # 模型引不到任何 evidence_id，只能把它当成「未提供的数据」写进待解问题。
        # 两条都只在 A 股侧生成除权归因——美股主序列本身就是前复权，归因无适用对象。
        if cn:
            data_evidence.append(
                evidence(
                    "market",
                    f"{req.symbol} 除权归因（未复权主序列 vs 同源前复权对照）",
                    "腾讯财经公共行情",
                    bars[-1]["date"],
                    {"attribution": attribution, "corporate_actions": actions},
                    False,
                    url=endpoint,
                    note=_attribution_note(attribution),
                )
            )
        cross_end = (cross.get("date_range") or [bars[-1]["date"]])[-1]
        data_evidence.append(
            evidence(
                "market",
                f"{req.symbol} 第二行情源交叉校验（{SOURCE_NAME}）",
                SOURCE_NAME,
                cross_end,
                cross,
                False,
                url=SINA_CN_URL if cn else SINA_US_URL,
                note=_cross_note(cross),
            )
        )
        return {
            "bars": bars,
            "name": name,
            "market": req.market,
            "currency": currency,
            "timezone": timezone,
            "adjustment": adjustment,
            "provider": "腾讯财经",
            "corporate_actions": actions,
            "action_attribution": attribution,
            "cross_check": cross,
            "warnings": warnings,
            "evidence": data_evidence,
        }

    def news(self, req: ResearchRequest) -> dict:
        if req.market == "CN":
            from .china_news import fetch_china_news

            return fetch_china_news(req, self.settings, self.transport)
        return AlphaVantageProvider(self.settings, self.transport).news(req)

    def macro(self, req: ResearchRequest) -> dict:
        if req.market == "CN":
            from .china_macro import fetch_china_macro

            return fetch_china_macro(req, self.settings, self.transport)
        return AlphaVantageProvider(self.settings, self.transport).macro(req)
