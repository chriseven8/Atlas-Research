"""Initial schema. IF NOT EXISTS also adopts an unversioned local v0.1 database."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("updated_at", sa.String(40), nullable=False),
        sa.Column("started_at", sa.Float()),
        sa.Column("finished_at", sa.String(40)),
        sa.Column("owner", sa.String(36)),
        sa.Column("lease_until", sa.Float()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("llm_calls", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        if_not_exists=True,
    )
    op.create_table(
        "agent_runs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(24), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.String(40), nullable=False),
        sa.Column("finished_at", sa.String(40)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("output", sa.JSON()),
        sa.Column("error", sa.Text()),
        if_not_exists=True,
    )
    op.create_table(
        "research_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.String(40), nullable=False),
        sa.Column("usage", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("job_id", "ordinal"),
        if_not_exists=True,
    )
    for table, column in [
        ("research_jobs", "status"),
        ("research_events", "job_id"),
        ("model_calls", "job_id"),
    ]:
        op.create_index(f"ix_{table}_{column}", table, [column], if_not_exists=True)


def downgrade():
    for table in ["model_calls", "research_events", "agent_runs", "research_jobs"]:
        op.drop_table(table)
