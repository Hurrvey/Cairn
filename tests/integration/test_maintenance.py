"""Audit partition continuity and retention against the real migrated schema."""

from uuid import uuid4

from sqlalchemy import text

from cairn.core.db import session_scope, transaction
from cairn.platform.maintenance import (
    drop_expired_audit_partitions,
    ensure_audit_partitions,
    handle_audit_partitions,
    handle_expire_sessions,
    handle_prune_tasks,
    handle_reap_leases,
)
from cairn.tasks.dto import TaskContext


def context() -> TaskContext:
    return TaskContext(
        task_id=1,
        queue="maintain",
        kind="test",
        workspace_id=uuid4(),
        kb_id=None,
        document_id=None,
        payload={},
        attempt=1,
        max_attempts=3,
        correlation_id=None,
        worker_id="test",
    )


async def test_future_audit_partitions_are_created_idempotently() -> None:
    await ensure_audit_partitions(lookahead=4)
    assert await ensure_audit_partitions(lookahead=4) == 0
    async with session_scope() as session:
        partitions = await session.scalar(
            text("SELECT count(*) FROM pg_inherits WHERE inhparent = 'audit_log'::regclass")
        )
    assert partitions >= 5
    assert (await handle_audit_partitions(context())).ok


async def test_audit_retention_drops_only_whole_expired_months() -> None:
    async with transaction() as session:
        await session.execute(
            text(
                "CREATE TABLE audit_log_2000_01 PARTITION OF audit_log "
                "FOR VALUES FROM ('2000-01-01') TO ('2000-02-01')"
            )
        )
    assert await drop_expired_audit_partitions(retention_days=365) == 1
    assert await drop_expired_audit_partitions(retention_days=365) == 0
    async with session_scope() as session:
        assert await session.scalar(text("SELECT to_regclass('audit_log_2000_01')")) is None
        assert await session.scalar(text("SELECT to_regclass('audit_log')")) == "audit_log"


async def test_maintenance_handlers_succeed_with_no_expired_work() -> None:
    assert (await handle_reap_leases(context())).detail == "reaped=0 counters_reconciled=0"
    assert (await handle_prune_tasks(context())).detail == "pruned=0"
    assert (await handle_expire_sessions(context())).detail == "expired_sessions=0"
