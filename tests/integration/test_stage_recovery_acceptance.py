"""Coordinator acceptance for resuming committed ingestion stages."""

from __future__ import annotations

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
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.core.db import session_scope
from cairn.ingestion.pipeline import PipelineArtifactError, register_pipeline_handlers
from cairn.tasks.worker import TaskWorker
from cairn.vectorstore.base import Namespace

__all__ = ["pipeline_admin"]


@pytest.mark.parametrize("failed_stage", ["parse", "chunk", "embed", "index"])
async def test_retry_resumes_failed_stage_without_changing_source_revision(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    parse_calls: list[bytes] = []
    original_parse = pipeline._resources.parsers.parse

    async def counted_parse(data, **kwargs):
        parse_calls.append(data)
        return await original_parse(data, **kwargs)

    monkeypatch.setattr(pipeline._resources.parsers, "parse", counted_parse)
    owner, operation = {
        "parse": (pipeline._resources.parsers, "parse"),
        "chunk": (pipeline._catalog, "commit_chunked"),
        "embed": (pipeline._catalog, "commit_embedded"),
        "index": (vectors, "upsert"),
    }[failed_stage]
    original = getattr(owner, operation)
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise PipelineArtifactError("Injected terminal stage failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, operation, fail_once)
    queues = ("parse", "chunk", "embed", "index")
    for queue in queues:
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
        if queue == failed_stage:
            break
    before = await catalog.get_document(registration.document.id)
    assert before.state == "failed"
    await catalog.retry_document(pipeline_admin, before.id)
    retried = await catalog.get_document(before.id)
    assert retried.revision == before.revision
    async with session_scope() as session:
        tasks = (
            await session.execute(
                text("SELECT queue, payload FROM task WHERE document_id=:doc AND state='ready'"),
                {"doc": before.id},
            )
        ).all()
    assert len(tasks) == 1
    assert tasks[0].queue == failed_stage
    assert tasks[0].payload["revision"] == 1
    assert tasks[0].payload["index_version"] == 1
    for queue in queues[queues.index(failed_stage) :]:
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
    assert (await catalog.get_document(before.id)).state == "indexed"
    assert len(parse_calls) == 1
    assert sum(len(batch) for batch in provider.calls) == 1
    assert await vectors.count(Namespace(kb.id, 1)) > 0


async def test_repeated_failure_record_does_not_destroy_original_resumepoint(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, _kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    worker = TaskWorker("parse", concurrency=1)
    rows = await worker._claim_batch()
    assert len(rows) == 1
    context = worker._build_context(rows[0])
    facade = CatalogIngestionFacade()
    for _attempt in range(2):
        await facade.record_failure(
            context,
            stage="parse",
            error_code="INGESTION_ARTIFACT_INVALID",
            detail="Injected repeat terminal failure",
            terminal=True,
        )
    await catalog.retry_document(pipeline_admin, registration.document.id)
    current = await catalog.get_document(registration.document.id)
    assert current.revision == 1
    assert current.state == "registered"


async def test_publication_recovery_does_not_discard_pending_manual_edit(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    pipeline, _provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    original_publish = pipeline._catalog.publish_runtime
    edited_id = None
    content = "Manual edit accepted before runtime publication failed."

    async def edit_then_fail(kb_id):
        nonlocal edited_id
        if edited_id is None:
            chunks = await catalog.list_chunks(kb_id, registration.document.id)
            chunk = next(chunk for chunk in chunks if chunk.metadata.get("embed", True))
            edited_id = chunk.id
            await catalog.edit_chunk(pipeline_admin, kb_id, chunk.id, content)
            raise PipelineArtifactError("Injected terminal publication failure after edit")
        await original_publish(kb_id)

    monkeypatch.setattr(pipeline._catalog, "publish_runtime", edit_then_fail)
    workers = {}
    for queue in ("parse", "chunk", "embed", "index"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        workers[queue] = worker
        await _execute_one(worker)
    assert edited_id is not None
    assert (await catalog.get_document(registration.document.id)).state == "failed"
    await catalog.retry_document(pipeline_admin, registration.document.id)
    await _execute_one(workers["index"])
    await _execute_one(workers["embed"])
    result = await vectors.fetch(Namespace(kb.id, 1), [edited_id])
    assert len(result) == 1 and result[0].content == content
    assert (await catalog.get_document(registration.document.id)).revision == 1
