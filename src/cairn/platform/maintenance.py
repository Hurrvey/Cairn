"""Scheduled maintenance.

These jobs are the difference between a system that runs for a week and one that
runs for a year: lease reaping recovers work from dead workers, partition
creation keeps the audit log writable, and pruning keeps the task table's hot
set small.

They run on the ``maintain`` queue, at low priority, so they never compete with
user-facing ingestion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from cairn.core.db import transaction
from cairn.core.logging import get_logger
from cairn.core.time import utcnow
from cairn.tasks.dto import TaskContext, TaskResult
from cairn.tasks.repository import TaskRepository
from cairn.tasks.worker import TaskWorker, reap_and_reconcile

__all__ = [
    "MAINTENANCE_KINDS",
    "drop_expired_audit_partitions",
    "ensure_audit_partitions",
    "register_maintenance_handlers",
]

log = get_logger(__name__)

MAINTENANCE_KINDS = (
    "maintain.reap_leases",
    "maintain.prune_tasks",
    "maintain.audit_partitions",
    "maintain.expire_sessions",
)

#: Months of audit partitions kept ahead of `now`. Running out means audit
#: writes start failing — which, because audits are written inside the caller's
#: transaction, would take down every state-changing operation with them.
PARTITION_LOOKAHEAD_MONTHS = 3


def _month_bounds(offset: int) -> tuple[str, datetime, datetime]:
    base = utcnow().astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year = base.year + (base.month - 1 + offset) // 12
    month = (base.month - 1 + offset) % 12 + 1
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month // 12), month % 12 + 1, 1, tzinfo=UTC)
    return f"{start:%Y_%m}", start, end


async def ensure_audit_partitions(lookahead: int = PARTITION_LOOKAHEAD_MONTHS) -> int:
    """Create the current month's partition and the next `lookahead` months."""
    created = 0
    async with transaction() as session:
        for offset in range(lookahead + 1):
            suffix, start, end = _month_bounds(offset)
            exists = await session.scalar(
                text("SELECT to_regclass(:name) IS NOT NULL"),
                {"name": f"public.audit_log_{suffix}"},
            )
            if exists:
                continue
            await session.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS audit_log_{suffix} PARTITION OF audit_log "
                    f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
                )
            )
            created += 1
            log.info("maintenance.audit_partition_created", partition=f"audit_log_{suffix}")
    return created


async def drop_expired_audit_partitions(retention_days: int) -> int:
    """Retention by partition drop — O(1), versus a DELETE that would rewrite
    the largest append-only table in the system (FR-O-03)."""
    cutoff = utcnow() - timedelta(days=retention_days)
    dropped = 0
    async with transaction() as session:
        rows = await session.execute(
            text(
                """
                SELECT c.relname
                  FROM pg_class c
                  JOIN pg_inherits i ON i.inhrelid = c.oid
                  JOIN pg_class p ON p.oid = i.inhparent
                 WHERE p.relname = 'audit_log'
                """
            )
        )
        for (name,) in rows.all():
            suffix = str(name).removeprefix("audit_log_")
            try:
                year, month = (int(part) for part in suffix.split("_"))
            except ValueError:  # pragma: no cover — a hand-made partition
                continue
            # A partition is droppable only once its whole month is past the cutoff.
            end = datetime(year + (month // 12), month % 12 + 1, 1, tzinfo=UTC)
            if end < cutoff:
                await session.execute(text(f"DROP TABLE IF EXISTS {name}"))
                dropped += 1
                log.info("maintenance.audit_partition_dropped", partition=name)
    return dropped


async def expire_sessions() -> int:
    async with transaction() as session:
        result = await session.execute(
            text("DELETE FROM session WHERE expires_at < now() - interval '24 hours'")
        )
        return int(getattr(result, "rowcount", 0) or 0)


# --- handlers ----------------------------------------------------------------


async def handle_reap_leases(ctx: TaskContext) -> TaskResult:
    reaped, drifted = await reap_and_reconcile()
    return TaskResult.success(f"reaped={reaped} counters_reconciled={drifted}")


async def handle_prune_tasks(ctx: TaskContext) -> TaskResult:
    from cairn.core.config import get_settings

    days = int(ctx.payload.get("retain_days", get_settings().tasks.retain_done_days))
    async with transaction() as session:
        pruned = await TaskRepository().prune_completed(session, older_than_days=days)
    return TaskResult.success(f"pruned={pruned}")


async def handle_audit_partitions(ctx: TaskContext) -> TaskResult:
    created = await ensure_audit_partitions()
    retention = int(ctx.payload.get("retention_days", 365))
    dropped = await drop_expired_audit_partitions(retention)
    return TaskResult.success(f"created={created} dropped={dropped}")


async def handle_expire_sessions(ctx: TaskContext) -> TaskResult:
    removed = await expire_sessions()
    return TaskResult.success(f"expired_sessions={removed}")


def register_maintenance_handlers(worker: TaskWorker) -> None:
    worker.register("maintain.reap_leases", handle_reap_leases)
    worker.register("maintain.prune_tasks", handle_prune_tasks)
    worker.register("maintain.audit_partitions", handle_audit_partitions)
    worker.register("maintain.expire_sessions", handle_expire_sessions)
