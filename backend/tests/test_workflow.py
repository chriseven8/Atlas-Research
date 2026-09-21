import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from financial_research.domain import LeaseLost, ProviderError, ResearchRequest
from financial_research.providers import DemoProvider
from financial_research.storage import Repository, jobs
from financial_research.worker import execute_job
from financial_research.workflow import ResearchWorkflow


def create_claim(repo, **kwargs):
    req = ResearchRequest(as_of="2026-06-10", **kwargs)
    job_id, _ = repo.create(req)
    return job_id, repo.claim()


@pytest.mark.parametrize("symbol,lookback", [("AAPL", 90), ("MSFT", 60), ("NVDA", 30), ("SPY", 100)])
def test_complete_pipeline_and_evidence_integrity(repo, settings, symbol, lookback):
    job_id, job = create_claim(repo, symbol=symbol, lookback_days=lookback)
    execute_job(repo, settings, job)
    result = repo.get(job_id)
    assert result["status"] == "completed", result["error"]
    assert len(result["agents"]) == 7
    report = result["report"]
    assert len(report["chart"]) == lookback
    assert report["mode"] == "demo" and "合成" in report["summary"]
    assert all(e["is_demo"] for e in report["evidence"])
    evidence_ids = {e["id"] for e in report["evidence"]}
    assert all(set(c["evidence_ids"]) <= evidence_ids for c in report["claims"])
    assert "SHA256" in report["markdown"]
    assert report["ai_synthesis"] is None


def test_optional_provider_failure_produces_partial_report(repo, settings):
    class PartialProvider(DemoProvider):
        def news(self, req):
            raise ProviderError("新闻额度不足")

    job_id, job = create_claim(repo)
    execute_job(repo, settings, job, PartialProvider())
    result = repo.get(job_id)
    assert result["status"] == "partial"
    assert result["report"]["news"] == []
    assert "新闻额度不足" in result["report"]["limitations"]


def test_market_failure_is_fatal(repo, settings):
    class BrokenProvider(DemoProvider):
        def market(self, req):
            raise ProviderError("行情不可用")

    job_id, job = create_claim(repo)
    execute_job(repo, settings, job, BrokenProvider())
    result = repo.get(job_id)
    assert result["status"] == "failed"
    assert result["report"] is None
    assert result["error"] == "行情不可用"


def test_expired_lease_resumes_saved_node_without_repeating(repo, settings):
    job_id, old = create_claim(repo)
    workflow = ResearchWorkflow(settings, repo, old)
    workflow.wrap("manager")({})
    workflow.wrap("market")({})
    with repo.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(lease_until=time.time() - 10))
    fresh_repo = Repository(settings)
    new = fresh_repo.claim()
    assert new["owner"] != old["owner"]
    with pytest.raises(LeaseLost):
        repo.finish(job_id, old["owner"], "completed")

    class CachedMarketProvider(DemoProvider):
        def market(self, req):
            raise AssertionError("A completed market node must not run twice")

    execute_job(fresh_repo, settings, new, CachedMarketProvider())
    result = fresh_repo.get(job_id)
    assert result["status"] == "completed", result["error"]
    assert result["attempts"] == 2
    assert (
        len([e for e in result["events"] if e["kind"] == "agent_started" and e["message"] == "market"]) == 1
    )
    fresh_repo.close()


def test_concurrent_workers_only_claim_once(repo):
    job_id, _ = repo.create(ResearchRequest())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: repo.claim(), range(4)))
    assert len([x for x in results if x]) == 1
    assert next(x for x in results if x)["id"] == job_id


def test_cancel_fences_inflight_results(repo):
    job_id, job = create_claim(repo)
    repo.start_node(job_id, job["owner"], "manager")
    assert repo.cancel(job_id)
    assert not repo.cancel(job_id)
    with pytest.raises(LeaseLost):
        repo.finish_node(job_id, job["owner"], "manager", {"status": "completed"}, 1)
    assert repo.get(job_id)["status"] == "cancelled"


def test_timeout_does_not_continue_work(repo, settings):
    job_id, job = create_claim(repo)
    with repo.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(started_at=time.time() - 10000))
    execute_job(repo, settings, job)
    assert repo.get(job_id)["status"] == "failed"
    assert "时间预算" in repo.get(job_id)["error"]


def test_model_call_budget_is_durable(repo):
    # 预算可持久化：按实际配置的预算耗尽后，超限调用不再放行。
    budget = repo.settings.max_llm_calls
    job_id, job = create_claim(repo)
    for _ in range(budget):
        assert repo.reserve_call(job_id, job["owner"])
    assert repo.reserve_call(job_id, job["owner"]) is None
    assert repo.get(job_id)["llm_calls"] == budget


def test_recovery_attempt_limit(repo, settings):
    job_id, job = create_claim(repo)
    with repo.engine.begin() as conn:
        conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(attempts=settings.max_task_attempts, lease_until=time.time() - 10)
        )
    assert repo.claim() is None
    assert repo.get(job_id)["status"] == "failed"
