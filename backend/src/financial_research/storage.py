import time
import uuid
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    event,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from .domain import LeaseLost, ResearchRequest, utcnow
from .settings import Settings

metadata = MetaData()
jobs = Table(
    "research_jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("request", JSON, nullable=False),
    Column("status", String(24), nullable=False, index=True),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("started_at", Float),
    Column("finished_at", String(40)),
    Column("owner", String(36)),
    Column("lease_until", Float),
    Column("attempts", Integer, nullable=False, default=0),
    Column("llm_calls", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("idempotency_key", String(128), unique=True),
)
nodes = Table(
    "agent_runs",
    metadata,
    Column("job_id", String(36), primary_key=True),
    Column("name", String(24), primary_key=True),
    Column("status", String(24), nullable=False),
    Column("started_at", String(40), nullable=False),
    Column("finished_at", String(40)),
    Column("duration_ms", Integer),
    Column("output", JSON),
    Column("error", Text),
)
events = Table(
    "research_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String(36), nullable=False, index=True),
    Column("created_at", String(40), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("message", Text, nullable=False),
)
calls = Table(
    "model_calls",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("job_id", String(36), nullable=False, index=True),
    Column("ordinal", Integer, nullable=False),
    Column("status", String(24), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("usage", JSON),
    Column("error", Text),
    UniqueConstraint("job_id", "ordinal"),
)


class Repository:
    def __init__(self, settings: Settings):
        self.settings = settings
        options = {"pool_pre_ping": True}
        if settings.database_url.startswith("sqlite"):
            from sqlalchemy.engine import make_url

            db = make_url(settings.database_url).database
            if db and db != ":memory:":
                Path(db).parent.mkdir(parents=True, exist_ok=True)
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(settings.database_url, **options)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _):
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA busy_timeout=30000")

    def initialize(self):
        # Fresh installations only; versioned upgrades are in backend/migrations.
        metadata.create_all(self.engine)

    def close(self):
        self.engine.dispose()

    def _event(self, conn, job_id: str, kind: str, message: str):
        conn.execute(insert(events).values(job_id=job_id, kind=kind, message=message, created_at=utcnow()))

    def _fence(self, conn, job_id: str, owner: str):
        changed = conn.execute(
            update(jobs)
            .where(
                jobs.c.id == job_id,
                jobs.c.owner == owner,
                jobs.c.status == "running",
                jobs.c.lease_until > time.time(),
            )
            .values(updated_at=utcnow())
        )
        if changed.rowcount != 1:
            raise LeaseLost("任务已取消或执行租约已失效")

    def create(self, req: ResearchRequest, idempotency_key: str | None = None) -> tuple[str, bool]:
        payload = req.model_dump(mode="json")
        job_id = str(uuid.uuid4())
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(jobs).values(
                        id=job_id,
                        request=payload,
                        status="queued",
                        created_at=utcnow(),
                        updated_at=utcnow(),
                        attempts=0,
                        llm_calls=0,
                        idempotency_key=idempotency_key,
                    )
                )
                self._event(conn, job_id, "queued", "研究任务已进入队列")
            return job_id, True
        except IntegrityError:
            if not idempotency_key:
                raise
            with self.engine.connect() as conn:
                existing = (
                    conn.execute(select(jobs).where(jobs.c.idempotency_key == idempotency_key))
                    .mappings()
                    .one()
                )
            if existing["request"] != payload:
                raise ValueError("同一个 Idempotency-Key 不能用于不同请求") from None
            return existing["id"], False

    def claim(self) -> dict | None:
        now = time.time()
        available = or_(jobs.c.status == "queued", and_(jobs.c.status == "running", jobs.c.lease_until < now))
        with self.engine.begin() as conn:
            candidates = (
                conn.execute(select(jobs).where(available).order_by(jobs.c.created_at).limit(8))
                .mappings()
                .all()
            )
            for row in candidates:
                owner = str(uuid.uuid4())
                if row["attempts"] >= self.settings.max_task_attempts:
                    changed = conn.execute(
                        update(jobs)
                        .where(jobs.c.id == row["id"], available)
                        .values(
                            status="failed",
                            error="多次中断后仍无法完成任务，请重新发起研究。",
                            finished_at=utcnow(),
                            owner=None,
                        )
                    )
                    if changed.rowcount:
                        self._event(conn, row["id"], "failed", "超过最大恢复次数")
                    continue
                changed = conn.execute(
                    update(jobs)
                    .where(jobs.c.id == row["id"], available)
                    .values(
                        status="running",
                        owner=owner,
                        lease_until=now + self.settings.lease_seconds,
                        attempts=jobs.c.attempts + 1,
                        updated_at=utcnow(),
                        started_at=row["started_at"] or now,
                    )
                )
                if changed.rowcount:
                    self._event(
                        conn,
                        row["id"],
                        "running",
                        "开始研究" if not row["attempts"] else "恢复已保存的节点结果",
                    )
                    return dict(conn.execute(select(jobs).where(jobs.c.id == row["id"])).mappings().one())
        return None

    def heartbeat(self, job_id: str, owner: str) -> bool:
        with self.engine.begin() as conn:
            changed = conn.execute(
                update(jobs)
                .where(
                    jobs.c.id == job_id,
                    jobs.c.owner == owner,
                    jobs.c.status == "running",
                    jobs.c.lease_until > time.time(),
                )
                .values(
                    lease_until=time.time() + self.settings.lease_seconds,
                    updated_at=utcnow(),
                )
            )
            return changed.rowcount == 1

    def check_active(self, job_id: str, owner: str):
        with self.engine.connect() as conn:
            row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().one()
        if row["status"] != "running" or row["owner"] != owner or row["lease_until"] <= time.time():
            raise LeaseLost("任务已取消或执行租约已失效")
        if time.time() - row["started_at"] > self.settings.task_timeout_seconds:
            raise TimeoutError("研究任务超过时间预算，已停止后续分析。")

    def start_node(self, job_id: str, owner: str, name: str) -> dict | None:
        with self.engine.begin() as conn:
            self._fence(conn, job_id, owner)
            row = (
                conn.execute(select(nodes).where(nodes.c.job_id == job_id, nodes.c.name == name))
                .mappings()
                .first()
            )
            if row and row["status"] in {"completed", "partial", "skipped"}:
                return row["output"]
            values = {
                "status": "running",
                "started_at": utcnow(),
                "finished_at": None,
                "duration_ms": None,
                "output": None,
                "error": None,
            }
            if row:
                conn.execute(
                    update(nodes).where(nodes.c.job_id == job_id, nodes.c.name == name).values(**values)
                )
            else:
                conn.execute(insert(nodes).values(job_id=job_id, name=name, **values))
            self._event(conn, job_id, "agent_started", name)
        return None

    def finish_node(self, job_id: str, owner: str, name: str, output: dict, duration_ms: int):
        with self.engine.begin() as conn:
            self._fence(conn, job_id, owner)
            status = output.get("status", "completed")
            conn.execute(
                update(nodes)
                .where(nodes.c.job_id == job_id, nodes.c.name == name)
                .values(
                    status=status,
                    finished_at=utcnow(),
                    output=output,
                    duration_ms=duration_ms,
                )
            )
            self._event(conn, job_id, "agent_finished", f"{name}: {status}")

    def fail_node(self, job_id: str, owner: str, name: str, error: str):
        with self.engine.begin() as conn:
            self._fence(conn, job_id, owner)
            conn.execute(
                update(nodes)
                .where(nodes.c.job_id == job_id, nodes.c.name == name)
                .values(
                    status="failed",
                    finished_at=utcnow(),
                    error=error,
                )
            )
            self._event(conn, job_id, "agent_failed", f"{name}: {error}")

    def finish(self, job_id: str, owner: str, status: str, error: str | None = None):
        with self.engine.begin() as conn:
            self._fence(conn, job_id, owner)
            if status == "failed":
                conn.execute(
                    update(nodes)
                    .where(nodes.c.job_id == job_id, nodes.c.status == "running")
                    .values(status="failed", finished_at=utcnow(), error="任务已终止，节点未完成。")
                )
            conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id)
                .values(
                    status=status,
                    error=error,
                    finished_at=utcnow(),
                    lease_until=None,
                    owner=None,
                )
            )
            self._event(conn, job_id, status, error or "研究报告已保存")

    def cancel(self, job_id: str) -> bool:
        with self.engine.begin() as conn:
            changed = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status.in_(["queued", "running"]))
                .values(
                    status="cancelled",
                    owner=None,
                    lease_until=None,
                    finished_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            if changed.rowcount:
                conn.execute(
                    update(nodes)
                    .where(nodes.c.job_id == job_id, nodes.c.status == "running")
                    .values(
                        status="cancelled",
                        finished_at=utcnow(),
                    )
                )
                self._event(conn, job_id, "cancelled", "任务已取消；已发出的外部请求可能仍会计费")
            return bool(changed.rowcount)

    def reserve_call(self, job_id: str, owner: str) -> str | None:
        with self.engine.begin() as conn:
            self._fence(conn, job_id, owner)
            count = conn.execute(select(jobs.c.llm_calls).where(jobs.c.id == job_id)).scalar_one()
            if count >= self.settings.max_llm_calls:
                return None
            call_id = str(uuid.uuid4())
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(llm_calls=count + 1))
            conn.execute(
                insert(calls).values(
                    id=call_id, job_id=job_id, ordinal=count + 1, status="reserved", created_at=utcnow()
                )
            )
            return call_id

    def finish_call(self, call_id: str, usage: dict | None = None, error: str | None = None):
        # Usage may arrive after cancellation; retain the accounting without altering the job.
        with self.engine.begin() as conn:
            conn.execute(
                update(calls)
                .where(calls.c.id == call_id)
                .values(
                    status="failed" if error else "completed",
                    usage=usage,
                    error=error,
                )
            )

    def get(self, job_id: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(jobs).where(jobs.c.id == job_id)).mappings().first()
            if row is None:
                return None
            result = {
                k: v for k, v in dict(row).items() if k not in {"owner", "lease_until", "idempotency_key"}
            }
            result["agents"] = [
                dict(r) for r in conn.execute(select(nodes).where(nodes.c.job_id == job_id)).mappings()
            ]
            result["events"] = [
                dict(r)
                for r in conn.execute(
                    select(events).where(events.c.job_id == job_id).order_by(events.c.id)
                ).mappings()
            ]
            result["model_calls"] = [
                dict(r) for r in conn.execute(select(calls).where(calls.c.job_id == job_id)).mappings()
            ]
            report = next(
                (a["output"] for a in result["agents"] if a["name"] == "report" and a["output"]), None
            )
            result["report"] = report
            return result

    def list(self, limit: int = 30, offset: int = 0) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    jobs.c.id,
                    jobs.c.request,
                    jobs.c.status,
                    jobs.c.created_at,
                    jobs.c.finished_at,
                    jobs.c.error,
                )
                .order_by(jobs.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return [dict(r) for r in rows.mappings()]
