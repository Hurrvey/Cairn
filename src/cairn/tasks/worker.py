"""Worker harness.

One process serves one queue. Queues are separated by what bounds them — CPU,
I/O, GPU — so they can be scaled independently (NFR-S-02). A single
undifferentiated pool means a 500-page PDF stalls crawl throughput.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import UUID

from cairn.core.config import get_settings
from cairn.core.db import transaction
from cairn.core.logging import bind_request_context, clear_request_context, get_logger
from cairn.core.telemetry import (
    task_attempts_total,
    task_duration_seconds,
    task_lease_expired_total,
    task_running,
)
from cairn.tasks.dto import QUEUE_PROFILES, TaskContext, TaskResult
from cairn.tasks.repository import TaskRepository
from cairn.tasks.service import is_retryable, next_delay

__all__ = ["TaskHandler", "TaskWorker", "worker_identity"]

log = get_logger(__name__)

TaskHandler = Callable[[TaskContext], Awaitable[TaskResult]]


def worker_identity() -> str:
    """Stable enough to attribute a stuck lease to a pod, unique enough to
    distinguish replicas."""
    return f"{socket.gethostname()}/{os.getpid()}"


class TaskWorker:
    def __init__(
        self,
        queue: str,
        *,
        concurrency: int | None = None,
        repository: TaskRepository | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        if queue not in QUEUE_PROFILES:
            raise ValueError(f"unknown queue {queue!r}")
        self.queue = queue
        self.profile = QUEUE_PROFILES[queue]
        self.concurrency = concurrency or self.profile.default_concurrency
        self.worker_id = worker_identity()
        self._repo = repository or TaskRepository()
        self._handlers: dict[str, TaskHandler] = {}
        self._shutdown = asyncio.Event()
        self._wakeup = asyncio.Event()
        #: Set whenever a slot frees, so the claim loop waits on a signal rather
        #: than spinning on a sleep.
        self._slot_free = asyncio.Event()
        self._inflight: set[asyncio.Task[None]] = set()
        self._poll_interval = poll_interval
        self._listener: Any = None

    # --- registration -------------------------------------------------------

    def register(self, kind: str, handler: TaskHandler) -> None:
        if kind in self._handlers:
            raise ValueError(f"handler for {kind!r} is already registered")
        self._handlers[kind] = handler

    def handler(self, kind: str) -> Callable[[TaskHandler], TaskHandler]:
        def decorator(fn: TaskHandler) -> TaskHandler:
            self.register(kind, fn)
            return fn

        return decorator

    # --- lifecycle ----------------------------------------------------------

    def install_signal_handlers(self) -> None:  # pragma: no cover — process-level
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self.request_shutdown)

    def request_shutdown(self) -> None:
        log.info("worker.shutdown_requested", queue=self.queue)
        self._shutdown.set()
        self._wakeup.set()

    async def run(self) -> None:
        log.info(
            "worker.started",
            queue=self.queue,
            concurrency=self.concurrency,
            worker_id=self.worker_id,
            kinds=sorted(self._handlers),
        )
        await self._start_listener()
        try:
            while not self._shutdown.is_set():
                claimed = await self._claim_batch()
                if not claimed:
                    await self._wait_for_work()
                    continue
                for row in claimed:
                    task = asyncio.create_task(self._execute(row))
                    self._inflight.add(task)
                    task.add_done_callback(self._on_task_done)

                # Do not out-run the concurrency budget. Waiting on a signal
                # rather than polling means a worker at capacity consumes no CPU.
                if len(self._inflight) >= self.concurrency:
                    self._slot_free.clear()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._slot_free.wait(), timeout=5.0)
        finally:
            await self._drain()
            await self._stop_listener()
            log.info("worker.stopped", queue=self.queue)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        self._slot_free.set()

    async def _drain(self) -> None:
        """Let in-flight work finish, then release its leases explicitly.

        Waiting for the lease to expire instead would leave the task stranded
        for up to the lease duration during every ordinary deploy.
        """
        if not self._inflight:
            return
        timeout = get_settings().tasks.graceful_shutdown_s
        log.info("worker.draining", queue=self.queue, inflight=len(self._inflight))
        _finished, pending = await asyncio.wait(set(self._inflight), timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            log.warning("worker.drain_timeout", queue=self.queue, abandoned=len(pending))

    # --- claim / execute ----------------------------------------------------

    async def _claim_batch(self) -> list[dict[str, Any]]:
        capacity = max(0, self.concurrency - len(self._inflight))
        if capacity == 0:
            return []
        batch = min(capacity, self.profile.claim_batch)
        async with transaction() as session:
            return await self._repo.claim(
                session,
                queue=self.queue,
                batch=batch,
                worker_id=self.worker_id,
                lease=self.profile.lease,
            )

    async def _execute(self, row: dict[str, Any]) -> None:
        task_id = int(row["id"])
        kind = str(row["kind"])
        workspace_id: UUID = row["workspace_id"]

        clear_request_context()
        bind_request_context(
            request_id=row.get("correlation_id"),
            task_id=task_id,
            queue=self.queue,
            kind=kind,
            workspace_id=str(workspace_id),
            attempt=row["attempt"],
        )
        task_running.labels(queue=self.queue).inc()
        started = time.perf_counter()
        outcome = "failed"

        try:
            handler = self._handlers.get(kind)
            if handler is None:
                # A task nobody can run must not spin forever: fail it terminally
                # so it surfaces in the dead-letter view instead of being retried
                # by a worker that will never have the handler either.
                await self._fail(
                    task_id,
                    workspace_id,
                    "NO_HANDLER",
                    f"No handler registered for kind {kind!r} on queue {self.queue!r}.",
                    retryable=False,
                    attempt=int(row["attempt"]),
                    max_attempts=int(row["max_attempts"]),
                )
                log.error("task.no_handler", kind=kind)
                return

            context = self._build_context(row)
            result = await handler(context)

            if result.ok:
                async with transaction() as session:
                    await self._repo.finish(
                        session,
                        task_id=task_id,
                        workspace_id=workspace_id,
                        state="done",
                        error_detail=result.detail,
                    )
                outcome = "done"
                log.info("task.completed", detail=result.detail)
            else:
                await self._fail(
                    task_id,
                    workspace_id,
                    result.error_code or "TASK_FAILED",
                    result.detail or "",
                    retryable=is_retryable(
                        result.error_code, default=result.retryable is not False
                    ),
                    attempt=int(row["attempt"]),
                    max_attempts=int(row["max_attempts"]),
                )
                outcome = "failed"

        except asyncio.CancelledError:
            log.warning("task.cancelled_at_shutdown", task_id=task_id)
            raise
        except Exception as exc:
            log.exception("task.unhandled_exception", task_id=task_id, error=type(exc).__name__)
            await self._fail(
                task_id,
                workspace_id,
                type(exc).__name__.upper(),
                str(exc)[:2000],
                retryable=True,
                attempt=int(row["attempt"]),
                max_attempts=int(row["max_attempts"]),
            )
        finally:
            task_running.labels(queue=self.queue).dec()
            task_attempts_total.labels(queue=self.queue, kind=kind).inc()
            task_duration_seconds.labels(queue=self.queue, kind=kind, outcome=outcome).observe(
                time.perf_counter() - started
            )
            clear_request_context()

    def _build_context(self, row: dict[str, Any]) -> TaskContext:
        task_id = int(row["id"])

        async def heartbeat(extend: timedelta | None = None) -> None:
            async with transaction() as session:
                await self._repo.heartbeat(session, task_id, extend or self.profile.lease)

        async def progress(done: int, total: int | None) -> None:
            async with transaction() as session:
                await self._repo.set_progress(session, task_id, done, total)

        payload = row["payload"]
        return TaskContext(
            task_id=task_id,
            queue=str(row["queue"]),
            kind=str(row["kind"]),
            workspace_id=row["workspace_id"],
            kb_id=row["kb_id"],
            document_id=row["document_id"],
            payload=payload if isinstance(payload, dict) else {},
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            correlation_id=row["correlation_id"],
            worker_id=self.worker_id,
            _heartbeat=heartbeat,
            _progress=progress,
        )

    async def _fail(
        self,
        task_id: int,
        workspace_id: UUID,
        error_code: str,
        detail: str,
        *,
        retryable: bool,
        attempt: int,
        max_attempts: int,
    ) -> None:
        exhausted = attempt >= max_attempts
        if retryable and not exhausted:
            delay = next_delay(attempt)
            async with transaction() as session:
                await self._repo.reschedule(
                    session,
                    task_id=task_id,
                    workspace_id=workspace_id,
                    delay=delay,
                    error_code=error_code,
                    error_detail=detail,
                )
            log.warning(
                "task.retrying",
                error_code=error_code,
                attempt=attempt,
                retry_in_s=round(delay.total_seconds(), 1),
            )
            return

        async with transaction() as session:
            await self._repo.finish(
                session,
                task_id=task_id,
                workspace_id=workspace_id,
                state="failed",
                error_code=error_code,
                error_detail=detail,
            )
        log.error(
            "task.dead_lettered",
            error_code=error_code,
            attempt=attempt,
            reason="exhausted" if exhausted else "terminal",
        )

    # --- wakeup -------------------------------------------------------------

    async def _wait_for_work(self) -> None:
        """Sleep until notified, or until the poll interval elapses.

        The poll is the safety net: a dropped NOTIFY costs one interval of
        latency rather than a stalled queue.
        """
        self._wakeup.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wakeup.wait(), timeout=self._poll_interval)

    async def _start_listener(self) -> None:
        """Best-effort LISTEN on a dedicated connection."""
        try:
            import asyncpg

            dsn = get_settings().database_url.replace("+asyncpg", "")
            self._listener = await asyncpg.connect(dsn)
            await self._listener.add_listener(
                f"cairn_task_{self.queue}", lambda *_: self._wakeup.set()
            )
            log.info("worker.listener_started", queue=self.queue)
        except Exception as exc:
            log.warning(
                "worker.listener_unavailable",
                queue=self.queue,
                error=type(exc).__name__,
                detail="falling back to polling",
            )
            self._listener = None

    async def _stop_listener(self) -> None:
        if self._listener is not None:
            with contextlib.suppress(Exception):
                await self._listener.close()
            self._listener = None


async def reap_and_reconcile(repository: TaskRepository | None = None) -> tuple[int, int]:
    """Reclaim expired leases and correct counter drift. Run from `maintain`."""
    repo = repository or TaskRepository()
    async with transaction() as session:
        reaped = await repo.reap_expired_leases(session)
        drifted = await repo.reconcile_running_counters(session)
    if reaped:
        task_lease_expired_total.labels(queue="all").inc(reaped)
        log.warning("task.leases_reaped", count=reaped)
    if drifted:
        log.warning("task.counters_reconciled", workspaces=drifted)
    return reaped, drifted
