"""Opt-in acceptance against an actual TEI server, never a mocked model endpoint."""

from __future__ import annotations

import json
import math
import os
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import _execute_pipeline, pipeline_admin

from cairn.authz.model import Principal
from cairn.catalog.config import ChunkConfig, ChunkStrategy
from cairn.catalog.dto import CreateKbSpec, UploadSpec
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.catalog.service import CatalogService
from cairn.core.cache import get_cache
from cairn.core.db import get_engine, session_scope
from cairn.ingestion.artifacts import decode_embedding_manifest
from cairn.ingestion.runtime import PipelineRuntime
from cairn.ingestion.runtime_config import get_ingestion_runtime_settings
from cairn.modelgw.catalog import ModelCatalog
from cairn.modelgw.dto import RegisterModelSpec
from cairn.objectstore.local import LocalObjectStore
from cairn.vectorstore.base import Namespace, VectorQuery
from cairn.vectorstore.pgvector import PgVectorStore

__all__ = ["pipeline_admin"]

pytestmark = pytest.mark.skipif(
    not os.environ.get("CAIRN_TEST_TEI_URL") or not os.environ.get("CAIRN_TEST_TEI_TOKENIZER"),
    reason="live TEI acceptance requires explicit endpoint and pinned local tokenizer",
)


@pytest.mark.parametrize("strategy", ["markdown", "semantic"])
async def test_live_model_runtime_indexes_and_replaces_document(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy: ChunkStrategy,
) -> None:
    tokenizer = Path(os.environ["CAIRN_TEST_TEI_TOKENIZER"])
    monkeypatch.setenv(
        "CAIRN_INGESTION__TOKENIZERS_JSON", json.dumps({"live-minilm": f"hf:{tokenizer}"})
    )
    get_ingestion_runtime_settings.cache_clear()
    catalog = CatalogService()
    models = ModelCatalog()
    provider = await models.create_provider(
        pipeline_admin.workspace_id,
        name="live-tei",
        family="tei",
        base_url=os.environ["CAIRN_TEST_TEI_URL"],
        config={
            "binding_revision": "live-minilm-1110a243",
            "allow_private": True,
            "max_batch_size": 16,
        },
    )
    model = await models.register_model(
        pipeline_admin.workspace_id,
        RegisterModelSpec(
            provider_id=provider.id,
            model_key="sentence-transformers/all-MiniLM-L6-v2",
            display_name="Live MiniLM acceptance",
            capability="embedding",
            dimension=384,
            max_input_tokens=256,
            optimal_batch_size=16,
            tokenizer_id="live-minilm",
        ),
    )
    vectors = await catalog.create_binding(
        pipeline_admin, kind="vector", driver="pgvector", name="live-vectors", config={}
    )
    objects = await catalog.create_binding(
        pipeline_admin,
        kind="object",
        driver="local",
        name="live-objects",
        config={"path": str(tmp_path)},
    )
    kb = await catalog.create_kb(
        pipeline_admin,
        CreateKbSpec(
            name="Live model pipeline",
            embedding_model_id=model.id,
            vector_binding_id=vectors.id,
            object_binding_id=objects.id,
            chunk_config=ChunkConfig(
                strategy=strategy, child_tokens=64, child_overlap=0, min_chunk_tokens=8
            ),
        ),
    )
    source = (
        b"# Astronomy\n\nThe planet Saturn has prominent rings made of ice and rock. "
        b"Its moons orbit the gas giant. Marine scientists study currents in the ocean."
    )
    digest = sha256(source).hexdigest()
    key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{digest}"
    object_store = LocalObjectStore(tmp_path)
    await object_store.put(key, source, content_type="text/markdown")
    registration = await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="astronomy.md",
            content_hash=digest,
            size_bytes=len(source),
            mime_type="text/markdown",
            object_key=key,
        ),
    )
    assert registration.document is not None
    runtime = PipelineRuntime()
    try:
        await _execute_pipeline(runtime.pipeline)
        document = await catalog.get_document(registration.document.id)
        assert document.state == "indexed"
        assert (await catalog.get_kb(kb.id)).active_index_version == 1
        vector_store = PgVectorStore(get_engine())
        namespace = Namespace(kb.id, 1)
        prepared = await runtime.embedding_for(model.id)
        query = await prepared.service.embed_query(prepared.model, "Which planet has icy rings?")
        assert query.dim == 384 and math.isclose(math.hypot(*query.values), 1, abs_tol=1e-6)
        hits = await vector_store.search(namespace, VectorQuery(dense=query.values, top_k=3))
        assert hits and "Saturn" in hits[0].content
        old_ids = {hit.id for hit in hits}
        async with session_scope() as session:
            artifact_key = await session.scalar(
                text("SELECT embeddings_object_key FROM document_ingestion WHERE document_id=:id"),
                {"id": document.id},
            )
        assert isinstance(artifact_key, str)
        manifest = decode_embedding_manifest(
            await object_store.get_bytes(artifact_key, max_bytes=1024 * 1024)
        )
        assert manifest.dimension == 384 and manifest.vectors
        assert all(len(vector) == 384 for vector in manifest.vectors.values())
        await get_cache().delete(f"kb:runtime:{kb.id}")
        replacement = b"# Oceans\n\nThe Pacific Ocean is the largest ocean on Earth."
        replacement_hash = sha256(replacement).hexdigest()
        replacement_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{replacement_hash}"
        await object_store.put(replacement_key, replacement, content_type="text/markdown")
        await CatalogIngestionFacade().start_revision(
            document.id,
            object_key=replacement_key,
            content_hash=replacement_hash,
            size_bytes=len(replacement),
            mime_type="text/markdown",
        )
        await _execute_pipeline(runtime.pipeline)
        replaced = await catalog.get_document(document.id)
        assert replaced.revision == 2 and replaced.state == "indexed"
        assert await vector_store.fetch(namespace, list(old_ids)) == []
        ocean_query = await prepared.service.embed_query(prepared.model, "largest ocean on Earth")
        ocean_hits = await vector_store.search(
            namespace, VectorQuery(dense=ocean_query.values, top_k=3)
        )
        assert ocean_hits and "Pacific" in ocean_hits[0].content
        assert all("Saturn" not in hit.content for hit in ocean_hits)
    finally:
        await runtime.close()
        get_ingestion_runtime_settings.cache_clear()
