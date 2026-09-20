from fastapi.testclient import TestClient

from financial_research.api import create_app
from financial_research.worker import execute_job


def test_api_end_to_end_and_downloads(repo, settings):
    with TestClient(create_app(settings, repo)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        config = client.get("/api/config").json()
        assert config["live_ready"] is True
        assert config["markets"] == ["CN", "US"]
        assert "api_key" not in str(config)
        response = client.post("/api/research", json={"symbol": "aapl", "as_of": "2026-06-10"})
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert client.get(f"/api/research/{job_id}/report.md").status_code == 409
        execute_job(repo, settings, repo.claim())
        result = client.get(f"/api/research/{job_id}").json()
        assert result["status"] == "completed"
        assert "owner" not in result
        download = client.get(f"/api/research/{job_id}/report.md")
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]
        assert "合成演示" in download.text
        assert client.get(f"/api/research/{job_id}/report.json").json()["symbol"] == "AAPL"
        assert len(client.get("/api/research").json()["items"]) == 1
        assert client.get("/api/research/not-found").status_code == 404


def test_idempotency_and_request_validation(repo, settings):
    with TestClient(create_app(settings, repo)) as client:
        payload = {"symbol": "AAPL", "as_of": "2026-06-10"}
        first = client.post("/api/research", json=payload, headers={"Idempotency-Key": "same"})
        second = client.post("/api/research", json=payload, headers={"Idempotency-Key": "same"})
        assert first.json()["id"] == second.json()["id"]
        assert second.json()["created"] is False
        assert (
            client.post(
                "/api/research", json={**payload, "symbol": "MSFT"}, headers={"Idempotency-Key": "same"}
            ).status_code
            == 409
        )
        for invalid in [
            {"symbol": "BTC"},
            {"as_of": "2099-01-01"},
            {"lookback_days": 500},
            {"symbol": "<script>"},
            {"question": "  "},
            {"unexpected": 1},
            {"use_llm": True},
        ]:
            assert client.post("/api/research", json=invalid).status_code == 422


def test_cancel_pending_job(repo, settings):
    with TestClient(create_app(settings, repo)) as client:
        job_id = client.post("/api/research", json={}).json()["id"]
        assert client.post(f"/api/research/{job_id}/cancel").status_code == 200
        assert repo.claim() is None
        assert client.post(f"/api/research/{job_id}/cancel").status_code == 409


def test_embedded_worker_finishes_task(repo, settings):
    import time

    settings.embed_worker = True
    with TestClient(create_app(settings, repo)) as client:
        job_id = client.post("/api/research", json={}).json()["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get(f"/api/research/{job_id}").json()
            if result["status"] == "completed":
                break
            time.sleep(0.05)
        assert result["status"] == "completed", result


def test_live_markets_do_not_require_keys(repo, settings):
    with TestClient(create_app(settings, repo)) as client:
        for market, symbol in [("CN", "600519"), ("US", "AAPL")]:
            response = client.post("/api/research", json={"market": market, "symbol": symbol, "mode": "live"})
            assert response.status_code == 202
