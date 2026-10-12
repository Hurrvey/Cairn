"""A transient vector-store failure is retried; the last attempt fails the document."""

from __future__ import annotations

from hashlib import sha256
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import BindingRef, IndexVersionConfig
from cairn.catalog.ingestion import CatalogIngestionFacade, IngestionRun
from cairn.core.modelref import ModelRef
from cairn.ingestion.pipeline import IngestionPipeline, PipelineResources
from cairn.tasks.dto import TaskContext
from cairn.vectorstore.errors import VectorStoreTransient, VectorStoreUnavailable


def _run() -> IngestionRun:
    workspace_id, kb_id = uuid4(), uuid4()
    return IngestionRun(
        document_id=uuid4(),
        workspace_id=workspace_id,
        kb_id=kb_id,
        revision=1,
        index_version=1,
        state="embedded",
        source_content_hash=sha256(b"source").hexdigest(),
        object_key=f"{workspace_id}/{kb_id}/originals/source",
        mime_type="text/plain",
        source_url=None,
        version_config=IndexVersionConfig(
            embedding_model=ModelRef(
                id=uuid4(),
                provider_family="tei",
                model_key="test",
                capability="embedding",
                dimension=3,
            ),
            metric="cosine",
            chunk_config=ChunkConfig(),
        ),
        object_binding=BindingRef(id=uuid4(), kind="object", driver="local", name="local"),
        vector_binding=BindingRef(id=uuid4(), kind="vector", driver="qdrant", name="vectors"),
        prior_parsed_object_key=None,
        parsed_object_key="parsed",
        chunks_object_key="chunks",
        embeddings_object_key="embeddings",
        prior_embeddings_object_key=None,
        point_count=1,
        stale_point_ids=(),
        active_index_version=1,
        building_index_version=None,
    )


def _context(attempt: int, max_attempts: int = 5) -> TaskContext:
    return TaskContext(
        task_id=7,
        queue="index",
        kind="document.index",
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        payload={},
        attempt=attempt,
        max_attempts=max_attempts,
        correlation_id=None,
        worker_id="test",
    )


def _pipeline(error: Exception) -> tuple[IngestionPipeline, Mock]:
    catalog = Mock(spec=CatalogIngestionFacade)
    catalog.load_run = AsyncMock(return_value=_run())
    catalog.record_failure = AsyncMock()
    resources = Mock(spec=PipelineResources)
    resources.object_store_for = AsyncMock(return_value=Mock())
    resources.vector_store_for = AsyncMock(side_effect=error)
    return IngestionPipeline(catalog=catalog, resources=resources), catalog


@pytest.mark.parametrize(("attempt", "terminal"), [(1, False), (4, False), (5, True)])
async def test_a_transient_store_failure_retries_until_the_last_attempt(
    attempt: int, terminal: bool
) -> None:
    pipeline, catalog = _pipeline(VectorStoreTransient("Qdrant is not reachable."))

    result = await pipeline.handle_index(_context(attempt))

    assert result.ok is False
    assert result.retryable is True
    assert result.error_code == "STORAGE_BINDING_UNAVAILABLE"
    assert catalog.record_failure.await_args.kwargs["terminal"] is terminal


async def test_a_rejected_request_fails_the_document_at_once() -> None:
    pipeline, catalog = _pipeline(VectorStoreUnavailable("Qdrant rejected the request (HTTP 400)."))

    result = await pipeline.handle_index(_context(1))

    assert result.retryable is False
    assert catalog.record_failure.await_args.kwargs["terminal"] is True


async def test_manual_chunk_reembedding_retries_a_transient_store_failure() -> None:
    from cairn.ingestion.pipeline import _manual_reembed_failure

    transient = _manual_reembed_failure(VectorStoreTransient("Qdrant is not reachable."))
    rejected = _manual_reembed_failure(VectorStoreUnavailable("HTTP 403"))

    assert transient.retryable is True
    assert rejected.retryable is False
