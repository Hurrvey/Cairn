"""Focused integration coverage for bounded rebuild enrollment."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.catalog.dto import KnowledgeBaseRuntime, ReindexSpec, UploadSpec
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.catalog.repository import CatalogRepository
from cairn.core.db import session_scope, transaction
from cairn.core.errors import ValidationFailed
from cairn.ingestion.artifacts import decode_parse_manifest
from cairn.ingestion.pipeline import register_pipeline_handlers
from cairn.objectstore.local import LocalObjectStore
from cairn.tasks.dto import TaskContext, TaskSpec
from cairn.tasks.repository import TaskRepository
from cairn.tasks.service import get_task_service
from cairn.tasks.worker import TaskWorker

__all__ = ["pipeline_admin"]


@pytest.mark.parametrize("batch_size", [0, 1001])
def test_reindex_fanout_batch_size_is_bounded(batch_size: int) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    with pytest.raises(ValueError, match="between 1 and 1000"):
        ReindexFanoutService(batch_size=batch_size)


async def test_initial_upload_version_is_enrollment_complete(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, _registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)

    async with session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT enrollment_state, enrollment_cursor, enrollment_generation "
                    "FROM kb_index_version WHERE kb_id=:kb_id AND version=1"
                ),
                {"kb_id": kb.id},
            )
        ).one()

    assert row.enrollment_state == "complete"
    assert row.enrollment_cursor is None
    assert row.enrollment_generation == 0


async def test_one_document_batches_resume_and_generation_replay_is_safe(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, _first, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    second_source = b"# Second\n\nIndependent document."
    second_hash = sha256(second_source).hexdigest()
    second_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{second_hash}"
    await LocalObjectStore(tmp_path).put(second_key, second_source, content_type="text/markdown")
    await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="second.md",
            content_hash=second_hash,
            size_bytes=len(second_source),
            mime_type="text/markdown",
            object_key=second_key,
        ),
    )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await _execute_pipeline(pipeline)
    assert (await catalog.get_kb(kb.id)).active_index_version == 1

    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    fanout = ReindexFanoutService(batch_size=1)
    worker = TaskWorker("maintain", concurrency=1, poll_interval=0.01)
    worker.register("kb.reindex_fanout", fanout.handle)

    await _execute_one(worker)
    async with session_scope() as session:
        first_batch = (
            await session.execute(
                text(
                    "SELECT enrollment_state, enrollment_cursor, enrollment_generation "
                    "FROM kb_index_version WHERE kb_id=:kb_id AND version=2"
                ),
                {"kb_id": kb.id},
            )
        ).one()
        ledgers = await session.scalar(
            text("SELECT count(*) FROM document_ingestion WHERE kb_id=:kb_id AND index_version=2"),
            {"kb_id": kb.id},
        )
        parse_tasks = await session.scalar(
            text(
                "SELECT count(*) FROM task WHERE kb_id=:kb_id AND kind='document.parse' "
                "AND payload->>'index_version'='2'"
            ),
            {"kb_id": kb.id},
        )
    assert first_batch.enrollment_state == "scanning"
    assert first_batch.enrollment_cursor is not None
    assert first_batch.enrollment_generation == 1
    assert ledgers == 1
    assert parse_tasks == 1

    await _execute_pipeline(pipeline)
    after_first_index = await catalog.get_kb(kb.id)
    assert after_first_index.active_index_version == 1
    assert after_first_index.building_index_version == 2

    async with transaction() as session:
        await get_task_service().enqueue(
            session,
            TaskSpec(
                queue="maintain",
                kind="kb.reindex_fanout",
                workspace_id=pipeline_admin.workspace_id,
                kb_id=kb.id,
                payload={"index_version": 2, "enrollment_generation": 0},
                priority=100,
                dedupe_key=f"test.stale-reindex:{kb.id}:2:0",
            ),
        )
    await _execute_one(worker)
    async with session_scope() as session:
        replay_state = (
            await session.execute(
                text(
                    "SELECT enrollment_generation, "
                    "(SELECT count(*) FROM document_ingestion "
                    " WHERE kb_id=:kb_id AND index_version=2) AS ledgers "
                    "FROM kb_index_version WHERE kb_id=:kb_id AND version=2"
                ),
                {"kb_id": kb.id},
            )
        ).one()
    assert replay_state.enrollment_generation == 1
    assert replay_state.ledgers == 1

    await _execute_one(worker)
    await _execute_pipeline(pipeline)
    await _execute_one(worker)
    await _execute_one(worker)

    completed = await catalog.get_kb(kb.id)
    assert completed.active_index_version == 2
    assert completed.building_index_version is None
    async with session_scope() as session:
        final = (
            await session.execute(
                text(
                    "SELECT enrollment_state, enrollment_generation, "
                    "(SELECT count(*) FROM document_ingestion "
                    " WHERE kb_id=:kb_id AND index_version=2) AS ledgers, "
                    "(SELECT count(*) FROM task WHERE kb_id=:kb_id "
                    " AND kind='document.parse' "
                    " AND payload->>'index_version'='2') AS parse_tasks "
                    "FROM kb_index_version WHERE kb_id=:kb_id AND version=2"
                ),
                {"kb_id": kb.id},
            )
        ).one()
    assert final.enrollment_state == "complete"
    assert final.ledgers == 2
    assert final.parse_tasks == 2


async def test_stale_concurrent_generation_cannot_regress_cursor_or_generation(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, _registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    async with transaction() as session:
        await get_task_service().enqueue(
            session,
            TaskSpec(
                queue="maintain",
                kind="kb.reindex_fanout",
                workspace_id=pipeline_admin.workspace_id,
                kb_id=kb.id,
                payload={"index_version": 2, "enrollment_generation": 0},
                priority=50,
                dedupe_key=f"test.concurrent-reindex:{kb.id}:2:0",
            ),
        )

    repository = TaskRepository()
    async with transaction() as session:
        stale_row = (
            await repository.claim(
                session,
                queue="maintain",
                batch=1,
                worker_id="stale-fanout",
                lease=timedelta(minutes=5),
            )
        )[0]
    async with transaction() as session:
        current_row = (
            await repository.claim(
                session,
                queue="maintain",
                batch=1,
                worker_id="current-fanout",
                lease=timedelta(minutes=5),
            )
        )[0]

    observed = asyncio.Event()
    release = asyncio.Event()

    class DelayedRepository(CatalogRepository):
        async def fanout_document_ids(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            selected = await super().fanout_document_ids(*args, **kwargs)
            observed.set()
            await release.wait()
            return selected

    stale_service = ReindexFanoutService(batch_size=1, repository=DelayedRepository())
    current_service = ReindexFanoutService(batch_size=1)
    stale_task = asyncio.create_task(stale_service.handle(_task_context(stale_row, "stale-fanout")))
    await asyncio.wait_for(observed.wait(), timeout=5)
    try:
        await current_service.handle(_task_context(current_row, "current-fanout"))
        async with transaction() as session:
            assert await repository.finish(
                session,
                task_id=current_row["id"],
                worker_id="current-fanout",
                attempt=current_row["attempt"],
                state="done",
            )
        async with transaction() as session:
            next_row = (
                await repository.claim(
                    session,
                    queue="maintain",
                    batch=1,
                    worker_id="next-fanout",
                    lease=timedelta(minutes=5),
                )
            )[0]
        await current_service.handle(_task_context(next_row, "next-fanout"))
    finally:
        release.set()
    stale_result = await asyncio.wait_for(stale_task, timeout=5)

    assert stale_result.ok
    assert stale_result.detail == "skipped: reindex enrollment state changed"
    async with session_scope() as session:
        state = (
            await session.execute(
                text(
                    "SELECT enrollment_state, enrollment_cursor, enrollment_generation, "
                    "(SELECT count(*) FROM document_ingestion WHERE kb_id=:kb_id "
                    " AND index_version=2) AS ledgers, "
                    "(SELECT count(*) FROM task WHERE kb_id=:kb_id AND kind='document.parse' "
                    " AND payload->>'index_version'='2') AS parse_tasks "
                    "FROM kb_index_version WHERE kb_id=:kb_id AND version=2"
                ),
                {"kb_id": kb.id},
            )
        ).one()
    assert state.enrollment_state == "reconciling"
    assert state.enrollment_cursor is not None
    assert state.enrollment_generation == 2
    assert state.ledgers == 1
    assert state.parse_tasks == 1


async def test_final_fanout_retry_republishes_a_committed_activation(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, _registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    publish_calls = 0

    async def publish_runtime(kb_id):  # type: ignore[no-untyped-def]
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise RuntimeError("simulated publication failure")
        await catalog.publish_runtime(kb_id)

    service = ReindexFanoutService(batch_size=1, publish_runtime=publish_runtime)
    worker = TaskWorker("maintain", concurrency=1, poll_interval=0.01)
    worker.register("kb.reindex_fanout", service.handle)
    await _execute_one(worker)
    await _execute_pipeline(pipeline)
    await _execute_one(worker)
    await _execute_one(worker)

    assert publish_calls == 1
    assert (await catalog.get_kb(kb.id)).active_index_version == 2
    cached = await catalog.cache.get(f"kb:runtime:{kb.id}")
    assert cached is not None
    assert KnowledgeBaseRuntime.model_validate_json(cached).index_version == 1

    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE task SET run_after=now() WHERE kb_id=:kb_id "
                "AND kind='kb.reindex_fanout' AND state='ready'"
            ),
            {"kb_id": kb.id},
        )
    await _execute_one(worker)

    assert publish_calls == 2
    repaired = await catalog.cache.get(f"kb:runtime:{kb.id}")
    assert repaired is not None
    assert KnowledgeBaseRuntime.model_validate_json(repaired).index_version == 2
    async with session_scope() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM document_ingestion WHERE kb_id=:kb_id AND index_version=2"
                ),
                {"kb_id": kb.id},
            )
            == 1
        )


async def test_now_empty_rebuild_fails_without_replacing_active_version(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    await catalog.delete_document(pipeline_admin, registration.document.id)
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE task SET run_after=clock_timestamp()+interval '1 day' "
                "WHERE document_id=:document_id AND kind='document.purge'"
            ),
            {"document_id": registration.document.id},
        )

    service = ReindexFanoutService(batch_size=1)
    worker = TaskWorker("maintain", concurrency=1, poll_interval=0.01)
    worker.register("kb.reindex_fanout", service.handle)
    await _execute_one(worker)
    await _execute_one(worker)

    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version is None
    assert current.status == "active"
    versions = await catalog.list_index_versions(kb.id)
    failed = next(version for version in versions if version.version == 2)
    assert failed.state == "failed"
    assert "no live documents" in (failed.error or "").lower()


async def test_empty_rebuild_is_rejected_before_version_allocation(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.delete_document(pipeline_admin, registration.document.id)

    with pytest.raises(ValidationFailed, match="no live documents"):
        await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))

    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version is None
    assert [version.version for version in await catalog.list_index_versions(kb.id)] == [1]


async def test_explicit_activation_obeys_real_rebuild_enrollment_barrier(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.core.errors import Conflict

    catalog, kb, _registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))

    with pytest.raises(Conflict, match="not ready for activation"):
        await catalog.activate_index_version(kb.id, 2)

    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version == 2


async def test_explicit_now_empty_activation_reports_committed_abandonment(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.core.errors import Conflict

    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    await catalog.delete_document(pipeline_admin, registration.document.id)
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE kb_index_version SET enrollment_state='complete' "
                "WHERE kb_id=:kb_id AND version=2"
            ),
            {"kb_id": kb.id},
        )

    with pytest.raises(Conflict, match="abandoned"):
        await catalog.activate_index_version(kb.id, 2)

    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version is None
    versions = await catalog.list_index_versions(kb.id)
    failed = next(version for version in versions if version.version == 2)
    assert failed.state == "failed"
    cached = await catalog.cache.get(f"kb:runtime:{kb.id}")
    assert cached is not None
    assert KnowledgeBaseRuntime.model_validate_json(cached).index_version == 1


async def test_out_of_scope_prior_parse_pointer_falls_back_without_outside_read(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, registration, _source, source_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, _provider, objects, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    async with session_scope() as session:
        original_parsed_key = await session.scalar(
            text(
                "SELECT parsed_object_key FROM document_ingestion "
                "WHERE document_id=:document_id AND index_version=1"
            ),
            {"document_id": registration.document.id},
        )
    assert original_parsed_key is not None

    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    fanout_worker = TaskWorker("maintain", concurrency=1, poll_interval=0.01)
    fanout_worker.register("kb.reindex_fanout", ReindexFanoutService(batch_size=1).handle)
    await _execute_one(fanout_worker)
    outside_key = f"{pipeline_admin.workspace_id}/outside-kb/i/forged/parsed.json"
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE document_ingestion SET prior_parsed_object_key=:outside_key "
                "WHERE document_id=:document_id AND index_version=2"
            ),
            {
                "document_id": registration.document.id,
                "outside_key": outside_key,
            },
        )

    reads: list[str] = []

    class RecordingStore:
        async def get_bytes(self, key, **kwargs):  # type: ignore[no-untyped-def]
            reads.append(key)
            return await objects.get_bytes(key, **kwargs)

        def __getattr__(self, name: str):
            return getattr(objects, name)

    async def object_store_for(_binding):  # type: ignore[no-untyped-def]
        return RecordingStore()

    pipeline._resources = replace(pipeline._resources, object_store_for=object_store_for)
    parse_worker = TaskWorker("parse", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(parse_worker, pipeline)
    await _execute_one(parse_worker)

    assert outside_key not in reads
    assert reads == [source_key]
    async with session_scope() as session:
        republished_key = await session.scalar(
            text(
                "SELECT parsed_object_key FROM document_ingestion "
                "WHERE document_id=:document_id AND index_version=2"
            ),
            {"document_id": registration.document.id},
        )
    assert republished_key is not None
    assert republished_key != original_parsed_key
    manifest = decode_parse_manifest(
        await objects.get_bytes(republished_key, max_bytes=256 * 1024 * 1024)
    )
    assert manifest.document_id == registration.document.id
    assert manifest.revision == 1
    assert manifest.index_version == 2


async def test_terminal_document_failure_blocks_completed_enrollment_activation(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    fanout = TaskWorker("maintain", concurrency=1, poll_interval=0.01)
    fanout.register("kb.reindex_fanout", ReindexFanoutService(batch_size=1).handle)
    await _execute_one(fanout)
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE document_ingestion SET prior_parsed_object_key=:missing "
                "WHERE document_id=:document_id AND index_version=2"
            ),
            {
                "document_id": registration.document.id,
                "missing": f"{pipeline_admin.workspace_id}/{kb.id}/i/missing/parsed.json",
            },
        )

    async def terminal_parse(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("terminal parser failure")

    monkeypatch.setattr(pipeline._resources.parsers, "parse", terminal_parse)
    parse_worker = TaskWorker("parse", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(parse_worker, pipeline)
    await _execute_one(parse_worker)
    await _execute_one(fanout)
    await _execute_one(fanout)

    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version == 2
    async with session_scope() as session:
        state = (
            await session.execute(
                text(
                    "SELECT version.enrollment_state, ingestion.state "
                    "FROM kb_index_version version "
                    "JOIN document_ingestion ingestion ON ingestion.kb_id=version.kb_id "
                    "AND ingestion.index_version=version.version "
                    "WHERE version.kb_id=:kb_id AND version.version=2"
                ),
                {"kb_id": kb.id},
            )
        ).one()
    assert state.enrollment_state == "complete"
    assert state.state == "failed"


async def test_replacement_behind_cursor_enrolls_current_revision_before_activation(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    fanout = TaskWorker("maintain", concurrency=1, poll_interval=0.01)
    fanout.register("kb.reindex_fanout", ReindexFanoutService(batch_size=1).handle)
    await _execute_one(fanout)

    replacement = b"# Replacement\n\nCommitted behind the durable UUID cursor."
    replacement_hash = sha256(replacement).hexdigest()
    replacement_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{replacement_hash}"
    await LocalObjectStore(tmp_path).put(replacement_key, replacement, content_type="text/markdown")
    revision = await CatalogIngestionFacade().start_revision(
        registration.document.id,
        object_key=replacement_key,
        content_hash=replacement_hash,
        size_bytes=len(replacement),
        mime_type="text/markdown",
    )
    assert revision == 2

    workers = [fanout]
    for queue in ("parse", "chunk", "embed", "index"):
        worker = TaskWorker(queue, concurrency=2, poll_interval=0.01)
        register_pipeline_handlers(worker, pipeline)
        workers.append(worker)
    for _attempt in range(20):
        for worker in workers:
            for row in await worker._claim_batch():
                await worker._execute(row)
        if (await catalog.get_kb(kb.id)).active_index_version == 2:
            break

    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 2
    assert current.building_index_version is None
    async with session_scope() as session:
        runs = (
            await session.execute(
                text(
                    "SELECT revision, state, source_content_hash FROM document_ingestion "
                    "WHERE document_id=:document_id AND index_version=2 ORDER BY revision"
                ),
                {"document_id": registration.document.id},
            )
        ).all()
    assert [(run.revision, run.state) for run in runs] == [(1, "registered"), (2, "indexed")]
    assert runs[1].source_content_hash == replacement_hash


async def test_deleted_enrolled_document_cleanup_creates_missing_build_namespace(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService
    from cairn.vectorstore.base import Namespace

    catalog, kb, _first, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    survivor = b"# Survivor\n\nCreates the later build points."
    survivor_hash = sha256(survivor).hexdigest()
    survivor_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{survivor_hash}"
    await LocalObjectStore(tmp_path).put(survivor_key, survivor, content_type="text/markdown")
    await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="survivor.md",
            content_hash=survivor_hash,
            size_bytes=len(survivor),
            mime_type="text/markdown",
            object_key=survivor_key,
        ),
    )
    pipeline, _provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    fanout = TaskWorker("maintain", concurrency=1, poll_interval=0.01)
    fanout.register("kb.reindex_fanout", ReindexFanoutService(batch_size=1).handle)
    await _execute_one(fanout)
    async with session_scope() as session:
        deleted_id = await session.scalar(
            text(
                "SELECT document_id FROM document_ingestion WHERE kb_id=:kb_id AND index_version=2"
            ),
            {"kb_id": kb.id},
        )
    assert deleted_id is not None
    await catalog.delete_document(pipeline_admin, deleted_id)
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE task SET run_after=clock_timestamp()+interval '1 day' "
                "WHERE document_id=:document_id AND kind='document.purge'"
            ),
            {"document_id": deleted_id},
        )

    index_worker = TaskWorker("index", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(index_worker, pipeline)
    await _execute_one(index_worker)
    assert await vectors.namespace_exists(Namespace(kb.id, 2))
    async with session_scope() as session:
        cleanup_state = await session.scalar(
            text(
                "SELECT state FROM task WHERE document_id=:document_id "
                "AND kind='document.reindex_delete'"
            ),
            {"document_id": deleted_id},
        )
    assert cleanup_state == "done"

    workers = [fanout]
    for queue in ("parse", "chunk", "embed", "index"):
        worker = TaskWorker(queue, concurrency=1, poll_interval=0.01)
        register_pipeline_handlers(worker, pipeline)
        workers.append(worker)
    for _attempt in range(20):
        for worker in workers:
            for row in await worker._claim_batch():
                await worker._execute(row)
        if (await catalog.get_kb(kb.id)).active_index_version == 2:
            break
    assert (await catalog.get_kb(kb.id)).active_index_version == 2


async def test_cleanup_activation_retry_republishes_without_repeating_cleanup(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, _first, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    second = b"# Second\n\nSurvives cleanup activation."
    second_hash = sha256(second).hexdigest()
    second_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{second_hash}"
    await LocalObjectStore(tmp_path).put(second_key, second, content_type="text/markdown")
    await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="second.md",
            content_hash=second_hash,
            size_bytes=len(second),
            mime_type="text/markdown",
            object_key=second_key,
        ),
    )

    class PublicationFacade:
        def __init__(self) -> None:
            self.inner = CatalogIngestionFacade()
            self.fail_next = False
            self.calls = 0

        async def publish_runtime(self, kb_id):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("simulated cleanup publication failure")
            await self.inner.publish_runtime(kb_id)

        def __getattr__(self, name: str):
            return getattr(self.inner, name)

    facade = PublicationFacade()
    pipeline, _provider, _objects, _vectors, *_ = await _test_pipeline(
        kb, tmp_path, catalog_facade=facade
    )
    await _execute_pipeline(pipeline)
    await _execute_pipeline(pipeline)
    assert facade.calls == 1

    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    fanout = TaskWorker("maintain", concurrency=1, poll_interval=0.01)
    fanout.register("kb.reindex_fanout", ReindexFanoutService(batch_size=100).handle)
    await _execute_one(fanout)
    await _execute_pipeline(pipeline)
    await _execute_pipeline(pipeline)
    async with session_scope() as session:
        deleted_id = await session.scalar(
            text(
                "SELECT document_id FROM document_ingestion "
                "WHERE kb_id=:kb_id AND index_version=2 ORDER BY document_id LIMIT 1"
            ),
            {"kb_id": kb.id},
        )
    assert deleted_id is not None
    await catalog.delete_document(pipeline_admin, deleted_id)
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE task SET run_after=clock_timestamp()+interval '1 day' "
                "WHERE document_id=:document_id AND kind='document.purge'"
            ),
            {"document_id": deleted_id},
        )
    await _execute_one(fanout)
    await _execute_one(fanout)
    assert (await catalog.get_kb(kb.id)).active_index_version == 1

    facade.fail_next = True
    cleanup = TaskWorker("index", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(cleanup, pipeline)
    await _execute_one(cleanup)
    assert (await catalog.get_kb(kb.id)).active_index_version == 2
    cached = await catalog.cache.get(f"kb:runtime:{kb.id}")
    assert cached is not None
    assert KnowledgeBaseRuntime.model_validate_json(cached).index_version == 1

    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE task SET run_after=now() WHERE document_id=:document_id "
                "AND kind='document.reindex_delete' AND state='ready'"
            ),
            {"document_id": deleted_id},
        )
    await _execute_one(cleanup)
    repaired = await catalog.cache.get(f"kb:runtime:{kb.id}")
    assert repaired is not None
    assert KnowledgeBaseRuntime.model_validate_json(repaired).index_version == 2
    assert facade.calls == 3


def _task_context(row: dict[str, object], worker_id: str) -> TaskContext:
    return TaskContext(
        task_id=int(row["id"]),
        queue=str(row["queue"]),
        kind=str(row["kind"]),
        workspace_id=row["workspace_id"],  # type: ignore[arg-type]
        kb_id=row["kb_id"],  # type: ignore[arg-type]
        document_id=row["document_id"],  # type: ignore[arg-type]
        payload=row["payload"],  # type: ignore[arg-type]
        attempt=int(row["attempt"]),
        max_attempts=int(row["max_attempts"]),
        correlation_id=row["correlation_id"],  # type: ignore[arg-type]
        worker_id=worker_id,
    )
