"""Public Eastmoney search snippets and issuer notice metadata, not full articles."""

import html
import json
import re
import time
from datetime import datetime, timedelta
from datetime import time as daytime
from zoneinfo import ZoneInfo

import httpx

from .providers import evidence

SHANGHAI = ZoneInfo("Asia/Shanghai")


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()


def fetch_china_news(req, settings, transport=None):
    code = req.symbol.split(".")[0]
    now = datetime.now(SHANGHAI)
    cutoff = min(datetime.combine(req.as_of, daytime.max, SHANGHAI), now)
    start = datetime.combine(req.as_of - timedelta(days=90), daytime.min, SHANGHAI)
    items, warnings, feeds = [], [], {}
    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        transport=transport,
        headers={"User-Agent": "AtlasResearch/0.1"},
    ) as client:
        for feed in ("news", "announcement"):
            try:
                if feed == "news":
                    callback = "jQuery35101792940631092459_" + str(int(time.time() * 1000))
                    params = {
                        "uid": "",
                        "keyword": code,
                        "type": ["cmsArticleWebOld"],
                        "client": "web",
                        "clientType": "web",
                        "clientVersion": "curr",
                        "param": {
                            "cmsArticleWebOld": {
                                "searchScope": "default",
                                "sort": "default",
                                "pageIndex": 1,
                                "pageSize": 20,
                                "preTag": "<em>",
                                "postTag": "</em>",
                            }
                        },
                    }
                    response = client.get(
                        "https://search-api-web.eastmoney.com/search/jsonp",
                        params={
                            "cb": callback,
                            "param": json.dumps(params),
                            "_": str(int(time.time() * 1000)),
                        },
                        headers={"Referer": f"https://so.eastmoney.com/news/s?keyword={code}"},
                    )
                    response.raise_for_status()
                    raw = response.text.strip().rstrip(";")
                    if raw.startswith(callback + "(") and raw.endswith(")"):
                        raw = raw[len(callback) + 1 : -1]
                    payload = json.loads(raw)
                    rows = payload["result"]["cmsArticleWebOld"]
                else:
                    response = client.get(
                        "https://np-anotice-stock.eastmoney.com/api/security/ann",
                        params={
                            "sr": "-1",
                            "page_size": 50,
                            "page_index": 1,
                            "ann_type": "A",
                            "client_source": "web",
                            "f_node": "0",
                            "s_node": "0",
                            "stock_list": code,
                            "begin_time": str(start.date()),
                            "end_time": str(req.as_of),
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("success") != 1:
                        raise ValueError("upstream error")
                    rows = payload["data"]["list"]
                if not isinstance(rows, list):
                    raise ValueError("invalid list")
                accepted, skipped = [], 0
                for row in rows:
                    try:
                        title = clean(row["title"])[:500]
                        if feed == "news":
                            published = datetime.strptime(row["date"], "%Y-%m-%d %H:%M:%S").replace(
                                tzinfo=SHANGHAI
                            )
                            article = str(row["code"])
                            if not re.fullmatch(r"\d{10,30}", article):
                                raise ValueError("invalid article")
                            url = f"https://finance.eastmoney.com/a/{article}.html"
                            summary = clean(row.get("content"))[:1600] or "仅取得标题，请打开原文核实。"
                            source = (clean(row.get("mediaName")) or "未知媒体") + "（东方财富检索）"
                            precision = "second"
                        else:
                            if not any(c.get("stock_code") == code for c in row.get("codes", [])):
                                continue
                            article = str(row["art_code"])
                            if not re.fullmatch(r"AN\d{10,30}", article):
                                raise ValueError("invalid notice")
                            # Use display timestamp where present; a date alone is conservatively end-of-day.
                            stamp = row.get("display_time")
                            published = (
                                datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
                                if stamp
                                else datetime.combine(
                                    datetime.fromisoformat(row["notice_date"]).date(), daytime.max, SHANGHAI
                                )
                            )
                            precision = "second" if stamp else "date"
                            url = f"https://data.eastmoney.com/notices/detail/{code}/{article}.html"
                            summary = (
                                "公司公告索引：仅获取标题与发布信息，未读取公告正文；请打开公告核实具体内容。"
                            )
                            source = "公司公告（东方财富披露索引）"
                        if title and start <= published <= cutoff:
                            accepted.append(
                                {
                                    "title": title,
                                    "summary": summary,
                                    "source": source,
                                    "url": url,
                                    "published_at": published.isoformat(),
                                    "category": feed,
                                    "time_precision": precision,
                                    "sentiment": None,
                                }
                            )
                    except (ValueError, KeyError, TypeError, AttributeError):
                        skipped += 1
                items.extend(sorted(accepted, key=lambda x: x["published_at"], reverse=True)[:8])
                feeds[feed] = "available" if accepted else "empty"
                if not accepted:
                    warnings.append(
                        f"{'个股新闻' if feed == 'news' else '公司公告'}：近 90 天窗口内未取得有效条目，不代表没有事件。"
                    )
                if skipped:
                    warnings.append(f"{feed} 已跳过 {skipped} 条格式无效记录。")
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
                feeds[feed] = "unavailable"
                warnings.append(
                    f"{'东方财富个股新闻' if feed == 'news' else '东方财富公司公告'}暂时不可用；未以虚构内容替代。"
                )
    unique = {}
    titles = set()
    for item in sorted(items, key=lambda x: x["published_at"], reverse=True):
        key = (item["category"], item["title"].casefold())
        if item["url"] not in unique and key not in titles:
            unique[item["url"]] = item
            titles.add(key)
    items = list(unique.values())
    evs = []
    note = "近 90 天有限检索结果；新闻为搜索片段、公告为索引，未读取或核实全文。历史检索不保证完整覆盖。"
    for item in items:
        ev = evidence(
            "news",
            item["title"],
            item["source"],
            item["published_at"],
            item.copy(),
            False,
            url=item["url"],
            note=note,
        )
        item["evidence_id"] = ev["id"]
        evs.append(ev)
    warnings.append(note)
    return {
        "items": items,
        "evidence": evs,
        "warnings": warnings,
        "feeds": feeds,
        "status": "completed" if items and "unavailable" not in feeds.values() else "partial",
    }
