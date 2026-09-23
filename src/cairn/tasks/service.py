"""Task queue service.

Note the signature of :meth:`TaskService.enqueue`: it *requires* a session.
There is deliberately no session-less overload, because that is the entire point
of ADR-0003 — the row that needs work and the task that does it must commit
together. An API that let a caller enqueue outside a transaction would reopen
the dual-write hole this design exists to close.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from cairn.core.db import session_scope, transaction
from cairn.core.logging import get_logger
from cairn.core.telemetry import task_enqueued_total
from cairn.tasks.dto import (
    QUEUE_PROFILES,
    DocumentTaskProgress,
    TaskContext,
    TaskSpec,
    TaskStatus,
)
from cairn.tasks.repository import TaskRepository

__all__ = [
    "RETRYABLE_CODES",
    "TERMINAL_CODES",
    "TaskLeaseLostError",
    "TaskService",
    "get_task_service",
    "next_delay",
]

log = get_logger(__name__)


class TaskLeaseLostError(RuntimeError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} lease is no longer owned by this attempt")
        self.task_id = task_id


#: A retryable failure is transient: the same input may succeed later.
RETRYABLE_CODES = frozenset(
    {
        "UPSTREAM_UNAVAILABLE",
        "RATE_LIMIT_EXCEEDED",
        "LEASE_EXPIRED",
        "CONNECTION_ERROR",
        "TIMEOUT",
        "PARSE_TIMEOUT",
        "OCR_FAILED",
        "EMBED_PROVIDER_ERROR",
        "CRAWL_HTTP_ERROR",
        "SANDBOX_UNAVAILABLE",
        "PROVIDER_CIRCUIT_OPEN",
    }
)

#: A terminal failure will fail identically on every retry. Burning five
#: attempts on a corrupt file wastes ~10 minutes of queue capacity and delays
#: every good document behind it.
TERMINAL_CODES = frozenset(
    {
        "UNSUPPORTED_FILE_TYPE",
        "PARSE_ENCRYPTED_PDF",
        "PARSE_CORRUPT_FILE",
        "PARSE_UNSUPPORTED_MIME",
        "PARSE_EMPTY_CONTENT",
        "VALIDATION_FAILED",
        "QUOTA_EXCEEDED",
        "PERMISSION_DENIED",
        "EMBEDDING_DIMENSION_MISMATCH",
        "INDEX_DIMENSION_MISMATCH",
        "CHUNK_CONFIG_INVALID",
        "CRAWL_ROBOTS_DISALLOWED",
        "CRAWL_BLOCKED_ADDRESS",
        "ARCHIVE_UNSAFE",
        "FUNCTION_TIMEOUT",
        "FUNCTION_OOM",
        "FUNCTION_OUTPUT_INVALID",
    }
)

_MAX_BACKOFF_SECONDS = 300


def next_delay(attempt: int) -> timedelta:
    """Exponential backoff with jitter.

    Jitter matters more than the exponent: without it, a provider outage that
    fails 500 tasks at once produces 500 simultaneous retries, which is a
    thundering herd aimed at a service that is already struggling.
    """
    base = min(2**attempt, _MAX_BACKOFF_SECONDS)
    jitter = 0.5 + secrets.randbelow(1000) / 1000.0
    return timedelta(seconds=base * jitter)


def is_retryable(error_code: str | None, *, default: bool = True) -> bool:
    if error_code is None:
        return default
    if error_code in TERMINAL_CODES:
        return False
    if error_code in RETRYABLE_CODES:
        return True
    return default


class TaskService:
    def __init__(self, repository: TaskRepository | None = None) -> None:
        self._repo = repository or TaskRepository()

    # --- enqueue ------------------------------------------------------------

    async def enqueue(self, session: AsyncSession, spec: TaskSpec) -> int | None:
        """Enqueue inside the caller's transaction (NFR-R-03).

        Returns the new task id, or the existing id when ``dedupe_key`` matches
        a task that is still ready or running.
        """
        await self._repo.ensure_runtime_row(session, spec.workspace_id)

        task_id = await self._repo.insert(
            session,
            {
                "workspace_id": spec.workspace_id,
                "kb_id": spec.kb_id,
                "document_id": spec.document_id,
                "correlation_id": spec.correlation_id,
                "queue": spec.queue,
                "kind": spec.kind,
                "payload": json.dumps(spec.payload),
                "priority": spec.priority,
                "max_attempts": spec.max_attempts,
                "run_after": spec.run_after,
                "dedupe_key": spec.dedupe_key,
            },
        )

        if task_id is None and spec.dedupe_key:
            existing = await self._repo.find_live_by_dedupe_key(session, spec.dedupe_key)
            log.info("task.deduplicated", kind=spec.kind, dedupe_key=spec.dedupe_key)
            task_enqueued_total.labels(queue=spec.queue, outcome="deduplicated").inc()
            return existing

        task_enqueued_total.labels(queue=spec.queue, outcome="enqueued").inc()
        log.info("task.enqueued", task_id=task_id, queue=spec.queue, kind=spec.kind)
        return task_id

    async def enqueue_many(
        self, session: AsyncSession, specs: Sequence[TaskSpec]
    ) -> list[int | None]:
        return [await self.enqueue(session, spec) for spec in specs]

    async def lock_owned_task(self, session: AsyncSession, context: TaskContext) -> dict[str, Any]:
        row = await self._repo.lock_owned_task(
            session,
            task_id=context.task_id,
            worker_id=context.worker_id,
            attempt=context.attempt,
        )
        if row is None:
            raise TaskLeaseLostError(context.task_id)
        return row

    async def notify(self, queue: str) -> None:
        """Signal waiting workers. Best-effort: a lost NOTIFY costs one poll
        interval of latency, never a lost task."""
        try:
            async with transaction() as session:
                await self._repo.notify(session, queue)
        except Exception as exc:
            log.warning("task.notify_failed", queue=queue, error=type(exc).__name__)

    # --- queries ------------------------------------------------------------

    async def get_status(self, task_id: int) -> TaskStatus | None:
        async with session_scope() as session:
            row = await self._repo.get(session, task_id)
        return _to_status(row) if row else None

    async def queue_depth(self) -> dict[str, int]:
        async with session_scope() as session:
            depths = await self._repo.queue_depth(session)
        return {queue: depths.get(queue, 0) for queue in QUEUE_PROFILES}

    async def oldest_ready_age(self) -> dict[str, float]:
        async with session_scope() as session:
            return await self._repo.oldest_ready_age(session)

    async def document_progress(self, document_id: UUID) -> DocumentTaskProgress:
        async with session_scope() as session:
            rows = await self._repo.list_for_document(session, document_id)
        return DocumentTaskProgress(
            document_id=document_id, tasks=tuple(_to_status(row) for row in rows)
        )

    # --- control ------------------------------------------------------------

    async def cancel(self, task_id: int) -> bool:
        async with transaction() as session:
            return await self._repo.cancel(session, task_id)

    async def cancel_for_document(self, document_id: UUID) -> int:
        async with transaction() as session:
            return await self._repo.cancel_for_document(session, document_id)

    async def drain_ready_for_document(
        self, session: AsyncSession, *, document_id: UUID, exclude_task_id: int
    ) -> bool:
        await self._repo.cancel_ready_for_document(
            session, document_id, exclude_task_id=exclude_task_id
        )
        return not await self._repo.has_live_for_document(
            session, document_id, exclude_task_id=exclude_task_id
        )

    async def cancel_ready_for_kb(
        self, session: AsyncSession, *, kb_id: UUID, exclude_task_id: int | None = None
    ) -> int:
        return await self._repo.cancel_ready_for_kb(session, kb_id, exclude_task_id=exclude_task_id)

    async def drain_ready_for_kb(
        self, session: AsyncSession, *, kb_id: UUID, exclude_task_id: int
    ) -> bool:
        await self._repo.cancel_ready_for_kb(session, kb_id, exclude_task_id=exclude_task_id)
        return not await self._repo.has_live_for_kb(session, kb_id, exclude_task_id=exclude_task_id)

    async def cancel_ready_ingestion_generation(
        self,
        session: AsyncSession,
        *,
        document_id: UUID,
        revision: int,
        index_version: int,
        recovery_generation: int,
    ) -> int:
        return await self._repo.cancel_ready_ingestion_generation(
            session,
            document_id=document_id,
            revision=revision,
            index_version=index_version,
            recovery_generation=recovery_generation,
        )

    async def retry(self, task_id: int) -> bool:
        """Operator-initiated retry: resets the attempt counter so a task that
        exhausted its budget gets a full fresh allowance."""
        async with transaction() as session:
            row = await self._repo.get(session, task_id)
            if row is None or row["state"] != "failed":
                return False
            from sqlalchemy import text

            await session.execute(
                text(
                    "UPDATE task SET state='ready', attempt=0, run_after=now(), "
                    "error_code=NULL, error_detail=NULL, finished_at=NULL WHERE id=:id"
                ),
                {"id": task_id},
            )
            return True

    async def set_workspace_concurrency(self, workspace_id: UUID, limit: int) -> None:
        async with transaction() as session:
            await self._repo.set_workspace_concurrency(session, workspace_id, limit)


def _to_status(row: dict[str, Any]) -> TaskStatus:
    return TaskStatus(
        id=int(row["id"]),
        queue=str(row["queue"]),
        kind=str(row["kind"]),
        state=row["state"],
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        error_code=row["error_code"],
        error_detail=row["error_detail"],
        progress_done=int(row["progress_done"]),
        progress_total=row["progress_total"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


_service: TaskService | None = None


def get_task_service() -> TaskService:
    global _service
    if _service is None:
        _service = TaskService()
    return _service


def reset_task_service() -> None:
    global _service
    _service = None
