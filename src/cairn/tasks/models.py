"""Task queue ORM models. Private to this module."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811 — dialect type
from sqlalchemy.orm import Mapped, mapped_column

from cairn.core.db import Base

__all__ = ["Task", "WorkspaceRuntime"]

QUEUES = ("fetch", "parse", "ocr", "chunk", "embed", "index", "maintain")
STATES = ("ready", "running", "done", "failed", "blocked", "cancelled")


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (
        # Partial index: stays small regardless of how many completed rows the
        # table accumulates, which is what keeps the claim query fast at scale.
        Index(
            "ix_task_claim",
            "queue",
            text("priority DESC"),
            "id",
            postgresql_where=text("state = 'ready'"),
        ),
        Index("ix_task_lease", "lease_until", postgresql_where=text("state = 'running'")),
        Index(
            "uq_task_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND state IN ('ready','running')"),
        ),
        Index("ix_task_document", "document_id", postgresql_where=text("document_id IS NOT NULL")),
        Index("ix_task_workspace_state", "workspace_id", "state"),
        CheckConstraint(f"queue IN {QUEUES}", name="queue"),
        CheckConstraint(f"state IN {STATES}", name="state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    document_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    #: The X-Request-Id of the call that enqueued this, so a worker's logs and
    #: spans link back to the originating HTTP request.
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    queue: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=100)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=5)

    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    progress_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceRuntime(Base):
    """Per-workspace concurrency counter, maintained in the claim/finish transaction.

    Counting running tasks inline with a correlated subquery would execute once
    per candidate row; at 100k queued tasks that cost dominates the claim. A
    maintained counter turns fairness into a single indexed join.

    Without fairness at all, one workspace uploading 50k documents blocks every
    other workspace behind it — the defining failure of naive FIFO queues in a
    multi-tenant system.
    """

    __tablename__ = "workspace_runtime"
    __table_args__ = (CheckConstraint("running >= 0", name="running_nonneg"),)

    workspace_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workspace.id", ondelete="CASCADE"),
        primary_key=True,
    )
    running: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    concurrency_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=16, server_default="16"
    )
