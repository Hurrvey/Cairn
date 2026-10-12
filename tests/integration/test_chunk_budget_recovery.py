"""Long default-config documents and recovery from pre-fix oversize artifacts."""

from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.core.db import session_scope, transaction
from cairn.core.errors import Conflict
from cairn.ingestion import pipeline as pipeline_module
from cairn.ingestion.chunkers import DocumentChunker
from cairn.ingestion.pipeline import register_pipeline_handlers
from cairn.tasks.worker import TaskWorker
from cairn.vectorstore.base import Namespace

__all__ = ["pipeline_admin"]
SOURCE = b"# Long document\n\n" + b"Saturn has rings. Titan is a moon. " * 80


async def test_default_chunks_respect_actual_model_limit(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(
        pipeline_admin, tmp_path, source=SOURCE
    )
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    for queue in ("parse", "chunk", "embed"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
    assert (await catalog.get_document(registration.document.id)).state == "embedded"
    worker = TaskWorker("index", concurrency=1)
    register_pipeline_handlers(worker, pipeline)
    await _execute_one(worker)
    assert (await catalog.get_document(registration.document.id)).state == "indexed"
    assert provider.calls and all(
        len(content) <= 128 for batch in provider.calls for content in batch
    )
    assert await vectors.count(Namespace(kb.id, 1)) > 1


@pytest.mark.parametrize("manual_edits", ["none", "before", "after"])
async def test_oversize_retry_rechunks_without_reparsing_or_losing_manual_edits(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual_edits: str
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(
        pipeline_admin, tmp_path, source=SOURCE
    )
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)

    def legacy_chunker(**kwargs):
        kwargs.pop("max_input_tokens", None)
        return DocumentChunker(**kwargs)

    with monkeypatch.context() as legacy:
        legacy.setattr(pipeline_module, "DocumentChunker", legacy_chunker)
        for queue in ("parse", "chunk", "embed"):
            worker = TaskWorker(queue, concurrency=1)
            register_pipeline_handlers(worker, pipeline)
            await _execute_one(worker)
    document = await catalog.get_document(registration.document.id)
    assert document.error_code == "EMBED_INPUT_TOO_LARGE"
    assert provider.calls == []
    async with session_scope() as session:
        parsed_key = await session.scalar(text("SELECT parsed_object_key FROM document_ingestion"))
    if manual_edits == "before":
        async with transaction() as session:
            await session.execute(text("UPDATE chunk SET is_edited=true WHERE ordinal=1"))
        with pytest.raises(Conflict, match="manual"):
            await catalog.retry_document(pipeline_admin, document.id)
        assert (await catalog.get_document(document.id)).state == "failed"
        return
    await catalog.retry_document(pipeline_admin, document.id)
    assert (await catalog.get_document(document.id)).state == "parsed"
    async with session_scope() as session:
        run = (
            await session.execute(
                text(
                    "SELECT parsed_object_key,chunks_object_key,embeddings_object_key,"
                    "recovery_generation "
                    "FROM document_ingestion"
                )
            )
        ).one()
    assert run == (parsed_key, None, None, 1)
    if manual_edits == "after":
        async with transaction() as session:
            await session.execute(text("UPDATE chunk SET is_edited=true WHERE ordinal=1"))
        worker = TaskWorker("chunk", concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
        assert (await catalog.get_document(document.id)).state == "failed"
        async with session_scope() as session:
            assert await session.scalar(text("SELECT count(*) FROM chunk WHERE is_edited")) == 1
        return
    for queue in ("chunk", "embed", "index"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
    assert (await catalog.get_document(document.id)).state == "indexed"
    assert await vectors.count(Namespace(kb.id, 1)) > 1
    async with session_scope() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM task WHERE kind='document.parse'")) == 1
        )
