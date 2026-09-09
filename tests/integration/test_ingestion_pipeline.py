"""Real task DB, Redis, EmbeddingService, and pgvector ingestion integration tests."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from cairn.authz.model import Principal
from cairn.authz.service import AuthzService
from cairn.catalog.dto import CreateKbSpec, UploadSpec
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.catalog.service import CatalogService
from cairn.core.cache import get_cache
from cairn.core.db import get_engine, session_scope, transaction
from cairn.core.errors import ValidationFailed
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import TokenizerRegistry
from cairn.identity.service import IdentityService
from cairn.ingestion.pipeline import (
    IngestionPipeline,
    PipelineResources,
    PreparedEmbedding,
    register_pipeline_handlers,
)
from cairn.ingestion.registry import get_parser_registry
from cairn.modelgw.catalog import ModelCatalog
from cairn.modelgw.dto import RegisterModelSpec
from cairn.objectstore.keys import ObjectKeys
from cairn.objectstore.local import LocalObjectStore
from cairn.tasks.dto import TaskContext
from cairn.tasks.repository import TaskRepository
from cairn.tasks.service import TaskLeaseLostError
from cairn.tasks.worker import TaskWorker
from cairn.vectorstore.base import Namespace
from cairn.vectorstore.pgvector import PgVectorStore

STRONG_PASSWORD = "Correct-Horse-Battery-9"


class _PipelineTokenizer:
    fingerprint = "pipeline-test-tokenizer-v1"

    def count(self, text: str) -> int:
        return len(text)

    def truncate(self, text: str, limit: int) -> str:
        return text[:limit]


class _DeterministicEmbeddingProvider:
    cache_namespace = "pipeline-deterministic-provider-v1"
    max_batch_size = 16

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, model, texts, *, purpose):  # type: ignore[no-untyped-def]
        self.calls.append(list(texts))
        return [[float(len(text)), float(sum(map(ord, text)) % 97), 1.0] for text in texts]


class _FailAfterArtifactPut:
    def __init__(self, inner: LocalObjectStore, suffix: str) -> None:
        self._inner = inner
        self._suffix = suffix
        self.failed = False

    async def put(self, key, data, **kwargs):  # type: ignore[no-untyped-def]
        result = await self._inner.put(key, data, **kwargs)
        if key.endswith(self._suffix) and not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash after artifact write")
        return result

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)


class _FailAfterVectorUpsert:
    def __init__(self, inner: PgVectorStore) -> None:
        self._inner = inner
        self.failed = False

    async def upsert(self, namespace, points):  # type: ignore[no-untyped-def]
        result = await self._inner.upsert(namespace, points)
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash after vector upsert")
        return result

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)


class _FailFirstRuntimePublish:
    def __init__(self, inner: CatalogIngestionFacade) -> None:
        self._inner = inner
        self.calls = 0

    async def publish_runtime(self, kb_id):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated cache publication failure with sensitive detail")
        await self._inner.publish_runtime(kb_id)

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)


@pytest.fixture
async def pipeline_admin(identity: IdentityService, monkeypatch: pytest.MonkeyPatch) -> Principal:
    monkeypatch.delenv("CAIRN_INITIAL_ADMIN_PASSWORD", raising=False)
    result = await identity.bootstrap()
    assert result is not None and result.password is not None
    login = await identity.login("admin", result.password)
    assert login.change_token is not None
    await identity.complete_initial_setup(
        login.change_token,
        current_password=result.password,
        new_password=STRONG_PASSWORD,
    )
    principal = await AuthzService().principal_for_user(result.user_id)
    assert principal is not None
    return principal


async def _create_pipeline_document(
    pipeline_admin: Principal,
    tmp_path: Path,
    *,
    source: bytes = b"# Pipeline\n\nA paragraph.",
    registered_hash: str | None = None,
) -> tuple[CatalogService, Any, Any, bytes, str]:
    catalog = CatalogService()
    models = ModelCatalog()
    provider = await models.create_provider(
        pipeline_admin.workspace_id,
        name="pipeline-test-provider",
        family="tei",
        base_url="http://127.0.0.1:9",
        config={"binding_revision": "test-v1"},
    )
    model = await models.register_model(
        pipeline_admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id,
            model_key="deterministic",
            display_name="Deterministic Test Embedding",
            capability="embedding",
            dimension=3,
            max_input_tokens=128,
            normalize=False,
            tokenizer_id="pipeline-test-tokenizer",
        ),
    )
    vector = await catalog.create_binding(
        pipeline_admin, kind="vector", driver="pgvector", name="pipeline-vector", config={}
    )
    objects = await catalog.create_binding(
        pipeline_admin,
        kind="object",
        driver="local",
        name="pipeline-objects",
        config={"path": str(tmp_path)},
    )
    kb = await catalog.create_kb(
        pipeline_admin,
        CreateKbSpec(
            name="Pipeline",
            embedding_model_id=model.id,
            vector_binding_id=vector.id,
            object_binding_id=objects.id,
        ),
    )
    digest = registered_hash or sha256(source).hexdigest()
    object_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{digest}"
    store = LocalObjectStore(tmp_path)
    await store.put(object_key, source, content_type="text/markdown")
    registration = await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="pipeline.md",
            content_hash=digest,
            size_bytes=len(source),
            mime_type="text/markdown",
            object_key=object_key,
        ),
    )
    assert registration.document is not None
    return catalog, kb, registration, source, object_key


@pytest.mark.anyio
async def test_upload_allocates_revision_scoped_pipeline_run_atomically(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    digest = sha256(source).hexdigest()

    async with session_scope() as session:
        run = (
            (
                await session.execute(
                    text(
                        "SELECT revision, index_version, source_content_hash, state "
                        "FROM document_ingestion WHERE document_id=:document_id"
                    ),
                    {"document_id": registration.document.id},
                )
            )
            .mappings()
            .one()
        )
        task = (
            await session.execute(
                text(
                    "SELECT payload FROM task WHERE document_id=:document_id "
                    "AND kind='document.parse'"
                ),
                {"document_id": registration.document.id},
            )
        ).scalar_one()
        kb_row = (
            await session.execute(
                text(
                    "SELECT active_index_version, building_index_version "
                    "FROM knowledge_base WHERE id=:kb_id"
                ),
                {"kb_id": kb.id},
            )
        ).one()

    assert dict(run) == {
        "revision": 1,
        "index_version": 1,
        "source_content_hash": digest,
        "state": "registered",
    }
    assert task == {"revision": 1, "index_version": 1}
    assert kb_row.active_index_version is None
    assert kb_row.building_index_version == 1


@pytest.mark.anyio
async def test_commit_parsed_is_fenced_and_enqueues_chunk_atomically_once(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    repository = TaskRepository()
    worker_id = "pipeline-test-worker"
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
    context = TaskContext(
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
    facade = CatalogIngestionFacade()

    run = await facade.load_run(context)
    assert run is not None
    assert await facade.commit_parsed(
        context,
        parsed_object_key=f"{run.artifact_prefix}/parsed.json",
        page_count=1,
    )
    assert not await facade.commit_parsed(
        context,
        parsed_object_key=f"{run.artifact_prefix}/parsed.json",
        page_count=1,
    )

    async with session_scope() as session:
        document_state = await session.scalar(
            text("SELECT state FROM document WHERE id=:id"), {"id": registration.document.id}
        )
        chunk_tasks = await session.scalar(
            text("SELECT count(*) FROM task WHERE document_id=:id AND kind='document.chunk'"),
            {"id": registration.document.id},
        )
        payload = await session.scalar(
            text("SELECT payload FROM task WHERE document_id=:id AND kind='document.chunk'"),
            {"id": registration.document.id},
        )

    assert document_state == "parsed"
    assert chunk_tasks == 1
    assert payload == {"revision": 1, "index_version": 1}
    assert run.kb_id == kb.id


@pytest.mark.anyio
async def test_expired_task_lease_cannot_commit_catalog_stage(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, _kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    repository = TaskRepository()
    worker_id = "expired-pipeline-worker"
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
    context = TaskContext(
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
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE task SET lease_until=clock_timestamp() - interval '1 second' WHERE id=:id"
            ),
            {"id": context.task_id},
        )

    with pytest.raises(TaskLeaseLostError):
        await CatalogIngestionFacade().commit_parsed(
            context,
            parsed_object_key="must-not-commit/parsed.json",
            page_count=1,
        )

    async with session_scope() as session:
        document_state = await session.scalar(
            text("SELECT state FROM document WHERE id=:id"),
            {"id": registration.document.id},
        )
        chunk_tasks = await session.scalar(
            text("SELECT count(*) FROM task WHERE document_id=:id AND kind='document.chunk'"),
            {"id": registration.document.id},
        )
    assert document_state == "registered"
    assert chunk_tasks == 0


async def _execute_one(worker: TaskWorker) -> None:
    claimed = await worker._claim_batch()
    assert len(claimed) == 1
    await worker._execute(claimed[0])


async def _execute_pipeline(pipeline: IngestionPipeline) -> None:
    for queue in ("parse", "chunk", "embed", "index"):
        worker = TaskWorker(queue, concurrency=1, poll_interval=0.01)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)


async def _test_pipeline(  # type: ignore[no-untyped-def]
    kb,
    tmp_path: Path,
    *,
    artifact_failure_suffix: str | None = None,
    fail_after_upsert: bool = False,
    catalog_facade=None,
):
    model = await ModelCatalog().get_ref(kb.embedding_model_id)
    tokenizers = TokenizerRegistry()
    tokenizer = _PipelineTokenizer()
    tokenizers.register("pipeline-test-tokenizer", tokenizer)
    provider = _DeterministicEmbeddingProvider()
    embedding = EmbeddingService(provider=provider, cache=get_cache(), tokenizers=tokenizers)
    object_store = LocalObjectStore(tmp_path)
    vector_store = PgVectorStore(get_engine())
    resolved_object_store = (
        _FailAfterArtifactPut(object_store, artifact_failure_suffix)
        if artifact_failure_suffix is not None
        else object_store
    )
    resolved_vector_store = (
        _FailAfterVectorUpsert(vector_store) if fail_after_upsert else vector_store
    )

    async def object_store_for(_binding):  # type: ignore[no-untyped-def]
        return resolved_object_store

    async def vector_store_for(_binding):  # type: ignore[no-untyped-def]
        return resolved_vector_store

    async def embedding_for(_model_id):  # type: ignore[no-untyped-def]
        return PreparedEmbedding(
            model=model,
            tokenizer=tokenizer,
            service=embedding,
            binding_fingerprint="pipeline-binding-v1",
        )

    return (
        IngestionPipeline(
            catalog=catalog_facade or CatalogIngestionFacade(),
            resources=PipelineResources(
                parsers=get_parser_registry(),
                object_store_for=object_store_for,
                vector_store_for=vector_store_for,
                embedding_for=embedding_for,
            ),
        ),
        provider,
        object_store,
        vector_store,
        resolved_object_store,
        resolved_vector_store,
    )


@pytest.mark.anyio
async def test_upload_runs_through_actual_workers_to_verified_published_index(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin,
        tmp_path,
        source=b"# First\n\nUnchanged paragraph.\n\n# Second\n\nOriginal paragraph.",
    )
    assert registration.document is not None
    (
        pipeline,
        provider,
        _object_store,
        vector_store,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)

    document = await CatalogService().get_document(registration.document.id)
    assert document.state == "indexed"
    refreshed_kb = await CatalogService().get_kb(kb.id)
    assert refreshed_kb.active_index_version == 1
    assert refreshed_kb.building_index_version is None
    assert provider.calls and len(provider.calls) == 1
    assert await vector_store.count(Namespace(kb.id, 1)) == 2
    async with session_scope() as session:
        chunk_counts = (
            await session.execute(
                text(
                    "SELECT count(*) AS total, "
                    "count(*) FILTER (WHERE metadata->>'role'='parent' "
                    "AND metadata->>'embed'='false') AS unembedded_parents "
                    "FROM chunk WHERE document_id=:document_id AND index_version=1"
                ),
                {"document_id": registration.document.id},
            )
        ).one()
    assert chunk_counts.total == 4
    assert chunk_counts.unembedded_parents == 2


@pytest.mark.anyio
async def test_build_index_activates_only_after_every_registered_document_is_indexed(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, first, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert first.document is not None
    second_source = b"# Second document\n\nIndependent content."
    second_hash = sha256(second_source).hexdigest()
    second_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{second_hash}"
    await LocalObjectStore(tmp_path).put(second_key, second_source, content_type="text/markdown")
    second = await catalog.register_upload(
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
    assert second.document is not None
    (
        pipeline,
        _provider,
        _objects,
        vectors,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)

    await _execute_pipeline(pipeline)

    first_pass_kb = await CatalogService().get_kb(kb.id)
    assert first_pass_kb.active_index_version is None
    assert first_pass_kb.building_index_version == 1
    states = {
        (await CatalogService().get_document(first.document.id)).state,
        (await CatalogService().get_document(second.document.id)).state,
    }
    assert states == {"registered", "indexed"}

    await _execute_pipeline(pipeline)

    activated_kb = await CatalogService().get_kb(kb.id)
    assert activated_kb.active_index_version == 1
    assert activated_kb.building_index_version is None
    assert await vectors.count(Namespace(kb.id, 1)) == 2


@pytest.mark.anyio
async def test_deleted_current_document_does_not_leave_build_aggregation_pending(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, first, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert first.document is not None
    deleted_source = b"# Deleted before processing"
    deleted_hash = sha256(deleted_source).hexdigest()
    deleted_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{deleted_hash}"
    await LocalObjectStore(tmp_path).put(deleted_key, deleted_source, content_type="text/markdown")
    deleted = await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="deleted.md",
            content_hash=deleted_hash,
            size_bytes=len(deleted_source),
            mime_type="text/markdown",
            object_key=deleted_key,
        ),
    )
    assert deleted.document is not None
    await catalog.delete_document(pipeline_admin, deleted.document.id)
    pipeline, *_rest = await _test_pipeline(kb, tmp_path)

    await _execute_pipeline(pipeline)

    activated = await catalog.get_kb(kb.id)
    assert activated.active_index_version == 1
    assert activated.building_index_version is None


@pytest.mark.anyio
async def test_every_stage_handler_is_idempotent_under_double_execution(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    (
        pipeline,
        provider,
        _objects,
        vectors,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    stages = (
        ("parse", "document.parse", pipeline.handle_parse),
        ("chunk", "document.chunk", pipeline.handle_chunk),
        ("embed", "document.embed", pipeline.handle_embed),
        ("index", "document.index", pipeline.handle_index),
    )
    for queue, kind, handler in stages:
        worker = TaskWorker(queue, concurrency=1, poll_interval=0.01)

        async def execute_twice(context, stage_handler=handler):  # type: ignore[no-untyped-def]
            first = await stage_handler(context)
            second = await stage_handler(context)
            assert first.ok
            assert second.ok
            assert second.detail is not None and second.detail.startswith("skipped:")
            return second

        worker.register(kind, execute_twice)
        await _execute_one(worker)

    document = await CatalogService().get_document(registration.document.id)
    assert document.state == "indexed"
    assert len(provider.calls) == 1
    assert await vectors.count(Namespace(kb.id, 1)) == 1
    async with session_scope() as session:
        task_counts = (
            await session.execute(
                text(
                    "SELECT kind, count(*) FROM task WHERE document_id=:document_id "
                    "GROUP BY kind ORDER BY kind"
                ),
                {"document_id": registration.document.id},
            )
        ).all()
    assert task_counts == [
        ("document.chunk", 1),
        ("document.embed", 1),
        ("document.index", 1),
        ("document.parse", 1),
    ]


@pytest.mark.anyio
async def test_incremental_reprocessing_reuses_persisted_vectors_and_deletes_stale_points(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin,
        tmp_path,
        source=b"# First\n\nUnchanged paragraph.\n\n# Second\n\nOriginal paragraph.",
    )
    assert registration.document is not None
    facade = CatalogIngestionFacade()
    (
        pipeline,
        provider,
        object_store,
        vector_store,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    initial_calls = len(provider.calls)
    async with session_scope() as session:
        initial_rows = (
            await session.execute(
                text(
                    "SELECT id, content FROM chunk WHERE document_id=:document_id "
                    "AND index_version=1 AND metadata->>'embed'='true'"
                ),
                {"document_id": registration.document.id},
            )
        ).all()
        stale_id = next(row.id for row in initial_rows if "Original paragraph" in row.content)

    redis = Redis.from_url(os.environ["CAIRN_REDIS_URL"])
    await redis.flushdb()
    await redis.aclose()
    unchanged = b"# First\n\nUnchanged paragraph.\n\n# Second\n\nOriginal paragraph."
    unchanged_hash = sha256(unchanged).hexdigest()
    unchanged_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{unchanged_hash}"
    await object_store.put(unchanged_key, unchanged, content_type="text/markdown")
    await facade.start_revision(
        registration.document.id,
        object_key=unchanged_key,
        content_hash=unchanged_hash,
        size_bytes=len(unchanged),
        mime_type="text/markdown",
    )
    await _execute_pipeline(pipeline)
    assert len(provider.calls) == initial_calls

    changed = b"# First\n\nUnchanged paragraph.\n\n# Second\n\nChanged paragraph."
    changed_hash = sha256(changed).hexdigest()
    changed_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{changed_hash}"
    await object_store.put(changed_key, changed, content_type="text/markdown")
    await facade.start_revision(
        registration.document.id,
        object_key=changed_key,
        content_hash=changed_hash,
        size_bytes=len(changed),
        mime_type="text/markdown",
    )
    await _execute_pipeline(pipeline)

    assert len(provider.calls) == initial_calls + 1
    assert await vector_store.count(Namespace(kb.id, 1)) == 2
    assert await vector_store.fetch(Namespace(kb.id, 1), [stale_id]) == []


@pytest.mark.anyio
async def test_reprocessing_preserves_manual_edit_in_artifact_catalog_and_vector(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, source, object_key = await _create_pipeline_document(
        pipeline_admin,
        tmp_path,
        source=b"# First\n\nOriginal first.\n\n# Second\n\nOriginal second.",
    )
    assert registration.document is not None
    (
        pipeline,
        provider,
        _objects,
        vectors,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    initial_calls = len(provider.calls)
    corrected = "# First\n\nCorrected manual text."
    corrected_hash = sha256(corrected.encode()).hexdigest()
    async with transaction() as session:
        edited = (
            await session.execute(
                text(
                    "UPDATE chunk SET content=:content, content_hash=:content_hash, "
                    "is_edited=true WHERE id=(SELECT id FROM chunk "
                    "WHERE document_id=:document_id AND metadata->>'embed'='true' "
                    "ORDER BY ordinal LIMIT 1) RETURNING id, ordinal"
                ),
                {
                    "content": corrected,
                    "content_hash": corrected_hash,
                    "document_id": registration.document.id,
                },
            )
        ).one()

    await CatalogIngestionFacade().start_revision(
        registration.document.id,
        object_key=object_key,
        content_hash=sha256(source).hexdigest(),
        size_bytes=len(source),
        mime_type="text/markdown",
    )
    await _execute_pipeline(pipeline)

    assert len(provider.calls) == initial_calls + 1
    assert provider.calls[-1] == [" ".join(corrected.split())]
    async with session_scope() as session:
        catalog_chunk = (
            await session.execute(
                text(
                    "SELECT content, content_hash, is_edited FROM chunk "
                    "WHERE document_id=:document_id AND ordinal=:ordinal AND index_version=1"
                ),
                {"document_id": registration.document.id, "ordinal": edited.ordinal},
            )
        ).one()
    assert catalog_chunk.content == corrected
    assert catalog_chunk.content_hash == corrected_hash
    assert catalog_chunk.is_edited is True
    vector = (await vectors.fetch(Namespace(kb.id, 1), [edited.id]))[0]
    assert vector.payload["content"] == corrected
    assert vector.payload["content_hash"] == corrected_hash


@pytest.mark.anyio
async def test_stale_revision_cannot_commit_or_enqueue_next_stage(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    (
        pipeline,
        _provider,
        object_store,
        _vector_store,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    worker = TaskWorker("parse", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(worker, pipeline)
    claimed = await worker._claim_batch()
    assert len(claimed) == 1

    replacement = b"# Pipeline\n\nReplacement."
    replacement_hash = sha256(replacement).hexdigest()
    replacement_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{replacement_hash}"
    await object_store.put(replacement_key, replacement, content_type="text/markdown")
    await CatalogIngestionFacade().start_revision(
        registration.document.id,
        object_key=replacement_key,
        content_hash=replacement_hash,
        size_bytes=len(replacement),
        mime_type="text/markdown",
    )

    await worker._execute(claimed[0])

    document = await CatalogService().get_document(registration.document.id)
    assert document.revision == 2
    assert document.state == "registered"
    async with session_scope() as session:
        old_chunk_tasks = await session.scalar(
            text(
                "SELECT count(*) FROM task WHERE document_id=:document_id "
                "AND kind='document.chunk' AND payload->>'revision'='1'"
            ),
            {"document_id": registration.document.id},
        )
    assert old_chunk_tasks == 0

    await _execute_pipeline(pipeline)
    completed = await CatalogService().get_document(registration.document.id)
    activated = await CatalogService().get_kb(kb.id)
    assert completed.state == "indexed"
    assert activated.active_index_version == 1
    assert activated.building_index_version is None


@pytest.mark.anyio
async def test_parse_recomputes_source_hash_and_sanitizes_terminal_failure(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path, registered_hash="0" * 64
    )
    assert registration.document is not None
    (
        pipeline,
        _provider,
        _object_store,
        _vector_store,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    worker = TaskWorker("parse", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(worker, pipeline)

    await _execute_one(worker)

    document = await CatalogService().get_document(registration.document.id)
    assert document.state == "failed"
    assert document.error_code == "SOURCE_INTEGRITY_MISMATCH"
    assert document.error_detail == "The stored source does not match the registered document."
    assert sha256(source).hexdigest() not in document.error_detail
    async with session_scope() as session:
        task = (
            await session.execute(
                text(
                    "SELECT state, error_code, error_detail FROM task "
                    "WHERE document_id=:document_id AND kind='document.parse'"
                ),
                {"document_id": registration.document.id},
            )
        ).one()
    assert task.state == "failed"
    assert task.error_code == "SOURCE_INTEGRITY_MISMATCH"
    assert task.error_detail == document.error_detail


@pytest.mark.anyio
@pytest.mark.parametrize("traversal", [False, True], ids=["wrong-kb", "backslash-traversal"])
async def test_parse_rejects_source_key_outside_task_kb_scope(
    pipeline_admin: Principal, tmp_path: Path, traversal: bool
) -> None:
    _catalog, kb, registration, source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    digest = sha256(source).hexdigest()
    forged_key = f"{pipeline_admin.workspace_id}/another-kb/originals/{digest}"
    if traversal:
        forged_key = f"{pipeline_admin.workspace_id}/{kb.id}/..\\another-kb/originals/{digest}"
    await LocalObjectStore(tmp_path).put(forged_key, source, content_type="text/markdown")
    async with transaction() as session:
        await session.execute(
            text("UPDATE document SET object_key=:object_key WHERE id=:document_id"),
            {"document_id": registration.document.id, "object_key": forged_key},
        )
    (
        pipeline,
        _provider,
        _objects,
        _vectors,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    resolver_calls = 0

    async def forbidden_object_store_resolver(_binding):  # type: ignore[no-untyped-def]
        nonlocal resolver_calls
        resolver_calls += 1
        raise AssertionError("out-of-scope source reached object-store resolution")

    pipeline._resources = replace(
        pipeline._resources, object_store_for=forbidden_object_store_resolver
    )
    worker = TaskWorker("parse", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(worker, pipeline)

    await _execute_one(worker)

    document = await CatalogService().get_document(registration.document.id)
    assert document.state == "failed"
    assert document.error_code == "SOURCE_INTEGRITY_MISMATCH"
    assert document.error_detail == "The stored source does not match the registered document."
    assert forged_key not in document.error_detail
    assert resolver_calls == 0


@pytest.mark.anyio
async def test_registration_and_revision_replacement_enforce_same_source_scope(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, _source, original_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    digest = sha256(b"outside").hexdigest()
    outside_key = f"{pipeline_admin.workspace_id}/{kb.id}/..\\outside/{digest}"

    with pytest.raises(ValidationFailed):
        await catalog.register_upload(
            pipeline_admin,
            kb.id,
            UploadSpec(
                filename="outside.md",
                content_hash=digest,
                size_bytes=7,
                mime_type="text/markdown",
                object_key=outside_key,
            ),
        )
    with pytest.raises(ValidationFailed):
        await CatalogIngestionFacade().start_revision(
            registration.document.id,
            object_key=outside_key,
            content_hash=digest,
            size_bytes=7,
            mime_type="text/markdown",
        )

    unchanged = await catalog.get_document(registration.document.id)
    assert unchanged.revision == 1
    async with session_scope() as session:
        stored_key = await session.scalar(
            text("SELECT object_key FROM document WHERE id=:document_id"),
            {"document_id": registration.document.id},
        )
    assert stored_key == original_key


async def _make_ready_now(document_id) -> None:  # type: ignore[no-untyped-def]
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE task SET run_after=now() WHERE document_id=:document_id AND state='ready'"
            ),
            {"document_id": document_id},
        )


@pytest.mark.parametrize(
    ("queue", "suffix", "artifact_column", "committed_state"),
    [
        ("parse", "parsed.json", "parsed_object_key", "registered"),
        ("chunk", "chunks.json", "chunks_object_key", "parsed"),
        ("embed", "embeddings.json", "embeddings_object_key", "chunked"),
    ],
)
@pytest.mark.anyio
async def test_artifact_write_crash_retries_from_uncommitted_stage(
    pipeline_admin: Principal,
    tmp_path: Path,
    queue: str,
    suffix: str,
    artifact_column: str,
    committed_state: str,
) -> None:
    _catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    (
        pipeline,
        _provider,
        object_store,
        _vector_store,
        failing_store,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path, artifact_failure_suffix=suffix)
    queues = ["parse", "chunk", "embed", "index"]
    failed_index = queues.index(queue)
    for current in queues[: failed_index + 1]:
        worker = TaskWorker(current, concurrency=1, poll_interval=0.01)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)

    document = await CatalogService().get_document(registration.document.id)
    assert document.state == committed_state
    assert failing_store.failed
    run_context_worker = TaskWorker(queue, concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(run_context_worker, pipeline)
    await _make_ready_now(registration.document.id)
    await _execute_one(run_context_worker)
    for current in queues[failed_index + 1 :]:
        worker = TaskWorker(current, concurrency=1, poll_interval=0.01)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)

    recovered = await CatalogService().get_document(registration.document.id)
    assert recovered.state == "indexed"
    artifacts = [item for item in (await object_store.list("")).items if item.key.endswith(suffix)]
    assert len(artifacts) == 2
    assert len({item.key for item in artifacts}) == 2
    assert all(len(str(tmp_path.joinpath(*item.key.split("/")))) < 260 for item in artifacts)
    kb_prefix = ObjectKeys.kb_prefix(pipeline_admin.workspace_id, kb.id)
    assert all(item.key.startswith(kb_prefix) for item in artifacts)
    async with session_scope() as session:
        committed_key = await session.scalar(
            text(
                f"SELECT {artifact_column} FROM document_ingestion "  # noqa: S608 - fixed parametrization
                "WHERE document_id=:document_id AND revision=1 AND index_version=1"
            ),
            {"document_id": registration.document.id},
        )
    assert committed_key in {item.key for item in artifacts}
    assert await object_store.delete_prefix(kb_prefix) >= len(artifacts)
    assert (await object_store.list(kb_prefix)).items == ()


@pytest.mark.anyio
async def test_vector_upsert_crash_is_visible_and_retry_verifies_before_publish(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    (
        pipeline,
        _provider,
        _object_store,
        vector_store,
        _resolved_objects,
        failing_vectors,
    ) = await _test_pipeline(kb, tmp_path, fail_after_upsert=True)
    for queue in ("parse", "chunk", "embed", "index"):
        worker = TaskWorker(queue, concurrency=1, poll_interval=0.01)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)

    document = await CatalogService().get_document(registration.document.id)
    assert document.state == "embedded"
    assert failing_vectors.failed
    assert await vector_store.count(Namespace(kb.id, 1)) == 1
    kb_after_failure = await CatalogService().get_kb(kb.id)
    assert kb_after_failure.active_index_version is None
    await _make_ready_now(registration.document.id)
    worker = TaskWorker("index", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(worker, pipeline)
    await _execute_one(worker)

    recovered = await CatalogService().get_document(registration.document.id)
    assert recovered.state == "indexed"
    refreshed_kb = await CatalogService().get_kb(kb.id)
    assert refreshed_kb.active_index_version == 1


@pytest.mark.anyio
async def test_runtime_publish_crash_republishes_after_catalog_activation(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _object_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    failing_catalog = _FailFirstRuntimePublish(CatalogIngestionFacade())
    (
        pipeline,
        _provider,
        _objects,
        _vectors,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path, catalog_facade=failing_catalog)
    await _execute_pipeline(pipeline)

    failed_publish_document = await CatalogService().get_document(registration.document.id)
    assert failed_publish_document.state == "indexed"
    assert failed_publish_document.error_code == "INGESTION_STAGE_ERROR"
    assert failed_publish_document.error_detail == "The ingestion stage could not be completed."
    assert "sensitive" not in failed_publish_document.error_detail
    active_kb = await CatalogService().get_kb(kb.id)
    assert active_kb.active_index_version == 1
    assert failing_catalog.calls == 1

    await _make_ready_now(registration.document.id)
    index_worker = TaskWorker("index", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(index_worker, pipeline)
    await _execute_one(index_worker)

    recovered = await CatalogService().get_document(registration.document.id)
    assert recovered.state == "indexed"
    assert recovered.error_code is None
    assert recovered.error_detail is None
    assert failing_catalog.calls == 2
    async with session_scope() as session:
        task_state = await session.scalar(
            text("SELECT state FROM task WHERE document_id=:document_id AND kind='document.index'"),
            {"document_id": registration.document.id},
        )
    assert task_state == "done"


async def test_expired_index_attempt_cannot_mutate_vector_store(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    """NFR-R-05/06: a stale lease must fence side effects, not only the final SQL commit."""
    _catalog, kb, registration, _source, _key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    (
        pipeline,
        _provider,
        _objects,
        vectors,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    for queue in ("parse", "chunk", "embed"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)

    writes: list[int] = []

    class RecordingVectors:
        async def upsert(self, namespace, points):  # type: ignore[no-untyped-def]
            writes.append(len(points))
            return await vectors.upsert(namespace, points)

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(vectors, name)

    async def expire_lease_before_resolving_vectors(_binding):  # type: ignore[no-untyped-def]
        async with transaction() as session:
            await session.execute(
                text(
                    "UPDATE task SET lease_until=clock_timestamp()-interval '1 second' "
                    "WHERE document_id=:document_id AND kind='document.index'"
                ),
                {"document_id": registration.document.id},
            )
        return RecordingVectors()

    pipeline._resources = replace(
        pipeline._resources, vector_store_for=expire_lease_before_resolving_vectors
    )
    worker = TaskWorker("index", concurrency=1)
    register_pipeline_handlers(worker, pipeline)
    await _execute_one(worker)
    assert writes == []
    assert (await CatalogService().get_document(registration.document.id)).state == "embedded"


@pytest.mark.anyio
async def test_index_mutation_guard_does_not_block_independent_heartbeat(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, *_rest = await _test_pipeline(kb, tmp_path)
    for queue in ("parse", "chunk", "embed"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)

    index_worker = TaskWorker("index", concurrency=1)
    claimed = await index_worker._claim_batch()
    assert len(claimed) == 1
    context = index_worker._build_context(claimed[0])

    async with CatalogIngestionFacade().index_mutation(context):
        await asyncio.wait_for(context.heartbeat(), timeout=0.5)


@pytest.mark.anyio
async def test_lease_lost_between_external_calls_stops_upsert_delete_and_commit(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, _provider, _objects, vectors, *_rest = await _test_pipeline(kb, tmp_path)
    for queue in ("parse", "chunk", "embed"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)

    calls: list[str] = []

    class ExpireAfterEnsure:
        async def ensure_namespace(self, namespace, spec):  # type: ignore[no-untyped-def]
            calls.append("ensure")
            await vectors.ensure_namespace(namespace, spec)
            async with transaction() as session:
                await session.execute(
                    text(
                        "UPDATE task SET lease_until=clock_timestamp()-interval '1 second' "
                        "WHERE document_id=:document_id AND kind='document.index'"
                    ),
                    {"document_id": registration.document.id},
                )

        async def upsert(self, namespace, points):  # type: ignore[no-untyped-def]
            calls.append("upsert")
            return await vectors.upsert(namespace, points)

        async def delete(self, namespace, **kwargs):  # type: ignore[no-untyped-def]
            calls.append("delete")
            return await vectors.delete(namespace, **kwargs)

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(vectors, name)

    async def expiring_vectors(_binding):  # type: ignore[no-untyped-def]
        return ExpireAfterEnsure()

    pipeline._resources = replace(pipeline._resources, vector_store_for=expiring_vectors)
    worker = TaskWorker("index", concurrency=1)
    register_pipeline_handlers(worker, pipeline)
    await _execute_one(worker)

    assert calls == ["ensure"]
    assert (await CatalogService().get_document(registration.document.id)).state == "embedded"
    current_kb = await CatalogService().get_kb(kb.id)
    assert current_kb.active_index_version is None
    assert current_kb.building_index_version == 1


@pytest.mark.anyio
async def test_concurrent_revision_waits_until_index_guard_releases_catalog_locks(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, _source, _key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, _provider, object_store, *_rest = await _test_pipeline(kb, tmp_path)
    for queue in ("parse", "chunk", "embed"):
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
    index_worker = TaskWorker("index", concurrency=1)
    claimed = await index_worker._claim_batch()
    assert len(claimed) == 1
    context = index_worker._build_context(claimed[0])
    replacement = b"# Replacement while guarded"
    digest = sha256(replacement).hexdigest()
    object_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{digest}"
    await object_store.put(object_key, replacement, content_type="text/markdown")
    facade = CatalogIngestionFacade()

    async with facade.index_mutation(context):
        replacement_task = asyncio.create_task(
            facade.start_revision(
                registration.document.id,
                object_key=object_key,
                content_hash=digest,
                size_bytes=len(replacement),
                mime_type="text/markdown",
            )
        )
        await asyncio.sleep(0.1)
        assert not replacement_task.done()

    assert await asyncio.wait_for(replacement_task, timeout=1) == 2
