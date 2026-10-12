"""Durable failed-stage recovery fences and resumepoint tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.authz.service import ApiKeySpec, AuthzService
from cairn.catalog.dto import CreateKbSpec
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.core.db import session_scope, transaction
from cairn.core.errors import Conflict
from cairn.core.ids import encode_id
from cairn.ingestion.artifacts import decode_parse_manifest, encode_parse_manifest
from cairn.ingestion.pipeline import PipelineArtifactError, register_pipeline_handlers
from cairn.tasks.dto import TaskContext
from cairn.tasks.repository import TaskRepository
from cairn.tasks.worker import TaskWorker

__all__ = ["pipeline_admin"]


async def _claim_parse_context() -> TaskContext:
    repository = TaskRepository()
    worker_id = "stage-recovery-test-worker"
    async with transaction() as session:
        claimed = await repository.claim(
            session,
            queue="parse",
            batch=1,
            worker_id=worker_id,
            lease=timedelta(minutes=5),
        )
    assert len(claimed) == 1
    row = claimed[0]
    return TaskContext(
        task_id=row["id"],
        queue=row["queue"],
        kind=row["kind"],
        workspace_id=row["workspace_id"],
        kb_id=row["kb_id"],
        document_id=row["document_id"],
        payload=row["payload"],
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        correlation_id=row["correlation_id"],
        worker_id=worker_id,
    )


@pytest.mark.anyio
async def test_retry_persists_resumepoint_and_increments_generation_atomically(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, _kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    context = await _claim_parse_context()
    facade = CatalogIngestionFacade()
    await facade.record_failure(
        context,
        stage="parse",
        error_code="INGESTION_ARTIFACT_INVALID",
        detail="The ingestion artifact is invalid.",
        terminal=True,
    )

    await catalog.retry_document(pipeline_admin, registration.document.id)

    async with session_scope() as session:
        run = (
            await session.execute(
                text(
                    "SELECT state, failed_stage, previous_committed_state, recovery_generation "
                    "FROM document_ingestion WHERE document_id=:document_id"
                ),
                {"document_id": registration.document.id},
            )
        ).one()
        tasks = (
            await session.execute(
                text(
                    "SELECT queue, payload FROM task WHERE document_id=:document_id "
                    "AND state='ready'"
                ),
                {"document_id": registration.document.id},
            )
        ).all()

    assert run == ("registered", "parse", "registered", 1)
    assert tasks == [("parse", {"revision": 1, "index_version": 1, "recovery_generation": 1})]


@pytest.mark.anyio
async def test_old_generation_cannot_record_failure_after_retry(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, _kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    context = await _claim_parse_context()
    facade = CatalogIngestionFacade()
    await facade.record_failure(
        context,
        stage="parse",
        error_code="INGESTION_ARTIFACT_INVALID",
        detail="The ingestion artifact is invalid.",
        terminal=True,
    )
    await catalog.retry_document(pipeline_admin, registration.document.id)

    await facade.record_failure(
        context,
        stage="parse",
        error_code="STALE_TASK_MUST_NOT_WIN",
        detail="A stale task must not overwrite recovery.",
        terminal=True,
    )

    document = await catalog.get_document(registration.document.id)
    assert document.state == "registered"
    assert document.error_code is None
    async with session_scope() as session:
        run = (
            await session.execute(
                text(
                    "SELECT state, failed_stage, previous_committed_state, recovery_generation "
                    "FROM document_ingestion WHERE document_id=:document_id"
                ),
                {"document_id": registration.document.id},
            )
        ).one()
    assert run == ("registered", "parse", "registered", 1)


@pytest.mark.anyio
async def test_second_retry_cannot_enqueue_parallel_recovery(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, _kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    context = await _claim_parse_context()
    await CatalogIngestionFacade().record_failure(
        context,
        stage="parse",
        error_code="INGESTION_ARTIFACT_INVALID",
        detail="The ingestion artifact is invalid.",
        terminal=True,
    )

    outcomes = await asyncio.gather(
        catalog.retry_document(pipeline_admin, registration.document.id),
        catalog.retry_document(pipeline_admin, registration.document.id),
        return_exceptions=True,
    )
    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, Conflict) for outcome in outcomes) == 1

    async with session_scope() as session:
        recovery_tasks = await session.scalar(
            text(
                "SELECT count(*) FROM task WHERE document_id=:document_id "
                "AND payload->>'recovery_generation'='1'"
            ),
            {"document_id": registration.document.id},
        )
    assert recovery_tasks == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("artifact_damage", "expected_code"),
    [
        ("missing", "OBJECT_NOT_FOUND"),
        ("corrupt", "INGESTION_ARTIFACT_INVALID"),
        ("mismatched", "INGESTION_ARTIFACT_INVALID"),
    ],
)
async def test_invalid_committed_prerequisite_fails_without_provider_or_index_work(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_damage: str,
    expected_code: str,
) -> None:
    catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, provider, object_store, _vectors, *_ = await _test_pipeline(kb, tmp_path)
    parse_worker = TaskWorker("parse", concurrency=1)
    register_pipeline_handlers(parse_worker, pipeline)
    await _execute_one(parse_worker)
    commit_chunked = pipeline._catalog.commit_chunked

    async def fail_chunk_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PipelineArtifactError("Injected terminal chunk failure")

    monkeypatch.setattr(pipeline._catalog, "commit_chunked", fail_chunk_commit)
    chunk_worker = TaskWorker("chunk", concurrency=1)
    register_pipeline_handlers(chunk_worker, pipeline)
    await _execute_one(chunk_worker)
    monkeypatch.setattr(pipeline._catalog, "commit_chunked", commit_chunked)
    async with session_scope() as session:
        parsed_key = await session.scalar(
            text("SELECT parsed_object_key FROM document_ingestion WHERE document_id=:document_id"),
            {"document_id": registration.document.id},
        )
    assert isinstance(parsed_key, str)
    if artifact_damage == "missing":
        assert await object_store.delete(parsed_key)
    elif artifact_damage == "corrupt":
        await object_store.put(parsed_key, b"not-json", content_type="application/json")
    else:
        manifest = decode_parse_manifest(
            await object_store.get_bytes(parsed_key, max_bytes=1024 * 1024)
        )
        await object_store.put(
            parsed_key,
            encode_parse_manifest(replace(manifest, document_id=uuid4())),
            content_type="application/json",
        )

    await catalog.retry_document(pipeline_admin, registration.document.id)
    retry_worker = TaskWorker("chunk", concurrency=1)
    register_pipeline_handlers(retry_worker, pipeline)
    await _execute_one(retry_worker)

    failed = await catalog.get_document(registration.document.id)
    assert failed.state == "failed"
    assert failed.error_code == expected_code
    assert provider.calls == []
    async with session_scope() as session:
        downstream = await session.scalar(
            text(
                "SELECT count(*) FROM task WHERE document_id=:document_id "
                "AND kind IN ('document.embed','document.index')"
            ),
            {"document_id": registration.document.id},
        )
    assert downstream == 0


@pytest.mark.anyio
@pytest.mark.parametrize("unsafe_state", ["legacy", "failed", "retired"])
async def test_retry_fails_closed_without_known_stage_or_live_version(
    pipeline_admin: Principal,
    tmp_path: Path,
    unsafe_state: str,
) -> None:
    catalog, _kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    context = await _claim_parse_context()
    if unsafe_state == "legacy":
        async with transaction() as session:
            await session.execute(
                text("UPDATE document SET state='failed' WHERE id=:document_id"),
                {"document_id": registration.document.id},
            )
            await session.execute(
                text(
                    "UPDATE document_ingestion SET state='failed', failed_stage=NULL, "
                    "previous_committed_state=NULL WHERE document_id=:document_id"
                ),
                {"document_id": registration.document.id},
            )
    else:
        await CatalogIngestionFacade().record_failure(
            context,
            stage="parse",
            error_code="INGESTION_ARTIFACT_INVALID",
            detail="The ingestion artifact is invalid.",
            terminal=True,
        )
        async with transaction() as session:
            await session.execute(
                text("UPDATE kb_index_version SET state=:state"),
                {"state": unsafe_state},
            )

    with pytest.raises(Conflict):
        await catalog.retry_document(pipeline_admin, registration.document.id)

    assert (await catalog.get_document(registration.document.id)).state == "failed"
    async with session_scope() as session:
        recovery_tasks = await session.scalar(
            text(
                "SELECT count(*) FROM task WHERE document_id=:document_id "
                "AND payload->'recovery_generation' IS NOT NULL"
            ),
            {"document_id": registration.document.id},
        )
    assert recovery_tasks == 0


@pytest.mark.anyio
async def test_retry_path_kb_must_match_document_kb(
    pipeline_admin: Principal,
    tmp_path: Path,
    client: httpx.AsyncClient,
) -> None:
    catalog, document_kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    path_kb = await catalog.create_kb(
        pipeline_admin,
        CreateKbSpec(
            name="Different path knowledge base",
            embedding_model_id=document_kb.embedding_model_id,
            vector_binding_id=document_kb.vector_binding_id,
            object_binding_id=document_kb.object_binding_id,
        ),
    )
    context = await _claim_parse_context()
    await CatalogIngestionFacade().record_failure(
        context,
        stage="parse",
        error_code="INGESTION_ARTIFACT_INVALID",
        detail="The ingestion artifact is invalid.",
        terminal=True,
    )
    refreshed = await AuthzService().principal_for_user(pipeline_admin.id)
    assert refreshed is not None
    _key, raw_key = await AuthzService().create_api_key(
        refreshed,
        ApiKeySpec(name="path-kb-only", scopes=["kb:write"], kb_ids=[path_kb.id]),
    )

    response = await client.post(
        f"/v1/knowledge-bases/{encode_id('kb', path_kb.id)}/documents/"
        f"{encode_id('doc', registration.document.id)}/retry",
        headers={"Authorization": f"Bearer {raw_key}"},
    )

    assert response.status_code == 404
    assert (await catalog.get_document(registration.document.id)).state == "failed"


@pytest.mark.anyio
async def test_post_commit_failure_replaces_stale_ready_successor(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    commit_parsed = pipeline._catalog.commit_parsed

    async def commit_then_fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        await commit_parsed(*args, **kwargs)
        raise PipelineArtifactError("Injected failure after committed parse")

    monkeypatch.setattr(pipeline._catalog, "commit_parsed", commit_then_fail)
    worker = TaskWorker("parse", concurrency=1)
    register_pipeline_handlers(worker, pipeline)
    await _execute_one(worker)

    await catalog.retry_document(pipeline_admin, registration.document.id)

    async with session_scope() as session:
        successors = (
            await session.execute(
                text(
                    "SELECT state, queue, payload FROM task WHERE document_id=:document_id "
                    "AND kind='document.chunk' ORDER BY id"
                ),
                {"document_id": registration.document.id},
            )
        ).all()
    assert successors == [
        ("cancelled", "chunk", {"revision": 1, "index_version": 1}),
        (
            "ready",
            "chunk",
            {"revision": 1, "index_version": 1, "recovery_generation": 1},
        ),
    ]


@pytest.mark.anyio
async def test_old_generation_cannot_commit_stage_after_retry(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, _kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    context = await _claim_parse_context()
    facade = CatalogIngestionFacade()
    await facade.record_failure(
        context,
        stage="parse",
        error_code="INGESTION_ARTIFACT_INVALID",
        detail="The ingestion artifact is invalid.",
        terminal=True,
    )
    await catalog.retry_document(pipeline_admin, registration.document.id)

    assert not await facade.commit_parsed(
        context,
        parsed_object_key="must-not-commit/parsed.json",
        page_count=999,
    )

    async with session_scope() as session:
        document = (
            await session.execute(
                text("SELECT state, page_count FROM document WHERE id=:document_id"),
                {"document_id": registration.document.id},
            )
        ).one()
        chunk_tasks = await session.scalar(
            text(
                "SELECT count(*) FROM task WHERE document_id=:document_id AND kind='document.chunk'"
            ),
            {"document_id": registration.document.id},
        )
    assert document == ("registered", None)
    assert chunk_tasks == 0


@pytest.mark.anyio
async def test_publication_failure_retry_does_not_embed_or_reindex(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    publish = pipeline._catalog.publish_runtime
    publish_calls = 0

    async def fail_publish_once(kb_id):  # type: ignore[no-untyped-def]
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise PipelineArtifactError("Injected terminal publication failure")
        await publish(kb_id)

    monkeypatch.setattr(pipeline._catalog, "publish_runtime", fail_publish_once)
    upsert = vectors.upsert
    upsert_calls = 0

    async def counted_upsert(namespace, points):  # type: ignore[no-untyped-def]
        nonlocal upsert_calls
        upsert_calls += 1
        return await upsert(namespace, points)

    monkeypatch.setattr(vectors, "upsert", counted_upsert)
    for queue in ("parse", "chunk", "embed", "index"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
    assert (await catalog.get_document(registration.document.id)).state == "failed"
    provider_calls = len(provider.calls)
    index_upserts = upsert_calls

    await catalog.retry_document(pipeline_admin, registration.document.id)
    worker = TaskWorker("index", concurrency=1)
    register_pipeline_handlers(worker, pipeline)
    await _execute_one(worker)

    recovered = await catalog.get_document(registration.document.id)
    assert recovered.state == "indexed"
    assert recovered.error_code is None
    assert len(provider.calls) == provider_calls
    assert upsert_calls == index_upserts
    assert publish_calls == 2
