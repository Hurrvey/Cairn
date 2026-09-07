"""Task queue DTOs and the handler contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

__all__ = [
    "QUEUE_PROFILES",
    "DocumentTaskProgress",
    "QueueName",
    "QueueProfile",
    "TaskContext",
    "TaskResult",
    "TaskSpec",
    "TaskState",
    "TaskStatus",
]

QueueName = Literal["fetch", "parse", "ocr", "chunk", "embed", "index", "maintain"]
TaskState = Literal["ready", "running", "done", "failed", "blocked", "cancelled"]


@dataclass(frozen=True, slots=True)
class QueueProfile:
    """Resource shape of a queue. Drives worker deployment sizing.

    Queues are separated by what bounds them, not by what they mean: parsing a
    500-page PDF pins a core for minutes while crawling is pure I/O. One
    undifferentiated pool means a single large document stalls crawl throughput.
    """

    name: QueueName
    bound_by: Literal["cpu", "io", "gpu", "mixed"]
    default_concurrency: int
    lease: timedelta
    claim_batch: int = 16


QUEUE_PROFILES: dict[str, QueueProfile] = {
    "fetch": QueueProfile("fetch", "io", 100, timedelta(minutes=5)),
    "parse": QueueProfile("parse", "cpu", 4, timedelta(minutes=15)),
    "ocr": QueueProfile("ocr", "cpu", 2, timedelta(minutes=30)),
    "chunk": QueueProfile("chunk", "cpu", 8, timedelta(minutes=5)),
    # Batch-claimed so a GPU batch fills from several documents at once.
    "embed": QueueProfile("embed", "gpu", 4, timedelta(minutes=10), claim_batch=64),
    "index": QueueProfile("index", "io", 16, timedelta(minutes=10)),
    "maintain": QueueProfile("maintain", "mixed", 2, timedelta(minutes=60)),
}


@dataclass(frozen=True, slots=True)
class TaskSpec:
    queue: QueueName
    kind: str
    workspace_id: UUID
    payload: dict[str, Any] = field(default_factory=dict)
    kb_id: UUID | None = None
    document_id: UUID | None = None
    correlation_id: str | None = None
    priority: int = 100
    max_attempts: int = 5
    run_after: datetime | None = None
    #: Convention: ``{kind}:{entity_id}:{revision}``. A partial unique index
    #: prevents a duplicate while one is ready or running.
    dedupe_key: str | None = None


@dataclass(frozen=True, slots=True)
class TaskStatus:
    id: int
    queue: str
    kind: str
    state: TaskState
    attempt: int
    max_attempts: int
    error_code: str | None
    error_detail: str | None
    progress_done: int
    progress_total: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class DocumentTaskProgress:
    document_id: UUID
    tasks: tuple[TaskStatus, ...]

    @property
    def current(self) -> TaskStatus | None:
        for task in self.tasks:
            if task.state in ("running", "ready"):
                return task
        return None

    @property
    def failed(self) -> TaskStatus | None:
        return next((t for t in self.tasks if t.state == "failed"), None)


@dataclass(slots=True)
class TaskResult:
    ok: bool
    detail: str | None = None
    error_code: str | None = None
    retryable: bool | None = None

    @classmethod
    def success(cls, detail: str | None = None) -> TaskResult:
        return cls(ok=True, detail=detail)

    @classmethod
    def skipped(cls, detail: str) -> TaskResult:
        return cls(ok=True, detail=f"skipped: {detail}")

    @classmethod
    def failure(cls, error_code: str, detail: str, *, retryable: bool = True) -> TaskResult:
        return cls(ok=False, error_code=error_code, detail=detail, retryable=retryable)


@dataclass(slots=True)
class TaskContext:
    """Everything a handler is given, plus the two calls it owes a long task."""

    task_id: int
    queue: str
    kind: str
    workspace_id: UUID
    kb_id: UUID | None
    document_id: UUID | None
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    correlation_id: str | None
    worker_id: str

    _heartbeat: Callable[[timedelta | None], Awaitable[None]] | None = None
    _progress: Callable[[int, int | None], Awaitable[None]] | None = None

    async def heartbeat(self, extend: timedelta | None = None) -> None:
        """Extend the lease. A task that outruns its lease is reclaimed and run
        again by another worker — correct, but wasteful."""
        if self._heartbeat is not None:
            await self._heartbeat(extend)

    async def report_progress(self, done: int, total: int | None = None) -> None:
        if self._progress is not None:
            await self._progress(done, total)

    @property
    def is_final_attempt(self) -> bool:
        return self.attempt >= self.max_attempts
