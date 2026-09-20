import json

import httpx

from financial_research.china_news import fetch_china_news
from financial_research.domain import ResearchRequest


def test_news_notices_filter_deduplicate_and_cite(settings):
    news = {
        "title": "<em>600519</em> 新闻",
        "content": "600519 测试摘要",
        "date": "2026-06-01 12:00:00",
        "code": "202606011234567890",
        "mediaName": "测试媒体",
    }
    notice = {
        "title": "公司公告",
        "art_code": "AN202605311234567890",
        "codes": [{"stock_code": "600519"}],
        "notice_date": "2026-06-01 00:00:00",
        "display_time": "2026-05-31 19:00:00:123",
    }

    def handler(r):
        if "search" in r.url.host:
            rows = [news, news, {**news, "date": "2026-06-02 12:00:00"}, {**news, "code": "bad"}]
            return httpx.Response(
                200, text=r.url.params["cb"] + "(" + json.dumps({"result": {"cmsArticleWebOld": rows}}) + ")"
            )
        return httpx.Response(
            200,
            json={"success": 1, "data": {"list": [notice, {**notice, "codes": [{"stock_code": "000001"}]}]}},
        )

    result = fetch_china_news(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-01"),
        settings,
        httpx.MockTransport(handler),
    )
    assert result["status"] == "completed"
    assert len(result["items"]) == 2
    assert {i["category"] for i in result["items"]} == {"news", "announcement"}
    assert "<em>" not in result["items"][0]["title"]
    assert all(not e["is_demo"] for e in result["evidence"])
    assert {i["evidence_id"] for i in result["items"]} == {e["id"] for e in result["evidence"]}


def test_one_feed_failure_preserves_other_evidence(settings):
    def handler(r):
        if "search" in r.url.host:
            return httpx.Response(200, text="")
        return httpx.Response(
            200,
            json={
                "success": 1,
                "data": {
                    "list": [
                        {
                            "title": "公告",
                            "art_code": "AN202605311234567890",
                            "codes": [{"stock_code": "600519"}],
                            "notice_date": "2026-05-31 00:00:00",
                        }
                    ]
                },
            },
        )

    result = fetch_china_news(
        ResearchRequest(market="CN", symbol="600519", mode="live", as_of="2026-06-01"),
        settings,
        httpx.MockTransport(handler),
    )
    assert result["status"] == "partial"
    assert len(result["items"]) == 1
    assert result["feeds"]["news"] == "unavailable"
    assert result["items"][0]["time_precision"] == "date"


def test_all_sources_fail_without_fake_data(settings):
    result = fetch_china_news(
        ResearchRequest(market="CN", symbol="600519", mode="live"),
        settings,
        httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    assert result["status"] == "partial"
    assert result["items"] == result["evidence"] == []
