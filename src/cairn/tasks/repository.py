"""Task queue data access.

The claim query is the heart of this module. It is written as explicit SQL
rather than ORM because the exact shape — the CTE, ``FOR UPDATE ... SKIP
LOCKED``, and the fairness join — *is* the design, and expressing it through
query-builder indirection would obscure the one thing a reader needs to check.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.core.time import utcnow

__all__ = ["TaskRepository"]

_TASK_COLUMNS = """
    id, workspace_id, kb_id, document_id, correlation_id, queue, kind, payload,
    state, priority, attempt, max_attempts, run_after, lease_until, worker_id,
    dedupe_key, error_code, error_detail, progress_done, progress_total,
    created_at, started_at, finished_at
"""
_CLAIM_COLUMNS = ", ".join(f"t.{column.strip()}" for column in _TASK_COLUMNS.split(","))

# One statement: pick candidate rows, lock them, and flip them to running.
#
#   FOR UPDATE OF t SKIP LOCKED  -> exactly-once claiming with no coordinator.
#                                   Concurrent workers step over each other's
#                                   locked rows instead of blocking.
#   JOIN workspace_runtime       -> fairness. Without it, one workspace with
#                                   50k queued documents starves every other.
#   ORDER BY priority DESC, id   -> FIFO within a priority band.
#
# Each workspace contributes no more than its remaining capacity to a batch.
# Concurrent transactions can still observe the same counter, so the cap is
# soft across workers rather than a globally serialized admission lock.

# `_TASK_COLUMNS`, a module constant. Every value is a bound parameter.
_CLAIM_SQL = text(
    f"""
    WITH candidates AS (
        SELECT eligible.id
          FROM workspace_runtime wr
          CROSS JOIN LATERAL (
              SELECT queued.id, queued.priority
                FROM task queued
               WHERE queued.workspace_id = wr.workspace_id
                 AND queued.queue = :queue
                 AND queued.state = 'ready'
                 AND queued.run_after <= now()
               ORDER BY queued.priority DESC, queued.id
               LIMIT GREATEST(0, wr.concurrency_limit - wr.running)
          ) eligible
         WHERE wr.running < wr.concurrency_limit
    ), picked AS (
        SELECT t.id
          FROM task t
          JOIN candidates candidate ON candidate.id = t.id
         WHERE t.queue = :queue
           AND t.state = 'ready'
           AND t.run_after <= now()
         ORDER BY t.priority DESC, t.id
         LIMIT :batch
           FOR UPDATE OF t SKIP LOCKED
    )
    UPDATE task t
       SET state       = 'running',
           attempt     = t.attempt + 1,
           worker_id   = :worker_id,
           started_at  = now(),
           lease_until = now() + make_interval(secs => :lease_seconds)
      FROM picked p
     WHERE t.id = p.id
    RETURNING {_CLAIM_COLUMNS}
    """
)


class TaskRepository:
    # --- enqueue ------------------------------------------------------------

    async def ensure_runtime_row(self, session: AsyncSession, workspace_id: UUID) -> None:
        """A workspace with no ``workspace_runtime`` row is invisible to the
        claim query's inner join — its tasks would sit ready forever. Creating
        the row at enqueue time makes that unreachable."""
        await session.execute(
            text(
                "INSERT INTO workspace_runtime (workspace_id) VALUES (:ws) "
                "ON CONFLICT (workspace_id) DO NOTHING"
            ),
            {"ws": workspace_id},
        )

    async def insert(self, session: AsyncSession, values: dict[str, Any]) -> int | None:
        """Insert a task. Returns ``None`` when a live duplicate already exists."""
        result = await session.execute(
            text(
                """
                INSERT INTO task (workspace_id, kb_id, document_id, correlation_id,
                                  queue, kind, payload, priority, max_attempts,
                                  run_after, dedupe_key)
                VALUES (:workspace_id, :kb_id, :document_id, :correlation_id,
                        :queue, :kind, CAST(:payload AS jsonb), :priority, :max_attempts,
                        COALESCE(:run_after, now()), :dedupe_key)
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            values,
        )
        row = result.first()
        return int(row[0]) if row else None

    async def find_live_by_dedupe_key(self, session: AsyncSession, key: str) -> int | None:
        result = await session.execute(
            text(
                "SELECT id FROM task WHERE dedupe_key = :key "
                "AND state IN ('ready','running') LIMIT 1"
            ),
            {"key": key},
        )
        row = result.first()
        return int(row[0]) if row else None

    # --- claim / finish -----------------------------------------------------

    async def claim(
        self, session: AsyncSession, *, queue: str, batch: int, worker_id: str, lease: timedelta
    ) -> list[dict[str, Any]]:
        result = await session.execute(
            _CLAIM_SQL,
            {
                "queue": queue,
                "batch": batch,
                "worker_id": worker_id,
                "lease_seconds": int(lease.total_seconds()),
            },
        )
        rows = [dict(row) for row in result.mappings().all()]
        rows.sort(key=lambda row: (-row["priority"], row["id"]))
        if rows:
            await self._adjust_running(session, Counter(str(r["workspace_id"]) for r in rows))
        return rows

    async def _adjust_running(
        self, session: AsyncSession, deltas: Counter[str], *, sign: int = 1
    ) -> None:
        for workspace_id, count in sorted(deltas.items()):
            await session.execute(
                text(
                    "UPDATE workspace_runtime SET running = GREATEST(0, running + :delta) "
                    "WHERE workspace_id = CAST(:ws AS uuid)"
                ),
                {"delta": sign * count, "ws": workspace_id},
            )

    async def finish(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        attempt: int,
        state: str,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        result = await session.execute(
            text(
                """
                UPDATE task
                   SET state = :state, finished_at = now(), lease_until = NULL,
                       worker_id = NULL, error_code = :error_code,
                       error_detail = :error_detail
                 WHERE id = :task_id AND state = 'running'
                   AND worker_id = :worker_id AND attempt = :attempt
                   AND lease_until > clock_timestamp()
                RETURNING workspace_id
                """
            ),
            {
                "state": state,
                "task_id": task_id,
                "worker_id": worker_id,
                "attempt": attempt,
                "error_code": error_code,
                "error_detail": error_detail,
            },
        )
        workspace_id = result.scalar_one_or_none()
        if workspace_id is None:
            return False
        await self._adjust_running(session, Counter({str(workspace_id): 1}), sign=-1)
        return True

    async def reschedule(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        attempt: int,
        delay: timedelta,
        error_code: str | None,
        error_detail: str | None,
    ) -> bool:
        result = await session.execute(
            text(
                """
                UPDATE task
                   SET state = 'ready', lease_until = NULL, worker_id = NULL,
                        run_after = now() + make_interval(secs => :delay_seconds),
                        error_code = :error_code, error_detail = :error_detail
                 WHERE id = :task_id AND state = 'running'
                   AND worker_id = :worker_id AND attempt = :attempt
                   AND lease_until > clock_timestamp()
                RETURNING workspace_id
                """
            ),
            {
                "task_id": task_id,
                "worker_id": worker_id,
                "attempt": attempt,
                "delay_seconds": int(delay.total_seconds()),
                "error_code": error_code,
                "error_detail": error_detail,
            },
        )
        workspace_id = result.scalar_one_or_none()
        if workspace_id is None:
            return False
        await self._adjust_running(session, Counter({str(workspace_id): 1}), sign=-1)
        return True

    async def heartbeat(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        attempt: int,
        lease: timedelta,
    ) -> bool:
        result = await session.execute(
            text(
                "UPDATE task SET lease_until = clock_timestamp() + make_interval(secs => :secs) "
                "WHERE id = :task_id AND state = 'running' "
                "AND worker_id = :worker_id AND attempt = :attempt "
                "AND lease_until > clock_timestamp() RETURNING id"
            ),
            {
                "task_id": task_id,
                "worker_id": worker_id,
                "attempt": attempt,
                "secs": int(lease.total_seconds()),
            },
        )
        return result.first() is not None

    async def set_progress(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        attempt: int,
        done: int,
        total: int | None,
    ) -> bool:
        result = await session.execute(
            text(
                "UPDATE task SET progress_done = :done, progress_total = "
                "COALESCE(:total, progress_total) WHERE id = :task_id "
                "AND state = 'running' AND worker_id = :worker_id "
                "AND attempt = :attempt AND lease_until > clock_timestamp() RETURNING id"
            ),
            {
                "task_id": task_id,
                "worker_id": worker_id,
                "attempt": attempt,
                "done": done,
                "total": total,
            },
        )
        return result.first() is not None

    async def lock_owned_task(
        self,
        session: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        attempt: int,
    ) -> dict[str, Any] | None:
        result = await session.execute(
            text(
                f"SELECT {_TASK_COLUMNS} FROM task "
                "WHERE id = :task_id AND state = 'running' "
                "AND worker_id = :worker_id AND attempt = :attempt "
                "AND lease_until > clock_timestamp() FOR UPDATE"
            ),
            {"task_id": task_id, "worker_id": worker_id, "attempt": attempt},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    # --- maintenance --------------------------------------------------------

    async def reap_expired_leases(self, session: AsyncSession) -> int:
        """Return abandoned tasks to the queue.

        A worker that is OOM-killed or evicted leaves its task ``running`` with
        a lease nobody will renew. Because every handler is idempotent, running
        it again is safe; losing the work would not be.
        """
        result = await session.execute(
            text(
                """
                UPDATE task
                   SET state = 'ready', lease_until = NULL, worker_id = NULL,
                       error_code = 'LEASE_EXPIRED'
                 WHERE state = 'running' AND lease_until < now()
                RETURNING id, workspace_id
                """
            )
        )
        rows = result.all()
        if rows:
            await self._adjust_running(session, Counter(str(r[1]) for r in rows), sign=-1)
        return len(rows)

    async def reconcile_running_counters(self, session: AsyncSession) -> int:
        """Recompute ``workspace_runtime.running`` from the source of truth.

        The counter is maintained incrementally, so a process killed between the
        UPDATE and the COMMIT can leave it drifted. Drift is self-correcting
        rather than silently throttling a workspace forever.
        """
        result = await session.execute(
            text(
                """
                UPDATE workspace_runtime wr
                   SET running = COALESCE(actual.count, 0)
                  FROM (
                        SELECT w.workspace_id,
                               (SELECT count(*) FROM task t
                                 WHERE t.workspace_id = w.workspace_id
                                   AND t.state = 'running') AS count
                          FROM workspace_runtime w
                       ) AS actual
                 WHERE wr.workspace_id = actual.workspace_id
                   AND wr.running IS DISTINCT FROM COALESCE(actual.count, 0)
                RETURNING wr.workspace_id
                """
            )
        )
        return len(result.all())

    async def prune_completed(self, session: AsyncSession, *, older_than_days: int) -> int:
        result = await session.execute(
            text(
                "DELETE FROM task WHERE state IN ('done','cancelled') "
                "AND finished_at < now() - make_interval(days => :days)"
            ),
            {"days": older_than_days},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    # --- queries ------------------------------------------------------------

    async def queue_depth(self, session: AsyncSession) -> dict[str, int]:
        result = await session.execute(
            text("SELECT queue, count(*) FROM task WHERE state = 'ready' GROUP BY queue")
        )
        return {str(row[0]): int(row[1]) for row in result.all()}

    async def oldest_ready_age(self, session: AsyncSession) -> dict[str, float]:
        """Age of the oldest ready task per queue — the real starvation signal.

        Depth alone can look healthy while one task sits at the back for hours
        behind a saturated workspace.
        """
        result = await session.execute(
            text(
                "SELECT queue, EXTRACT(EPOCH FROM (now() - min(created_at))) "
                "FROM task WHERE state = 'ready' GROUP BY queue"
            )
        )
        return {str(row[0]): float(row[1] or 0.0) for row in result.all()}

    async def get(self, session: AsyncSession, task_id: int) -> dict[str, Any] | None:
        result = await session.execute(
            text(f"SELECT {_TASK_COLUMNS} FROM task WHERE id = :id"),
            {"id": task_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def list_for_document(
        self, session: AsyncSession, document_id: UUID
    ) -> list[dict[str, Any]]:
        result = await session.execute(
            text(f"SELECT {_TASK_COLUMNS} FROM task WHERE document_id = :doc ORDER BY id"),
            {"doc": document_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def cancel(self, session: AsyncSession, task_id: int) -> bool:
        """Cancel a task that has not started. A running task is left to finish:
        interrupting it mid-write is how partial state gets created."""
        result = await session.execute(
            text(
                "UPDATE task SET state = 'cancelled', finished_at = now() "
                "WHERE id = :id AND state = 'ready'"
            ),
            {"id": task_id},
        )
        return bool(getattr(result, "rowcount", 0))

    async def cancel_for_document(self, session: AsyncSession, document_id: UUID) -> int:
        result = await session.execute(
            text(
                "UPDATE task SET state = 'cancelled', finished_at = now() "
                "WHERE document_id = :doc AND state = 'ready'"
            ),
            {"doc": document_id},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def cancel_ready_for_document(
        self, session: AsyncSession, document_id: UUID, *, exclude_task_id: int
    ) -> int:
        result = await session.execute(
            text(
                "WITH cancellable AS ("
                "SELECT id FROM task WHERE document_id=:document_id "
                "AND id<>:exclude_task_id AND state='ready' FOR UPDATE SKIP LOCKED"
                ") UPDATE task SET state='cancelled', finished_at=now() "
                "FROM cancellable WHERE task.id=cancellable.id"
            ),
            {"document_id": document_id, "exclude_task_id": exclude_task_id},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def has_live_for_document(
        self, session: AsyncSession, document_id: UUID, *, exclude_task_id: int
    ) -> bool:
        return bool(
            await session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM task WHERE document_id=:document_id "
                    "AND id<>:exclude_task_id AND state IN ('ready','running'))"
                ),
                {"document_id": document_id, "exclude_task_id": exclude_task_id},
            )
        )

    async def cancel_ready_for_kb(
        self, session: AsyncSession, kb_id: UUID, *, exclude_task_id: int | None = None
    ) -> int:
        exclusion = "" if exclude_task_id is None else " AND id<>:exclude_task_id"
        result = await session.execute(
            text(
                "WITH cancellable AS (SELECT id FROM task "
                f"WHERE kb_id=:kb_id AND state='ready'{exclusion} FOR UPDATE SKIP LOCKED) "
                "UPDATE task SET state='cancelled', finished_at=now() "
                "FROM cancellable WHERE task.id=cancellable.id"
            ),
            (
                {"kb_id": kb_id}
                if exclude_task_id is None
                else {"kb_id": kb_id, "exclude_task_id": exclude_task_id}
            ),
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def has_live_for_kb(
        self, session: AsyncSession, kb_id: UUID, *, exclude_task_id: int
    ) -> bool:
        return bool(
            await session.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM task WHERE kb_id=:kb_id "
                    "AND id<>:exclude_task_id AND state IN ('ready','running'))"
                ),
                {"kb_id": kb_id, "exclude_task_id": exclude_task_id},
            )
        )

    async def cancel_ready_ingestion_generation(
        self,
        session: AsyncSession,
        *,
        document_id: UUID,
        revision: int,
        index_version: int,
        recovery_generation: int,
    ) -> int:
        result = await session.execute(
            text(
                "UPDATE task SET state='cancelled', finished_at=now() "
                "WHERE document_id=:document_id AND state='ready' "
                "AND kind IN ('document.parse','document.chunk','document.embed','document.index') "
                "AND payload->>'revision'=:revision "
                "AND payload->>'index_version'=:index_version "
                "AND ((:recovery_generation=0 AND payload->'recovery_generation' IS NULL) OR "
                "payload->'recovery_generation'=to_jsonb(CAST(:recovery_generation AS integer)))"
            ),
            {
                "document_id": document_id,
                "revision": str(revision),
                "index_version": str(index_version),
                "recovery_generation": recovery_generation,
            },
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def set_workspace_concurrency(
        self, session: AsyncSession, workspace_id: UUID, limit: int
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO workspace_runtime (workspace_id, concurrency_limit) "
                "VALUES (:ws, :limit) ON CONFLICT (workspace_id) "
                "DO UPDATE SET concurrency_limit = :limit"
            ),
            {"ws": workspace_id, "limit": limit},
        )

    async def notify(self, session: AsyncSession, queue: str) -> None:
        """Wake an idle worker immediately instead of waiting out its poll."""
        await session.execute(text(f"NOTIFY cairn_task_{queue}"))

    @staticmethod
    def _now() -> Any:
        return utcnow()
