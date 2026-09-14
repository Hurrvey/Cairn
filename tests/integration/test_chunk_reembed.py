"""Owned lifecycle slice-3 regressions for manual chunk re-embedding."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _execute_pipeline,
    _FailAfterVectorUpsert,
    _make_ready_now,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.catalog.dto import ReindexSpec
from cairn.catalog.errors import ChunkNotEditable
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.catalog.models import Chunk
from cairn.core.db import session_scope, transaction
from cairn.core.errors import NotFound
from cairn.core.time import utcnow
from cairn.ingestion.pipeline import register_pipeline_handlers
from cairn.tasks.dto import TaskContext
from cairn.tasks.worker import TaskWorker
from cairn.vectorstore.base import Namespace

__all__ = ["pipeline_admin"]


async def test_aba_edits_use_monotonic_generation_and_keep_physical_chunk_id(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    first = "Manual value A"
    second = "Manual value B"

    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, first)
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, second)
    edited = await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, first)

    assert edited.id == chunk.id
    async with session_scope() as session:
        tasks = (
            await session.execute(
                text(
                    "SELECT payload FROM task WHERE kb_id=:kb AND kind='chunk.reembed' ORDER BY id"
                ),
                {"kb": kb.id},
            )
        ).scalars()
        generation = await session.scalar(
            text("SELECT edit_generation FROM chunk WHERE kb_id=:kb AND id=:chunk"),
            {"kb": kb.id, "chunk": chunk.id},
        )

    payloads = list(tasks)
    assert [payload["edit_generation"] for payload in payloads] == [1, 2, 3]
    assert [payload["content_hash"] for payload in payloads] == [
        sha256(first.encode()).hexdigest(),
        sha256(second.encode()).hexdigest(),
        sha256(first.encode()).hexdigest(),
    ]
    assert all(payload["chunk_id"] == str(chunk.id) for payload in payloads)
    assert generation == 3


async def test_new_revision_generation_one_is_not_deduped_by_old_ready_task(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, source, object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "revision one edit")
    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET run_after=:later WHERE kb_id=:kb AND kind='chunk.reembed'"),
            {"later": utcnow() + timedelta(days=1), "kb": kb.id},
        )

    await CatalogIngestionFacade().start_revision(
        registration.document.id,
        object_key=object_key,
        content_hash=sha256(source).hexdigest(),
        size_bytes=len(source),
        mime_type="text/markdown",
    )
    for queue in ("parse", "chunk", "embed", "index"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)

    replacement = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    assert replacement.id == chunk.id
    await catalog.edit_chunk(pipeline_admin, kb.id, replacement.id, "revision two edit")

    async with session_scope() as session:
        payloads = list(
            (
                await session.execute(
                    text(
                        "SELECT payload FROM task WHERE kb_id=:kb AND kind='chunk.reembed' "
                        "ORDER BY id"
                    ),
                    {"kb": kb.id},
                )
            ).scalars()
        )
    assert [(payload["revision"], payload["edit_generation"]) for payload in payloads] == [
        (1, 1),
        (2, 1),
    ]


async def test_deleting_document_cannot_accept_chunk_edit(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    async with transaction() as session:
        await session.execute(
            text("UPDATE document SET state='deleting' WHERE id=:document"),
            {"document": registration.document.id},
        )

    with pytest.raises(NotFound):
        await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "must not persist")


async def test_successful_reembed_refreshes_chunk_and_document_token_counts_only(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    async with transaction() as session:
        document_tokens = await session.scalar(
            text("SELECT token_count FROM document WHERE id=:document"),
            {"document": registration.document.id},
        )
        session.add(
            Chunk(
                kb_id=kb.id,
                id=uuid4(),
                workspace_id=pipeline_admin.workspace_id,
                document_id=uuid4(),
                index_version=1,
                ordinal=999,
                content="unrelated",
                content_hash="f" * 64,
                token_count=100,
                chunk_metadata={"embed": False},
            )
        )
    replacement = "short edit"
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, replacement)
    worker = TaskWorker("embed", concurrency=1)
    register_pipeline_handlers(worker, pipeline)
    await _execute_one(worker)

    async with session_scope() as session:
        updated_chunk_tokens = await session.scalar(
            text("SELECT token_count FROM chunk WHERE kb_id=:kb AND id=:chunk"),
            {"kb": kb.id, "chunk": chunk.id},
        )
        updated_document_tokens = await session.scalar(
            text("SELECT token_count FROM document WHERE id=:document"),
            {"document": registration.document.id},
        )
    assert updated_chunk_tokens == len(replacement)
    assert updated_document_tokens == document_tokens - chunk.token_count + len(replacement)


async def test_retry_after_vector_write_verifies_payload_without_second_provider_call(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    failing_vectors = _FailAfterVectorUpsert(vectors)

    async def vector_store_for(_binding):  # type: ignore[no-untyped-def]
        return failing_vectors

    pipeline._resources = replace(pipeline._resources, vector_store_for=vector_store_for)
    provider.calls.clear()
    content = "durable retry after remote vector write"
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, content)
    worker = TaskWorker("embed", concurrency=1)
    register_pipeline_handlers(worker, pipeline)

    await _execute_one(worker)
    assert failing_vectors.failed is True
    assert sum(len(batch) for batch in provider.calls) == 1
    written = await vectors.fetch(Namespace(kb.id, 1), [chunk.id])
    assert written[0].content == content
    async with session_scope() as session:
        applied = await session.scalar(
            text("SELECT reembed_applied_generation FROM chunk WHERE kb_id=:kb AND id=:chunk"),
            {"kb": kb.id, "chunk": chunk.id},
        )
    assert applied == 0

    await _make_ready_now(registration.document.id)
    await _execute_one(worker)
    assert sum(len(batch) for batch in provider.calls) == 1
    async with session_scope() as session:
        applied = await session.scalar(
            text("SELECT reembed_applied_generation FROM chunk WHERE kb_id=:kb AND id=:chunk"),
            {"kb": kb.id, "chunk": chunk.id},
        )
    assert applied == 1


@pytest.mark.parametrize("stale_reason", ["source", "deleted", "retired", "workspace"])
async def test_stale_scope_and_version_tasks_skip_before_provider_or_vector_write(
    pipeline_admin: Principal, tmp_path: Path, stale_reason: str
) -> None:
    catalog, kb, registration, source, object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    namespace = Namespace(kb.id, 1)
    before = await vectors.fetch(namespace, [chunk.id])
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "must be fenced")
    if stale_reason == "source":
        await CatalogIngestionFacade().start_revision(
            registration.document.id,
            object_key=object_key,
            content_hash=sha256(source).hexdigest(),
            size_bytes=len(source),
            mime_type="text/markdown",
        )
    else:
        async with transaction() as session:
            if stale_reason == "deleted":
                await session.execute(
                    text(
                        "UPDATE document SET deleted_at=now(), state='deleting' WHERE id=:document"
                    ),
                    {"document": registration.document.id},
                )
            elif stale_reason == "retired":
                await session.execute(
                    text(
                        "UPDATE kb_index_version SET state='retired' WHERE kb_id=:kb AND version=1"
                    ),
                    {"kb": kb.id},
                )
    provider.calls.clear()
    if stale_reason == "workspace":
        async with session_scope() as session:
            payload = await session.scalar(
                text("SELECT payload FROM task WHERE kb_id=:kb AND kind='chunk.reembed'"),
                {"kb": kb.id},
            )
        result = await pipeline.handle_chunk_reembed(
            TaskContext(
                task_id=999,
                queue="embed",
                kind="chunk.reembed",
                workspace_id=uuid4(),
                kb_id=kb.id,
                document_id=registration.document.id,
                payload=payload,
                attempt=1,
                max_attempts=1,
                correlation_id=None,
                worker_id="wrong-workspace",
            )
        )
        assert result.ok is True
    else:
        worker = TaskWorker("embed", concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)

    assert provider.calls == []
    assert await vectors.fetch(namespace, [chunk.id]) == before


async def test_active_chunk_edit_is_refused_while_rebuild_exists(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))

    with pytest.raises(ChunkNotEditable, match="rebuilding"):
        await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "unsafe during rebuild")

    current = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.id == chunk.id
    )
    assert current.content == chunk.content


@pytest.mark.parametrize("run_state", ["chunked", "embedded"])
async def test_shared_manifest_states_refuse_manual_edit(
    pipeline_admin: Principal, tmp_path: Path, run_state: str
) -> None:
    catalog, kb, registration, source, object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    await CatalogIngestionFacade().start_revision(
        registration.document.id,
        object_key=object_key,
        content_hash=sha256(source).hexdigest(),
        size_bytes=len(source),
        mime_type="text/markdown",
    )
    for queue in ("parse", "chunk"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
    if run_state == "embedded":
        worker = TaskWorker("embed", concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )

    with pytest.raises(ChunkNotEditable, match="immutable index artifact"):
        await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "manifest race")


async def test_expired_reembed_lease_cannot_write_vector(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, _provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        item
        for item in await catalog.list_chunks(kb.id, registration.document.id)
        if item.metadata.get("embed", True)
    )
    namespace = Namespace(kb.id, 1)
    before = await vectors.fetch(namespace, [chunk.id])
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "expired lease edit")
    worker = TaskWorker("embed", concurrency=1)
    register_pipeline_handlers(worker, pipeline)
    rows = await worker._claim_batch()
    assert len(rows) == 1
    async with transaction() as session:
        await session.execute(
            text("UPDATE task SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"),
            {"id": rows[0]["id"]},
        )

    await worker._execute(rows[0])

    assert await vectors.fetch(namespace, [chunk.id]) == before
