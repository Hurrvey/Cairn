from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from apps.worker import main as worker_main
from cairn.catalog.maintenance import RuntimeRefreshScheduler


async def test_scheduler_refreshes_immediately_and_survives_failed_cycle() -> None:
    stop = asyncio.Event()
    calls = 0

    async def refresh(batch_size: int) -> None:
        nonlocal calls
        assert batch_size == 7
        calls += 1
        if calls == 1:
            raise RuntimeError("redis restart")
        stop.set()

    @asynccontextmanager
    async def leader():  # type: ignore[no-untyped-def]
        yield True

    scheduler = RuntimeRefreshScheduler(
        refresh=refresh,
        interval_s=0.001,
        batch_size=7,
        leader_lock=leader,
    )
    await asyncio.wait_for(scheduler.run(stop), timeout=1)

    assert calls == 2


async def test_maintain_run_bootstraps_and_stops_runtime_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Worker:
        def install_signal_handlers(self) -> None:
            pass

        async def run(self) -> None:
            events.append("worker")

    class Scheduler:
        async def run(self, stop: asyncio.Event) -> None:
            events.append("scheduler-started")
            await stop.wait()
            events.append("scheduler-stopped")

    async def noop() -> None:
        pass

    monkeypatch.setattr(worker_main, "build_worker", lambda _queue: Worker())
    monkeypatch.setattr(worker_main, "build_runtime_refresh_scheduler", lambda: Scheduler())
    monkeypatch.setattr("cairn.core.cache.close_cache", noop)
    monkeypatch.setattr("cairn.ingestion.runtime.close_pipeline_runtime", noop)
    monkeypatch.setattr(worker_main, "dispose_engine", noop)

    await worker_main._run("maintain")

    assert events == ["scheduler-started", "worker", "scheduler-stopped"]
