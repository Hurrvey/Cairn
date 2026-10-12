"""Catalog runtime projection and destructive-maintenance integration tests."""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64encode
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from apps.worker.main import build_worker
from cairn.authz.model import Principal
from cairn.catalog.dto import UploadSpec
from cairn.catalog.maintenance import (
    RuntimeRefresher,
    RuntimeRefreshScheduler,
    _postgres_refresh_leader,
)
from cairn.catalog.models import Document, KbIndexVersion
from cairn.catalog.service import CatalogService
from cairn.core.cache import InMemoryCache, get_cache
from cairn.core.db import session_scope, transaction
from cairn.core.errors import NotFound
from cairn.core.time import utcnow
from cairn.objectstore.keys import ObjectKeys
from cairn.objectstore.local import LocalObjectStore
from cairn.tasks.dto import TaskSpec
from cairn.tasks.service import TaskService
from cairn.vectorstore.base import Namespace, NamespaceSpec, Point

__all__ = ["pipeline_admin"]


class _PausedSetCache(InMemoryCache):
    def __init__(self) -> None:
        super().__init__()
        self.set_started = asyncio.Event()
        self.release_set = asyncio.Event()

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        self.set_started.set()
        await self.release_set.wait()
        await super().set(key, value, ttl)


def test_maintain_worker_registers_catalog_maintenance_handlers() -> None:
    worker = build_worker("maintain")
    assert {"document.purge", "kb.purge", "index.drop"} <= worker._handlers.keys()


async def test_concurrent_delete_linearizes_after_runtime_publication(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    cache = _PausedSetCache()
    service = CatalogService(cache=cache)

    publishing = asyncio.create_task(service.publish_runtime(kb.id))
    await asyncio.wait_for(cache.set_started.wait(), timeout=1)
    deleting = asyncio.create_task(service.delete_kb(pipeline_admin, kb.id))
    await asyncio.sleep(0.05)
    cache.release_set.set()
    await asyncio.gather(publishing, deleting)

    assert await cache.get(f"kb:runtime:{kb.id}") is None


async def test_scheduler_repairs_missing_active_runtime(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    cache_key = f"kb:runtime:{kb.id}"
    await get_cache().delete(cache_key)
    stop = asyncio.Event()
    refresher = RuntimeRefresher()

    async def refresh(batch_size: int) -> None:
        await refresher.refresh(batch_size)
        stop.set()

    scheduler = RuntimeRefreshScheduler(
        refresh=refresh,
        interval_s=60,
        batch_size=1,
        leader_lock=_postgres_refresh_leader,
    )
    await asyncio.wait_for(scheduler.run(stop), timeout=5)

    assert await get_cache().get(cache_key) is not None


async def test_document_purge_removes_vectors_objects_and_catalog_identity(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, _source, source_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, _provider, objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    document_id = registration.document.id
    compact_id = urlsafe_b64encode(document_id.bytes).rstrip(b"=").decode("ascii")
    parsed_key = f"{ObjectKeys.kb_prefix(kb.workspace_id, kb.id)}parsed/{document_id}/1/extra.json"
    orphan_key = f"{ObjectKeys.kb_prefix(kb.workspace_id, kb.id)}i/{compact_id}/1-1/orphan.json"
    await objects.put(parsed_key, b"parsed")
    await objects.put(orphan_key, b"orphan")
    missing_row_namespace = Namespace(kb.id, 2)
    await vectors.ensure_namespace(missing_row_namespace, NamespaceSpec(dim=3, metric="cosine"))
    await vectors.upsert(
        missing_row_namespace,
        [
            Point(
                id=document_id,
                dense=[1.0, 0.0, 0.0],
                payload={"document_id": str(document_id), "content": "orphan"},
            )
        ],
    )
    async with transaction() as session:
        await session.execute(
            text("UPDATE knowledge_base SET index_version_high_water=1000000000 WHERE id=:kb_id"),
            {"kb_id": kb.id},
        )

    await catalog.delete_document(pipeline_admin, document_id)
    await _execute_one(build_worker("maintain"))

    assert await vectors.count(Namespace(kb.id, 1)) == 0
    from cairn.retrieval.runtime import KnowledgeBaseRuntimeLoader

    runtime = await KnowledgeBaseRuntimeLoader().load_one(kb.id, kb.workspace_id)
    assert runtime.directory.doc_count == 0
    assert runtime.directory.chunk_count == 0
    assert await vectors.count(missing_row_namespace) == 0
    assert await objects.head(source_key) is None
    assert await objects.head(parsed_key) is None
    assert await objects.head(orphan_key) is None
    async with session_scope() as session:
        assert await session.get(Document, document_id) is None
    try:
        await catalog.get_document(document_id)
    except NotFound:
        pass
    else:
        raise AssertionError("purged document remains in the catalog")


async def test_document_purge_preserves_source_referenced_by_live_document(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, _source, source_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    second_source = b"different live source identity"
    await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="shared.md",
            content_hash=sha256(second_source).hexdigest(),
            size_bytes=len(second_source),
            mime_type="text/markdown",
            object_key=source_key,
        ),
    )

    await catalog.delete_document(pipeline_admin, registration.document.id)
    await _execute_one(build_worker("maintain"))

    assert await LocalObjectStore(tmp_path).head(source_key) is not None


async def test_kb_purge_removes_all_external_and_owned_sql_state(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, _registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, _provider, objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    extra_key = f"{ObjectKeys.kb_prefix(kb.workspace_id, kb.id)}icon/deleting.png"
    await objects.put(extra_key, b"icon")

    await catalog.delete_kb(pipeline_admin, kb.id)
    await _execute_one(build_worker("maintain"))

    assert not await vectors.namespace_exists(Namespace(kb.id, 1))
    assert (await objects.list(ObjectKeys.kb_prefix(kb.workspace_id, kb.id), limit=1)).items == ()
    async with session_scope() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM knowledge_base WHERE id=:kb_id"),
                {"kb_id": kb.id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM resource_grant "
                    "WHERE resource_type='knowledge_base' AND resource_id=:kb_id"
                ),
                {"kb_id": kb.id},
            )
            == 0
        )


async def test_index_drop_preserves_active_and_removes_failed_version(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, _registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, _provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    tasks = TaskService()
    async with transaction() as session:
        await tasks.enqueue(
            session,
            TaskSpec(
                queue="maintain",
                kind="index.drop",
                workspace_id=kb.workspace_id,
                kb_id=kb.id,
                payload={"index_version": 1},
                dedupe_key=f"index.drop:{kb.id}:1",
            ),
        )
    await _execute_one(build_worker("maintain"))
    assert await vectors.namespace_exists(Namespace(kb.id, 1))

    failed = Namespace(kb.id, 2)
    await vectors.ensure_namespace(failed, NamespaceSpec(dim=3, metric="cosine"))
    async with transaction() as session:
        session.add(
            KbIndexVersion(
                kb_id=kb.id,
                version=2,
                workspace_id=kb.workspace_id,
                state="failed",
                physical_ref=failed.key(),
                snapshot_unavailable=True,
            )
        )
        await tasks.enqueue(
            session,
            TaskSpec(
                queue="maintain",
                kind="index.drop",
                workspace_id=kb.workspace_id,
                kb_id=kb.id,
                payload={"index_version": 2},
                dedupe_key=f"index.drop:{kb.id}:2",
            ),
        )
    await _execute_one(build_worker("maintain"))

    assert not await vectors.namespace_exists(failed)
    async with session_scope() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM kb_index_version WHERE kb_id=:kb_id AND version=2"),
                {"kb_id": kb.id},
            )
            == 0
        )


async def test_retired_index_drops_only_after_retention(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, _registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, _provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    retired = Namespace(kb.id, 2)
    await vectors.ensure_namespace(retired, NamespaceSpec(dim=3, metric="cosine"))
    tasks = TaskService()
    async with transaction() as session:
        session.add(
            KbIndexVersion(
                kb_id=kb.id,
                version=2,
                workspace_id=kb.workspace_id,
                state="retired",
                physical_ref=retired.key(),
                snapshot_unavailable=True,
                retire_after=utcnow() + timedelta(hours=1),
            )
        )
        await tasks.enqueue(
            session,
            TaskSpec(
                queue="maintain",
                kind="index.drop",
                workspace_id=kb.workspace_id,
                kb_id=kb.id,
                payload={"index_version": 2},
                max_attempts=2,
                dedupe_key=f"index.drop:{kb.id}:2",
            ),
        )
    await _execute_one(build_worker("maintain"))
    assert await vectors.namespace_exists(retired)

    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE kb_index_version SET retire_after=now() - interval '1 second' "
                "WHERE kb_id=:kb_id AND version=2"
            ),
            {"kb_id": kb.id},
        )
        await session.execute(
            text("UPDATE task SET run_after=now() WHERE kind='index.drop' AND state='ready'")
        )
    await _execute_one(build_worker("maintain"))

    assert not await vectors.namespace_exists(retired)
