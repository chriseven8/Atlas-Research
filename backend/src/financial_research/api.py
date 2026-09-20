import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from . import __version__
from .domain import AGENT_NAMES, ResearchRequest
from .settings import Settings
from .storage import Repository
from .worker import run_worker


def create_app(settings: Settings | None = None, repository: Repository | None = None) -> FastAPI:
    settings = settings or Settings()
    repo = repository or Repository(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repo.initialize()
        stop = threading.Event()
        worker = None
        if settings.embed_worker:
            worker = threading.Thread(
                target=run_worker, args=(repo, settings, stop), daemon=True, name="research-worker"
            )
            worker.start()
        yield
        stop.set()
        if worker:
            await asyncio.to_thread(worker.join, 4)
        if not worker or not worker.is_alive():
            repo.close()

    app = FastAPI(
        title="Atlas Financial Research",
        version=__version__,
        lifespan=lifespan,
        description="本地单用户研究 API。演示模式不需要密钥；A 股及美股真实日线无需密钥；AI 研判需要单独配置。",
    )
    app.state.repository = repo

    @app.get("/api/health")
    def health():
        with repo.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    def config():
        return {
            "live_ready": settings.live_ready,
            "llm_ready": settings.llm_ready,
            "demo_symbols": ["AAPL", "MSFT", "NVDA", "SPY"],
            "agents": AGENT_NAMES,
            "max_lookback_days": 100,
            "version": __version__,
            "markets": ["CN", "US"],
            "market_provider": "腾讯财经公共行情",
            "alpha_vantage_ready": bool(settings.alpha_vantage_api_key),
        }

    @app.post("/api/research", status_code=202)
    def create_research(
        req: ResearchRequest, idempotency_key: Annotated[str | None, Header(max_length=128)] = None
    ):
        if req.use_llm and not settings.llm_ready:
            raise HTTPException(422, "AI 研判尚未配置，请设置 OPENAI_API_KEY 和 OPENAI_MODEL。")
        try:
            job_id, created = repo.create(req, idempotency_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"id": job_id, "created": created, "status_url": f"/api/research/{job_id}"}

    @app.get("/api/research")
    def list_research(
        limit: Annotated[int, Query(ge=1, le=100)] = 30, offset: Annotated[int, Query(ge=0)] = 0
    ):
        return {"items": repo.list(limit, offset)}

    def get_job(job_id: str):
        job = repo.get(job_id)
        if not job:
            raise HTTPException(404, "研究任务不存在。")
        return job

    @app.get("/api/research/{job_id}")
    def get_research(job_id: str):
        return get_job(job_id)

    @app.post("/api/research/{job_id}/cancel")
    def cancel_research(job_id: str):
        get_job(job_id)
        if not repo.cancel(job_id):
            raise HTTPException(409, "该任务已结束，无法取消。")
        return {"id": job_id, "status": "cancelled"}

    @app.get("/api/research/{job_id}/report.md", response_class=PlainTextResponse)
    def download_report(job_id: str):
        job = get_job(job_id)
        if job["status"] not in {"completed", "partial"} or not job["report"]:
            raise HTTPException(409, "报告尚未生成。")
        return PlainTextResponse(
            job["report"]["markdown"],
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="research-{job_id}.md"'},
        )

    @app.get("/api/research/{job_id}/report.json")
    def download_json(job_id: str):
        job = get_job(job_id)
        if job["status"] not in {"completed", "partial"} or not job["report"]:
            raise HTTPException(409, "报告尚未生成。")
        return job["report"]

    return app


app = create_app()
