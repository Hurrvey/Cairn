"""Durable task queue — TC-M06-01 … TC-M06-22.

The properties under test are the ones ADR-0003 exists to provide: transactional
enqueue, exactly-once claiming, fairness, and no lost work when a worker dies.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from cairn.core.db import session_scope, transaction
from cairn.core.time import utcnow
from cairn.tasks.dto import TaskContext, TaskResult, TaskSpec
from cairn.tasks.repository import TaskRepository
from cairn.tasks.service import TaskService, is_retryable, next_delay
from cairn.tasks.worker import TaskWorker, reap_and_reconcile


@pytest.fixture
async def workspace_id() -> UUID:
    """A workspace to hang tasks off. `task` has no FK to it, but
    `workspace_runtime` does — and without that row the claim query's inner join
    silently excludes every task."""
    ws = uuid4()
    async with transaction() as session:
        await session.execute(
            text(
                "INSERT INTO workspace (id, name, slug) VALUES (:id, 'T', :slug) "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": ws, "slug": f"ws-{ws.hex[:8]}"},
        )
    return ws


@pytest.fixture
def service() -> TaskService:
    return TaskService()


@pytest.fixture
def repo() -> TaskRepository:
    return TaskRepository()


def spec(workspace_id: UUID, **overrides: object) -> TaskSpec:
    base: dict[str, object] = {
        "queue": "parse",
        "kind": "test.echo",
        "workspace_id": workspace_id,
        "payload": {"value": 1},
    }
    base.update(overrides)
    return TaskSpec(**base)  # type: ignore[arg-type]


async def _count(state: str) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(text("SELECT count(*) FROM task WHERE state = :s"), {"s": state})
            or 0
        )


# --- TC-M06-01 / 02: transactional enqueue -----------------------------------


async def test_enqueue_commits_with_the_callers_transaction(
    service: TaskService, workspace_id: UUID
) -> None:
    """TC-M06-01 / NFR-R-03."""
    async with transaction() as session:
        task_id = await service.enqueue(session, spec(workspace_id))
        assert task_id is not None
    assert await _count("ready") == 1


async def test_enqueue_rolls_back_with_the_callers_transaction(
    service: TaskService, workspace_id: UUID
) -> None:
    """TC-M06-01 — the property a message broker cannot give you.

    With a broker, the row and the job are two writes to two systems: crash
    between them and the entity sits unprocessed forever with nothing able to
    detect it.
    """

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        async with transaction() as session:
            await service.enqueue(session, spec(workspace_id))
            raise Boom

    assert await _count("ready") == 0


def test_enqueue_has_no_session_free_overload(service: TaskService) -> None:
    """TC-M06-02: the API cannot be used incorrectly.

    A session-less `enqueue` would silently reintroduce the dual-write hole, so
    the session is a required positional parameter rather than a convention.
    """
    import inspect

    parameters = list(inspect.signature(service.enqueue).parameters)
    assert parameters[0] == "session"
    assert not any(name.startswith("enqueue_without") for name in dir(service))


# --- TC-M06-03 … 07: claiming and fairness -----------------------------------


async def test_concurrent_workers_claim_each_task_exactly_once(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-03 — `FOR UPDATE ... SKIP LOCKED` with no coordinator."""
    async with transaction() as session:
        await repo.set_workspace_concurrency(session, workspace_id, 1000)
        for _ in range(60):
            await service.enqueue(session, spec(workspace_id))

    async def worker(name: str) -> list[int]:
        claimed: list[int] = []
        for _ in range(10):
            async with transaction() as session:
                rows = await repo.claim(
                    session, queue="parse", batch=5, worker_id=name, lease=timedelta(minutes=5)
                )
            claimed.extend(int(r["id"]) for r in rows)
            if not rows:
                break
        return claimed

    results = await asyncio.gather(*(worker(f"w{i}") for i in range(6)))
    all_ids = [task_id for batch in results for task_id in batch]

    assert len(all_ids) == 60
    assert len(set(all_ids)) == 60, "a task was claimed twice"


async def test_claim_respects_priority_then_fifo(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-04."""
    async with transaction() as session:
        await repo.set_workspace_concurrency(session, workspace_id, 1000)
        low = await service.enqueue(session, spec(workspace_id, priority=10))
        high = await service.enqueue(session, spec(workspace_id, priority=200))
        second_high = await service.enqueue(session, spec(workspace_id, priority=200))

    async with transaction() as session:
        rows = await repo.claim(
            session, queue="parse", batch=3, worker_id="w", lease=timedelta(minutes=5)
        )
    assert [int(r["id"]) for r in rows] == [high, second_high, low]


async def test_claim_skips_locked_candidates_without_hiding_other_ready_tasks(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    async with transaction() as session:
        await repo.set_workspace_concurrency(session, workspace_id, 1000)
        for _ in range(20):
            await service.enqueue(session, spec(workspace_id))

    async with transaction() as locking_session:
        await locking_session.execute(
            text("SELECT id FROM task ORDER BY priority DESC, id LIMIT 5 FOR UPDATE")
        )
        async with transaction() as claiming_session:
            rows = await repo.claim(
                claiming_session, queue="parse", batch=5, worker_id="w", lease=timedelta(minutes=5)
            )
        assert [row["id"] for row in rows] == [6, 7, 8, 9, 10]


async def test_claim_skips_tasks_scheduled_for_the_future(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-05 — how backoff keeps a retry out of the way."""
    async with transaction() as session:
        await service.enqueue(session, spec(workspace_id, run_after=utcnow() + timedelta(hours=1)))

    async with transaction() as session:
        rows = await repo.claim(
            session, queue="parse", batch=10, worker_id="w", lease=timedelta(minutes=5)
        )
    assert rows == []


async def test_workspace_at_its_concurrency_cap_is_skipped(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-06."""
    async with transaction() as session:
        await repo.set_workspace_concurrency(session, workspace_id, 2)
        for _ in range(5):
            await service.enqueue(session, spec(workspace_id))

    async with transaction() as session:
        first = await repo.claim(
            session, queue="parse", batch=2, worker_id="w", lease=timedelta(minutes=5)
        )
    assert len(first) == 2

    # `running` is now at the cap, so the next scan finds nothing.
    async with transaction() as session:
        second = await repo.claim(
            session, queue="parse", batch=5, worker_id="w", lease=timedelta(minutes=5)
        )
    assert second == []


async def test_a_saturated_workspace_does_not_starve_another(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-07 — the defining failure of a naive FIFO queue.

    Workspace A enqueues 50 documents; workspace B enqueues one. Without the
    fairness join, B waits behind all 50.
    """
    other = uuid4()
    async with transaction() as session:
        await session.execute(
            text("INSERT INTO workspace (id, name, slug) VALUES (:id, 'B', :slug)"),
            {"id": other, "slug": f"ws-{other.hex[:8]}"},
        )
        await repo.set_workspace_concurrency(session, workspace_id, 2)
        for _ in range(50):
            await service.enqueue(session, spec(workspace_id))
        await repo.set_workspace_concurrency(session, other, 2)
        latecomer = await service.enqueue(session, spec(other))

    async with transaction() as session:
        rows = await repo.claim(
            session, queue="parse", batch=10, worker_id="w", lease=timedelta(minutes=5)
        )

    claimed = {int(r["id"]) for r in rows}
    assert latecomer in claimed, "the second workspace was starved by the first"
    assert len(claimed) <= 4, "the per-workspace cap was not applied"


# --- TC-M06-08 / 09: leases --------------------------------------------------


async def test_an_abandoned_task_is_reclaimed(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-08 / NFR-R-04 — a worker killed mid-task loses no work.

    Handlers are idempotent, so running it again is safe; losing it would not be.
    """
    async with transaction() as session:
        task_id = await service.enqueue(session, spec(workspace_id))

    async with transaction() as session:
        rows = await repo.claim(
            session, queue="parse", batch=1, worker_id="doomed", lease=timedelta(seconds=1)
        )
    assert len(rows) == 1

    # Simulate the worker vanishing: nothing renews the lease.
    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": task_id},
        )

    reaped, _ = await reap_and_reconcile(repo)
    assert reaped == 1

    async with session_scope() as session:
        state = await session.scalar(text("SELECT state FROM task WHERE id = :id"), {"id": task_id})
    assert state == "ready"


async def test_heartbeat_protects_a_long_task_from_the_reaper(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-09."""
    async with transaction() as session:
        task_id = await service.enqueue(session, spec(workspace_id))
    async with transaction() as session:
        await repo.claim(session, queue="parse", batch=1, worker_id="w", lease=timedelta(seconds=1))
        assert task_id is not None
        await repo.heartbeat(
            session, task_id=task_id, worker_id="w", attempt=1, lease=timedelta(minutes=30)
        )

    reaped, _ = await reap_and_reconcile(repo)
    assert reaped == 0


async def test_reaper_corrects_counter_drift(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """The counter is maintained incrementally, so a process killed between the
    UPDATE and the COMMIT can leave it wrong. Drift must be self-correcting
    rather than throttling a workspace forever."""
    async with transaction() as session:
        await session.execute(
            text(
                "INSERT INTO workspace_runtime (workspace_id, running) VALUES (:ws, 7) "
                "ON CONFLICT (workspace_id) DO UPDATE SET running = 7"
            ),
            {"ws": workspace_id},
        )

    _, drifted = await reap_and_reconcile(repo)
    assert drifted >= 1

    async with session_scope() as session:
        running = await session.scalar(
            text("SELECT running FROM workspace_runtime WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
    assert running == 0


# --- TC-M06-10 … 12: retries -------------------------------------------------


def test_retryable_classification() -> None:
    """TC-M06-11 — burning five attempts on a corrupt file wastes ~10 minutes of
    queue capacity and delays every good document behind it."""
    assert is_retryable("UPSTREAM_UNAVAILABLE") is True
    assert is_retryable("PARSE_ENCRYPTED_PDF") is False
    assert is_retryable("EMBEDDING_DIMENSION_MISMATCH") is False
    assert is_retryable("SOMETHING_NEW", default=True) is True


def test_backoff_grows_and_is_jittered() -> None:
    """TC-M06-10. Jitter matters more than the exponent: without it, a provider
    outage that fails 500 tasks produces 500 simultaneous retries."""
    assert next_delay(1) < next_delay(5)
    assert next_delay(20).total_seconds() <= 300 * 1.5
    samples = {next_delay(3).total_seconds() for _ in range(20)}
    assert len(samples) > 1, "backoff is not jittered"


# --- TC-M06-13 / 14: deduplication -------------------------------------------


async def test_dedupe_key_prevents_a_duplicate_in_flight_task(
    service: TaskService, workspace_id: UUID
) -> None:
    """TC-M06-13."""
    async with transaction() as session:
        first = await service.enqueue(session, spec(workspace_id, dedupe_key="parse:doc1:1"))
    async with transaction() as session:
        second = await service.enqueue(session, spec(workspace_id, dedupe_key="parse:doc1:1"))

    assert first == second
    assert await _count("ready") == 1


async def test_dedupe_key_is_reusable_once_the_task_finishes(
    service: TaskService, workspace_id: UUID
) -> None:
    """TC-M06-14 — what makes `{kind}:{entity}:{revision}` a usable convention
    rather than a one-shot."""
    async with transaction() as session:
        first = await service.enqueue(session, spec(workspace_id, dedupe_key="parse:doc1:1"))
    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET state='done', finished_at=now() WHERE id=:id"), {"id": first}
        )
    async with transaction() as session:
        second = await service.enqueue(session, spec(workspace_id, dedupe_key="parse:doc1:1"))

    assert second != first


# --- TC-M06-15: cancellation -------------------------------------------------


async def test_cancel_stops_a_ready_task_but_not_a_running_one(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-15 — interrupting a running task mid-write is how partial state
    gets created, so a claimed task is left to finish."""
    async with transaction() as session:
        ready_id = await service.enqueue(session, spec(workspace_id))
        running_id = await service.enqueue(session, spec(workspace_id, dedupe_key="other"))

    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET state='running' WHERE id=:id"), {"id": running_id}
        )

    assert await service.cancel(ready_id) is True
    assert await service.cancel(running_id) is False


# --- worker execution --------------------------------------------------------


async def test_worker_executes_and_completes_a_task(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    seen: list[dict[str, object]] = []

    worker = TaskWorker("parse", concurrency=2, repository=repo, poll_interval=0.05)

    async def handler(ctx: TaskContext) -> TaskResult:
        seen.append(dict(ctx.payload))
        await ctx.report_progress(1, 1)
        return TaskResult.success("done")

    worker.register("test.echo", handler)

    async with transaction() as session:
        await repo.set_workspace_concurrency(session, workspace_id, 10)
        await service.enqueue(session, spec(workspace_id, payload={"value": 42}))

    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if await _count("done"):
            break
        await asyncio.sleep(0.05)
    worker.request_shutdown()
    await asyncio.wait_for(task, timeout=10)

    assert seen == [{"value": 42}]
    assert await _count("done") == 1


async def test_a_terminal_failure_is_not_retried(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """TC-M06-11 at the worker level."""
    worker = TaskWorker("parse", concurrency=1, repository=repo, poll_interval=0.05)

    async def handler(ctx: TaskContext) -> TaskResult:
        return TaskResult.failure("PARSE_ENCRYPTED_PDF", "password protected")

    worker.register("test.echo", handler)

    async with transaction() as session:
        await repo.set_workspace_concurrency(session, workspace_id, 10)
        task_id = await service.enqueue(session, spec(workspace_id))

    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if await _count("failed"):
            break
        await asyncio.sleep(0.05)
    worker.request_shutdown()
    await asyncio.wait_for(task, timeout=10)

    status = await service.get_status(task_id)
    assert status is not None
    assert status.state == "failed"
    assert status.error_code == "PARSE_ENCRYPTED_PDF"
    assert status.attempt == 1, "a terminal failure must not consume further attempts"


async def test_an_unregistered_kind_fails_terminally(
    service: TaskService, repo: TaskRepository, workspace_id: UUID
) -> None:
    """A task nobody can run must surface in the dead-letter view rather than
    being retried forever by workers that will never have the handler."""
    worker = TaskWorker("parse", concurrency=1, repository=repo, poll_interval=0.05)
    worker.register("test.echo", lambda ctx: _ok())

    async with transaction() as session:
        await repo.set_workspace_concurrency(session, workspace_id, 10)
        task_id = await service.enqueue(session, spec(workspace_id, kind="test.unknown"))

    task = asyncio.create_task(worker.run())
    for _ in range(100):
        if await _count("failed"):
            break
        await asyncio.sleep(0.05)
    worker.request_shutdown()
    await asyncio.wait_for(task, timeout=10)

    status = await service.get_status(task_id)
    assert status is not None and status.error_code == "NO_HANDLER"


async def _ok() -> TaskResult:
    return TaskResult.success()


# --- observability -----------------------------------------------------------


async def test_queue_depth_and_starvation_age(service: TaskService, workspace_id: UUID) -> None:
    """TC-M06-20 / NFR-S-06 — KEDA scales on depth; the age is what reveals a
    queue that looks healthy while one task sits behind a saturated workspace."""
    async with transaction() as session:
        for _ in range(3):
            await service.enqueue(session, spec(workspace_id))
        await service.enqueue(session, spec(workspace_id, queue="embed", dedupe_key="e1"))

    depth = await service.queue_depth()
    assert depth["parse"] == 3
    assert depth["embed"] == 1
    assert depth["ocr"] == 0

    ages = await service.oldest_ready_age()
    assert ages["parse"] >= 0
