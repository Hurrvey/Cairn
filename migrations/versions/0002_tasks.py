"""tasks: durable queue and per-workspace fairness counter

Revision ID: 0002_tasks
Revises: 0001_baseline
Create Date: 2026-08-31

ADR-0003. The two indexes here carry the design:

* ``ix_task_claim`` is PARTIAL on ``state = 'ready'``. The hot set stays small
  no matter how many completed rows accumulate, so claim latency does not
  degrade as the table grows.
* ``uq_task_dedupe`` is partial on the live states, so the same dedupe key can
  be reused once the previous task finishes — which is what makes
  ``{kind}:{entity}:{revision}`` a usable convention rather than a one-shot.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_tasks"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUEUES = "('fetch','parse','ocr','chunk','embed','index','maintain')"
STATES = "('ready','running','done','failed','blocked','cancelled')"


def upgrade() -> None:
    op.create_table(
        "workspace_runtime",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("running", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("concurrency_limit", sa.Integer(), nullable=False, server_default="16"),
        sa.PrimaryKeyConstraint("workspace_id", name="pk_workspace_runtime"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name="fk_workspace_runtime_workspace_id_workspace",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("running >= 0", name="ck_workspace_runtime_running_nonneg"),
    )

    op.create_table(
        "task",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("queue", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="100"),
        sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.SmallInteger(), nullable=False, server_default="5"),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("dedupe_key", sa.String(255), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("progress_done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_task"),
        sa.CheckConstraint(f"queue IN {QUEUES}", name="ck_task_queue"),
        sa.CheckConstraint(f"state IN {STATES}", name="ck_task_state"),
    )

    # The claim index. Partial, so it covers only the working set.
    op.execute(
        "CREATE INDEX ix_task_claim ON task (queue, priority DESC, id) WHERE state = 'ready'"
    )
    op.execute("CREATE INDEX ix_task_lease ON task (lease_until) WHERE state = 'running'")
    op.execute(
        "CREATE UNIQUE INDEX uq_task_dedupe ON task (dedupe_key) "
        "WHERE dedupe_key IS NOT NULL AND state IN ('ready','running')"
    )
    op.execute("CREATE INDEX ix_task_document ON task (document_id) WHERE document_id IS NOT NULL")
    op.create_index("ix_task_workspace_state", "task", ["workspace_id", "state"])

    # Backfill a runtime row for every existing workspace. Without one, the
    # claim query's inner join would silently exclude that workspace's tasks.
    op.execute(
        "INSERT INTO workspace_runtime (workspace_id) SELECT id FROM workspace "
        "ON CONFLICT DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("task")
    op.drop_table("workspace_runtime")
