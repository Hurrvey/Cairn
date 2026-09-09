"""Worker entrypoint.

One process, one queue (``CAIRN_TASKS__QUEUE``). Handlers are registered by the
modules that own them; Phase 1 ships only ``maintain``, whose jobs keep the
queue itself and the audit partitions healthy.
"""

from __future__ import annotations

import asyncio
import sys

from cairn.core.config import get_settings
from cairn.core.db import dispose_engine
from cairn.core.logging import configure_logging, get_logger
from cairn.tasks.dto import QUEUE_PROFILES
from cairn.tasks.worker import TaskWorker

log = get_logger(__name__)


def build_worker(queue: str) -> TaskWorker:
    settings = get_settings()
    worker = TaskWorker(
        queue,
        concurrency=settings.tasks.concurrency,
        poll_interval=settings.tasks.poll_interval_s,
    )

    if queue == "maintain":
        from cairn.platform.maintenance import register_maintenance_handlers

        register_maintenance_handlers(worker)

    if queue in {"parse", "chunk", "embed", "index"}:
        from cairn.ingestion.pipeline import register_pipeline_handlers
        from cairn.ingestion.runtime import get_pipeline_runtime

        register_pipeline_handlers(worker, get_pipeline_runtime().pipeline)

    return worker


async def _run(queue: str) -> None:
    worker = build_worker(queue)
    worker.install_signal_handlers()
    try:
        await worker.run()
    finally:
        from cairn.core.cache import close_cache
        from cairn.ingestion.runtime import close_pipeline_runtime

        await close_pipeline_runtime()
        await close_cache()
        await dispose_engine()


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    queue = settings.tasks.queue
    if not queue:
        log.error(
            "worker.no_queue",
            detail="CAIRN_TASKS__QUEUE is required when running a worker",
            valid=sorted(QUEUE_PROFILES),
        )
        sys.exit(78)  # EX_CONFIG
    if queue not in QUEUE_PROFILES:
        log.error("worker.unknown_queue", queue=queue, valid=sorted(QUEUE_PROFILES))
        sys.exit(78)

    asyncio.run(_run(queue))


if __name__ == "__main__":  # pragma: no cover
    main()
