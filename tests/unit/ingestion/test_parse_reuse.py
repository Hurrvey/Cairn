"""Prior parse candidates use real local storage without database services."""

from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import BindingRef, IndexVersionConfig
from cairn.catalog.ingestion import CatalogIngestionFacade, IngestionRun
from cairn.core.modelref import ModelRef
from cairn.ingestion.pipeline import IngestionPipeline, PipelineResources
from cairn.objectstore.errors import ObjectStoreUnavailable
from cairn.objectstore.local import LocalObjectStore
from cairn.tasks.dto import TaskContext


@pytest.fixture
def run() -> IngestionRun:
    workspace_id = uuid4()
    kb_id = uuid4()
    return IngestionRun(
        document_id=uuid4(),
        workspace_id=workspace_id,
        kb_id=kb_id,
        revision=1,
        index_version=2,
        state="registered",
        source_content_hash=sha256(b"source").hexdigest(),
        object_key=f"{workspace_id}/{kb_id}/originals/source",
        mime_type="text/plain",
        source_url=None,
        version_config=IndexVersionConfig(
            embedding_model=ModelRef(
                id=uuid4(),
                provider_family="test",
                model_key="test",
                capability="embedding",
                dimension=3,
            ),
            metric="cosine",
            chunk_config=ChunkConfig(),
        ),
        object_binding=BindingRef(id=uuid4(), kind="object", driver="local", name="local"),
        vector_binding=BindingRef(id=uuid4(), kind="vector", driver="pgvector", name="vectors"),
        prior_parsed_object_key=f"{workspace_id}/{kb_id}/i/prior/parsed.json",
        parsed_object_key=None,
        chunks_object_key=None,
        embeddings_object_key=None,
        prior_embeddings_object_key=None,
        point_count=0,
        stale_point_ids=(),
        active_index_version=1,
        building_index_version=2,
    )


def pipeline_for(store: LocalObjectStore, run: IngestionRun) -> IngestionPipeline:
    catalog = Mock(spec=CatalogIngestionFacade)
    catalog.load_run = AsyncMock(return_value=run)
    catalog.record_failure = AsyncMock()
    resources = Mock(spec=PipelineResources)
    resources.max_artifact_bytes = 64
    resources.max_source_bytes = 32
    resources.object_store_for = AsyncMock(return_value=store)
    resources.parsers = Mock()
    resources.parsers.parse = AsyncMock()
    return IngestionPipeline(catalog=catalog, resources=resources)


@pytest.mark.parametrize("candidate", ["missing", "oversized", "corrupt"])
async def test_unusable_local_candidate_returns_source_fallback(
    tmp_path: Path, run: IngestionRun, candidate: str
) -> None:
    store = LocalObjectStore(tmp_path)
    assert run.prior_parsed_object_key is not None
    if candidate != "missing":
        await store.put(
            run.prior_parsed_object_key,
            b"x" * 65 if candidate == "oversized" else b"invalid JSON",
        )

    assert await pipeline_for(store, run)._reuse_parsed(run, store) is None


async def test_storage_unavailability_propagates_from_reuse(
    tmp_path: Path, run: IngestionRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalObjectStore(tmp_path)
    unavailable = ObjectStoreUnavailable("storage unavailable")
    monkeypatch.setattr(store, "head", AsyncMock(side_effect=unavailable))

    with pytest.raises(ObjectStoreUnavailable) as caught:
        await pipeline_for(store, run)._reuse_parsed(run, store)

    assert caught.value is unavailable


@pytest.mark.parametrize("candidate", ["missing", "oversized"])
async def test_source_size_cap_remains_enforced_after_candidate_fallback(
    tmp_path: Path, run: IngestionRun, candidate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalObjectStore(tmp_path)
    assert run.prior_parsed_object_key is not None
    if candidate == "oversized":
        await store.put(run.prior_parsed_object_key, b"x" * 65)
    await store.put(run.object_key, b"x" * 33)
    pipeline = pipeline_for(store, run)
    monkeypatch.setattr(pipeline, "_prepared_embedding", AsyncMock())
    reads = AsyncMock(wraps=store.get_bytes)
    monkeypatch.setattr(store, "get_bytes", reads)
    context = TaskContext(
        task_id=1,
        queue="parse",
        kind="document.parse",
        workspace_id=run.workspace_id,
        kb_id=run.kb_id,
        document_id=run.document_id,
        payload={"revision": 1, "index_version": 2},
        attempt=1,
        max_attempts=3,
        correlation_id=None,
        worker_id="unit-test",
    )

    result = await pipeline.handle_parse(context)

    reads.assert_any_await(run.object_key, max_bytes=32)
    assert not result.ok
    assert result.error_code == "OBJECT_TOO_LARGE"
    pipeline._resources.parsers.parse.assert_not_awaited()
