"""Coordinator acceptance for manual chunk edits through the deployed worker factory."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
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
from cairn.core.db import session_scope
from cairn.vectorstore.base import Namespace

__all__ = ["pipeline_admin"]


def test_embed_factory_registers_manual_chunk_reembedding() -> None:
    assert "chunk.reembed" in build_worker("embed")._handlers


async def test_edit_updates_only_one_vector_and_preserves_citations(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(
        pipeline_admin, tmp_path, source=b"# One\n\nFirst paragraph.\n\n# Two\n\nSecond paragraph."
    )
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunks = await catalog.list_chunks(kb.id, registration.document.id)
    children = [chunk for chunk in chunks if chunk.metadata.get("embed", True)]
    assert len(children) == 2
    edited, other = children
    namespace = Namespace(kb.id, 1)
    other_before = await vectors.fetch(namespace, [other.id])
    provider.calls.clear()
    replacement = "  Corrected manual text.  "
    await catalog.edit_chunk(pipeline_admin, kb.id, edited.id, replacement)
    monkeypatch.setattr(
        "cairn.ingestion.runtime.get_pipeline_runtime", lambda: SimpleNamespace(pipeline=pipeline)
    )
    await _execute_one(build_worker("embed"))
    current = await vectors.fetch(namespace, [edited.id])
    assert len(current) == 1
    assert current[0].content == replacement
    assert current[0].payload["content_hash"] == sha256(replacement.encode()).hexdigest()
    for key, value in edited.metadata.items():
        assert current[0].payload[key] == value
    assert current[0].payload["parent_id"] == str(edited.parent_id)
    assert await vectors.fetch(namespace, [other.id]) == other_before
    assert await vectors.count(namespace) == 2
    assert sum(len(batch) for batch in provider.calls) == 1
    updated = await catalog.list_chunks(kb.id, registration.document.id)
    assert next(chunk for chunk in updated if chunk.id == edited.id).content == replacement


async def test_reembed_task_contains_expected_content_hash_and_source_revision(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        chunk
        for chunk in await catalog.list_chunks(kb.id, registration.document.id)
        if chunk.metadata.get("embed", True)
    )
    content = "Edited content with immutable task identity."
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, content)
    async with session_scope() as session:
        payload = await session.scalar(
            text("SELECT payload FROM task WHERE kb_id=:kb AND kind='chunk.reembed'"),
            {"kb": kb.id},
        )
    assert payload["content_hash"] == sha256(content.encode()).hexdigest()
    assert payload["revision"] == 1
    assert payload["index_version"] == 1


async def test_old_edit_is_skipped_before_embedding_and_latest_edit_wins(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        chunk
        for chunk in await catalog.list_chunks(kb.id, registration.document.id)
        if chunk.metadata.get("embed", True)
    )
    before = await vectors.fetch(Namespace(kb.id, 1), [chunk.id])
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "First obsolete edit.")
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "Second current edit.")
    provider.calls.clear()
    monkeypatch.setattr(
        "cairn.ingestion.runtime.get_pipeline_runtime", lambda: SimpleNamespace(pipeline=pipeline)
    )
    worker = build_worker("embed")
    worker.concurrency = 1
    await _execute_one(worker)
    assert provider.calls == []
    assert await vectors.fetch(Namespace(kb.id, 1), [chunk.id]) == before
    await _execute_one(worker)
    points = await vectors.fetch(Namespace(kb.id, 1), [chunk.id])
    assert points[0].content == "Second current edit."
    assert sum(len(batch) for batch in provider.calls) == 1


async def test_parent_edit_remains_nonvector_and_children_unchanged(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunks = await catalog.list_chunks(kb.id, registration.document.id)
    parent = next(chunk for chunk in chunks if chunk.metadata.get("embed") is False)
    children = [chunk for chunk in chunks if chunk.parent_id == parent.id]
    before = await vectors.fetch(Namespace(kb.id, 1), [chunk.id for chunk in children])
    provider.calls.clear()
    await catalog.edit_chunk(pipeline_admin, kb.id, parent.id, "Edited parent context only.")
    monkeypatch.setattr(
        "cairn.ingestion.runtime.get_pipeline_runtime", lambda: SimpleNamespace(pipeline=pipeline)
    )
    worker = build_worker("embed")
    for row in await worker._claim_batch():
        await worker._execute(row)
    assert provider.calls == []
    assert await vectors.fetch(Namespace(kb.id, 1), [parent.id]) == []
    assert await vectors.fetch(Namespace(kb.id, 1), [chunk.id for chunk in children]) == before
    updated = await catalog.list_chunks(kb.id, registration.document.id)
    assert (
        next(chunk for chunk in updated if chunk.id == parent.id).content
        == "Edited parent context only."
    )


async def test_oversized_edit_never_calls_embedding_or_overwrites_existing_vector(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        chunk
        for chunk in await catalog.list_chunks(kb.id, registration.document.id)
        if chunk.metadata.get("embed", True)
    )
    before = await vectors.fetch(Namespace(kb.id, 1), [chunk.id])
    provider.calls.clear()
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "x" * 129)
    monkeypatch.setattr(
        "cairn.ingestion.runtime.get_pipeline_runtime", lambda: SimpleNamespace(pipeline=pipeline)
    )
    await _execute_one(build_worker("embed"))
    assert provider.calls == []
    assert await vectors.fetch(Namespace(kb.id, 1), [chunk.id]) == before
    async with session_scope() as session:
        task = (
            await session.execute(
                text("SELECT state, error_code FROM task WHERE kb_id=:kb AND kind='chunk.reembed'"),
                {"kb": kb.id},
            )
        ).one()
    assert task.state == "failed"
    assert task.error_code == "EMBED_INPUT_TOO_LARGE"


async def test_new_edit_during_embedding_fences_old_vector_write(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    chunk = next(
        chunk
        for chunk in await catalog.list_chunks(kb.id, registration.document.id)
        if chunk.metadata.get("embed", True)
    )
    namespace = Namespace(kb.id, 1)
    before = await vectors.fetch(namespace, [chunk.id])
    await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "Edit that loses the embedding race.")
    original_embed = provider.embed
    raced = False

    async def racing_embed(model, texts, *, purpose):
        nonlocal raced
        if not raced:
            raced = True
            await catalog.edit_chunk(pipeline_admin, kb.id, chunk.id, "New edit wins the race.")
        return await original_embed(model, texts, purpose=purpose)

    monkeypatch.setattr(provider, "embed", racing_embed)
    monkeypatch.setattr(
        "cairn.ingestion.runtime.get_pipeline_runtime", lambda: SimpleNamespace(pipeline=pipeline)
    )
    worker = build_worker("embed")
    worker.concurrency = 1
    await asyncio.wait_for(_execute_one(worker), timeout=10)
    assert raced
    assert await vectors.fetch(namespace, [chunk.id]) == before
    await _execute_one(worker)
    latest = await vectors.fetch(namespace, [chunk.id])
    assert latest[0].content == "New edit wins the race."
