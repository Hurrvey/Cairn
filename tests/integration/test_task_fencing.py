"""Task lease fencing regressions against real PostgreSQL."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from cairn.core.db import session_scope, transaction
from cairn.tasks.dto import TaskContext, TaskSpec
from cairn.tasks.repository import TaskRepository
from cairn.tasks.service import TaskService


async def test_lease_expiry_uses_wall_clock_not_transaction_start(
    service: TaskService, repository: TaskRepository, workspace_id: UUID
) -> None:
    """NFR-R-05: a long transaction cannot resurrect an already expired lease."""
    async with transaction() as session:
        task_id = await service.enqueue(session, task_spec(workspace_id))
    assert task_id is not None
    async with transaction() as session:
        await repository.claim(
            session, queue="parse", batch=1, worker_id="worker-a", lease=timedelta(minutes=1)
        )
    async with transaction() as session:
        await session.execute(text("SELECT pg_sleep(0.03)"))
        await session.execute(
            text(
                "UPDATE task SET lease_until = clock_timestamp() - interval '1 millisecond' "
                "WHERE id = :task_id"
            ),
            {"task_id": task_id},
        )
        assert (
            await repository.heartbeat(
                session,
                task_id=task_id,
                worker_id="worker-a",
                attempt=1,
                lease=timedelta(minutes=1),
            )
            is False
        )


@pytest.fixture
async def workspace_id() -> UUID:
    workspace_id = uuid4()
    async with transaction() as session:
        await session.execute(
            text("INSERT INTO workspace (id, name, slug) VALUES (:id, 'Fencing', :slug)"),
            {"id": workspace_id, "slug": f"fencing-{workspace_id.hex[:8]}"},
        )
    return workspace_id


@pytest.fixture
def repository() -> TaskRepository:
    return TaskRepository()


@pytest.fixture
def service(repository: TaskRepository) -> TaskService:
    return TaskService(repository)


def task_spec(workspace_id: UUID) -> TaskSpec:
    return TaskSpec(
        queue="parse",
        kind="test.fencing",
        workspace_id=workspace_id,
        payload={"value": 1},
    )


def task_context(row: dict[str, object], worker_id: str) -> TaskContext:
    return TaskContext(
        task_id=int(row["id"]),
        queue=str(row["queue"]),
        kind=str(row["kind"]),
        workspace_id=row["workspace_id"],  # type: ignore[arg-type]
        kb_id=None,
        document_id=None,
        payload={},
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        correlation_id=None,
        worker_id=worker_id,
    )


async def test_stale_heartbeat_cannot_extend_a_reclaimed_attempt(
    service: TaskService, repository: TaskRepository, workspace_id: UUID
) -> None:
    async with transaction() as session:
        task_id = await service.enqueue(session, task_spec(workspace_id))
    assert task_id is not None

    async with transaction() as session:
        first_claim = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-a",
            lease=timedelta(minutes=5),
        )
    assert first_claim[0]["attempt"] == 1

    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": task_id},
        )
        assert await repository.reap_expired_leases(session) == 1

    async with transaction() as session:
        second_claim = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-b",
            lease=timedelta(minutes=5),
        )
    assert second_claim[0]["attempt"] == 2
    second_lease_until = second_claim[0]["lease_until"]

    async with transaction() as session:
        applied = await repository.heartbeat(
            session,
            task_id=task_id,
            worker_id="worker-a",
            attempt=1,
            lease=timedelta(hours=1),
        )

    async with session_scope() as session:
        row = (
            await session.execute(
                text("SELECT worker_id, attempt, lease_until FROM task WHERE id = :id"),
                {"id": task_id},
            )
        ).one()

    assert applied is False
    assert row.worker_id == "worker-b"
    assert row.attempt == 2
    assert row.lease_until == second_lease_until


async def test_stale_progress_cannot_update_a_reclaimed_attempt(
    service: TaskService, repository: TaskRepository, workspace_id: UUID
) -> None:
    async with transaction() as session:
        task_id = await service.enqueue(session, task_spec(workspace_id))
    assert task_id is not None

    async with transaction() as session:
        first_claim = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-a",
            lease=timedelta(minutes=5),
        )
    assert first_claim[0]["attempt"] == 1

    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": task_id},
        )
        assert await repository.reap_expired_leases(session) == 1

    async with transaction() as session:
        second_claim = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-b",
            lease=timedelta(minutes=5),
        )
    assert second_claim[0]["attempt"] == 2

    async with transaction() as session:
        applied = await repository.set_progress(
            session,
            task_id=task_id,
            worker_id="worker-a",
            attempt=1,
            done=99,
            total=100,
        )

    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT worker_id, attempt, progress_done, progress_total "
                    "FROM task WHERE id = :id"
                ),
                {"id": task_id},
            )
        ).one()

    assert applied is False
    assert row.worker_id == "worker-b"
    assert row.attempt == 2
    assert row.progress_done == 0
    assert row.progress_total is None


async def test_stale_finish_cannot_complete_a_reclaimed_attempt_or_decrement_running(
    service: TaskService, repository: TaskRepository, workspace_id: UUID
) -> None:
    async with transaction() as session:
        task_id = await service.enqueue(session, task_spec(workspace_id))
    assert task_id is not None

    async with transaction() as session:
        first_claim = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-a",
            lease=timedelta(minutes=5),
        )
    assert first_claim[0]["attempt"] == 1

    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": task_id},
        )
        assert await repository.reap_expired_leases(session) == 1

    async with transaction() as session:
        second_claim = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-b",
            lease=timedelta(minutes=5),
        )
    assert second_claim[0]["attempt"] == 2

    async with transaction() as session:
        applied = await repository.finish(
            session,
            task_id=task_id,
            worker_id="worker-a",
            attempt=1,
            state="done",
        )

    async with session_scope() as session:
        task_row = (
            await session.execute(
                text("SELECT state, worker_id, attempt FROM task WHERE id = :id"),
                {"id": task_id},
            )
        ).one()
        running = await session.scalar(
            text("SELECT running FROM workspace_runtime WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )

    assert applied is False
    assert task_row.state == "running"
    assert task_row.worker_id == "worker-b"
    assert task_row.attempt == 2
    assert running == 1


async def test_stale_reschedule_cannot_release_a_reclaimed_attempt_or_decrement_running(
    service: TaskService, repository: TaskRepository, workspace_id: UUID
) -> None:
    async with transaction() as session:
        task_id = await service.enqueue(session, task_spec(workspace_id))
    assert task_id is not None

    async with transaction() as session:
        first_claim = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-a",
            lease=timedelta(minutes=5),
        )
    assert first_claim[0]["attempt"] == 1

    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": task_id},
        )
        assert await repository.reap_expired_leases(session) == 1

    async with transaction() as session:
        second_claim = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-b",
            lease=timedelta(minutes=5),
        )
    assert second_claim[0]["attempt"] == 2

    async with transaction() as session:
        applied = await repository.reschedule(
            session,
            task_id=task_id,
            worker_id="worker-a",
            attempt=1,
            delay=timedelta(minutes=1),
            error_code="TIMEOUT",
            error_detail="stale attempt",
        )

    async with session_scope() as session:
        task_row = (
            await session.execute(
                text("SELECT state, worker_id, attempt, error_code FROM task WHERE id = :id"),
                {"id": task_id},
            )
        ).one()
        running = await session.scalar(
            text("SELECT running FROM workspace_runtime WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )

    assert applied is False
    assert task_row.state == "running"
    assert task_row.worker_id == "worker-b"
    assert task_row.attempt == 2
    assert task_row.error_code == "LEASE_EXPIRED"
    assert running == 1


async def test_lock_owned_task_rejects_an_expired_lease_before_reaping(
    service: TaskService, repository: TaskRepository, workspace_id: UUID
) -> None:
    async with transaction() as session:
        await service.enqueue(session, task_spec(workspace_id))
    async with transaction() as session:
        claimed = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id="worker-a",
            lease=timedelta(minutes=5),
        )
    context = task_context(claimed[0], "worker-a")

    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": context.task_id},
        )

    with pytest.raises(RuntimeError, match="lease"):
        async with transaction() as session:
            await service.lock_owned_task(session, context)
