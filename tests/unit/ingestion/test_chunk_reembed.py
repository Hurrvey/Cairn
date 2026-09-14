from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from cairn.catalog.dto import BindingRef, ChunkReembedTarget
from cairn.catalog.ingestion import _chunk_reembed_identity
from cairn.core.modelref import ModelRef
from cairn.embedding.base import Vector
from cairn.ingestion.pipeline import (
    IngestionPipeline,
    PipelineResources,
    PreparedEmbedding,
    _chunk_reembed_point,
)
from cairn.tasks.dto import TaskContext
from cairn.vectorstore.base import Hit


class _Tokenizer:
    fingerprint = "manual-tokenizer"

    def __init__(self) -> None:
        self.counted: list[str] = []

    def count(self, text: str) -> int:
        self.counted.append(text)
        return len(text) + 2

    def truncate(self, text: str, limit: int) -> str:
        raise AssertionError("manual re-embedding must never truncate")


class _EmbeddingService:
    def __init__(self) -> None:
        self.calls: list[tuple[ModelRef, list[str]]] = []

    async def embed_documents(
        self, model: ModelRef, texts: list[str], *, batch_size: int | None = None
    ) -> list[Vector]:
        self.calls.append((model, texts))
        return [Vector((1.0, 2.0, 3.0), 3, False)]


class _Vectors:
    def __init__(self) -> None:
        self.upserts: list[Any] = []
        self.point: Hit | None = None

    async def fetch(self, _namespace: Any, _ids: Any) -> list[Hit]:
        return [self.point] if self.point is not None else []

    async def upsert(self, _namespace: Any, points: Any) -> Any:
        self.upserts.extend(points)
        point = points[0]
        self.point = Hit(id=point.id, score=1.0, payload=point.payload)
        return None


class _Mutation:
    def __init__(self) -> None:
        self.commits: list[int] = []

    async def __aenter__(self) -> _Mutation:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def apply(self, operation: Any) -> Any:
        return await operation()

    async def commit(self, actual_token_count: int) -> None:
        self.commits.append(actual_token_count)


class _Catalog:
    def __init__(self, target: ChunkReembedTarget | None) -> None:
        self.target = target
        self.mutation = _Mutation()

    async def load_chunk_reembed(self, _context: TaskContext) -> ChunkReembedTarget | None:
        return self.target

    def chunk_reembed_mutation(
        self, _context: TaskContext, _target: ChunkReembedTarget
    ) -> _Mutation:
        return self.mutation


def _target(*, content: str = "  \uff21   B  ", max_input_tokens: int = 16) -> ChunkReembedTarget:
    workspace_id = uuid4()
    model = ModelRef(
        id=uuid4(),
        provider_family="tei",
        model_key="manual",
        capability="embedding",
        dimension=3,
        max_input_tokens=max_input_tokens,
        normalize=False,
        tokenizer_id="manual-tokenizer",
    )
    return ChunkReembedTarget(
        workspace_id=workspace_id,
        kb_id=uuid4(),
        document_id=uuid4(),
        chunk_id=uuid4(),
        revision=7,
        index_version=3,
        edit_generation=2,
        reembed_applied_generation=0,
        content=content,
        content_hash="a" * 64,
        parent_id=uuid4(),
        metadata={"citations": [{"page": 4}], "embed": True},
        embedding_model=model,
        metric="cosine",
        vector_binding=BindingRef(
            id=uuid4(), kind="vector", driver="pgvector", name="manual", config={}
        ),
    )


def _context(target: ChunkReembedTarget, payload: dict[str, Any] | None = None) -> TaskContext:
    return TaskContext(
        task_id=1,
        queue="embed",
        kind="chunk.reembed",
        workspace_id=target.workspace_id,
        kb_id=target.kb_id,
        document_id=target.document_id,
        payload=payload
        or {
            "chunk_id": str(target.chunk_id),
            "revision": target.revision,
            "index_version": target.index_version,
            "content_hash": target.content_hash,
            "edit_generation": target.edit_generation,
        },
        attempt=1,
        max_attempts=5,
        correlation_id=None,
        worker_id="test",
    )


def _pipeline(
    target: ChunkReembedTarget | None,
    *,
    prepared_model: ModelRef | None = None,
) -> tuple[IngestionPipeline, _Catalog, _Tokenizer, _EmbeddingService, _Vectors]:
    catalog = _Catalog(target)
    tokenizer = _Tokenizer()
    embeddings = _EmbeddingService()
    vectors = _Vectors()
    model = prepared_model or (
        target.embedding_model if target is not None else _target().embedding_model
    )

    async def embedding_for(_model: ModelRef) -> PreparedEmbedding:
        return PreparedEmbedding(
            model=model,
            tokenizer=tokenizer,
            service=embeddings,  # type: ignore[arg-type]
            binding_fingerprint="manual-binding",
        )

    async def vector_store_for(_binding: BindingRef) -> Any:
        return vectors

    pipeline = IngestionPipeline(
        catalog=catalog,  # type: ignore[arg-type]
        resources=PipelineResources(
            parsers=None,  # type: ignore[arg-type]
            object_store_for=None,  # type: ignore[arg-type]
            vector_store_for=vector_store_for,
            embedding_for=embedding_for,
        ),
    )
    return pipeline, catalog, tokenizer, embeddings, vectors


@pytest.mark.anyio
async def test_reembed_counts_exact_normalized_input_before_provider() -> None:
    target = _target(max_input_tokens=5)
    pipeline, catalog, tokenizer, embeddings, vectors = _pipeline(target)

    result = await pipeline.handle_chunk_reembed(_context(target))

    assert result.ok is True
    assert tokenizer.counted[0] == "A B"
    assert embeddings.calls == [(target.embedding_model, [target.content])]
    assert len(vectors.upserts) == 1
    assert catalog.mutation.commits == [5]


@pytest.mark.anyio
async def test_reembed_rejects_over_budget_before_provider_or_vector_write() -> None:
    target = _target(max_input_tokens=4)
    pipeline, catalog, _tokenizer, embeddings, vectors = _pipeline(target)

    result = await pipeline.handle_chunk_reembed(_context(target))

    assert result.ok is False
    assert result.error_code == "EMBED_INPUT_TOO_LARGE"
    assert result.retryable is False
    assert embeddings.calls == []
    assert vectors.upserts == []
    assert catalog.mutation.commits == []


@pytest.mark.anyio
async def test_reembed_legacy_payload_fails_closed_before_catalog_or_provider() -> None:
    target = _target()
    context = _context(target, {"chunk_id": str(target.chunk_id), "index_version": 3})
    assert _chunk_reembed_identity(context) is None
    pipeline, catalog, _tokenizer, embeddings, vectors = _pipeline(None)

    result = await pipeline.handle_chunk_reembed(context)

    assert result.ok is True
    assert result.detail == "skipped: stale manual chunk edit"
    assert catalog.mutation.commits == []
    assert embeddings.calls == []
    assert vectors.upserts == []


@pytest.mark.anyio
async def test_reembed_rejects_prepared_model_drift_before_reusing_remote_payload() -> None:
    target = _target()
    drifted = replace(target.embedding_model, model_key="changed-registration")
    pipeline, catalog, _tokenizer, embeddings, vectors = _pipeline(target, prepared_model=drifted)
    vectors.point = Hit(
        id=target.chunk_id,
        score=1.0,
        payload=_chunk_reembed_point(target, (1.0, 2.0, 3.0)).payload,
    )

    result = await pipeline.handle_chunk_reembed(_context(target))

    assert result.ok is False
    assert result.error_code == "INGESTION_ARTIFACT_INVALID"
    assert catalog.mutation.commits == []
    assert embeddings.calls == []
    assert vectors.upserts == []


@pytest.mark.anyio
async def test_reembed_does_not_reuse_wrong_point_id_with_matching_payload() -> None:
    target = _target()
    pipeline, catalog, _tokenizer, embeddings, vectors = _pipeline(target)
    vectors.point = Hit(
        id=uuid4(),
        score=1.0,
        payload=_chunk_reembed_point(target, (1.0, 2.0, 3.0)).payload,
    )

    result = await pipeline.handle_chunk_reembed(_context(target))

    assert result.ok is True
    assert embeddings.calls == [(target.embedding_model, [target.content])]
    assert [point.id for point in vectors.upserts] == [target.chunk_id]
    assert catalog.mutation.commits == [5]


def test_chunk_reembed_point_keeps_exact_text_and_deep_metadata() -> None:
    target = _target()
    point = _chunk_reembed_point(target, (1.0, 2.0, 3.0))

    assert point.id == target.chunk_id
    assert point.payload["content"] == target.content
    assert point.payload["content_hash"] == target.content_hash
    assert point.payload["revision"] == target.revision
    assert point.payload["parent_id"] == str(target.parent_id)
    assert point.payload["manual_edit_generation"] == target.edit_generation
    assert point.payload["citations"] == [{"page": 4}]


def test_chunk_reembed_target_deep_copies_nested_metadata() -> None:
    metadata = {"citations": [{"page": 4}], "embed": True}
    target = replace(_target(), metadata=metadata)

    metadata["citations"][0]["page"] = 99

    assert target.metadata["citations"] == [{"page": 4}]
