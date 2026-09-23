"""Opt-in acceptance against an actual TEI server, never a mocked model endpoint."""

from __future__ import annotations

import json
import math
import os
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _execute_one,
    _execute_pipeline,
    pipeline_admin,
)

from apps.api.main import create_app
from apps.worker.main import build_worker
from cairn.authz.model import Principal
from cairn.authz.service import ApiKeySpec, AuthzService
from cairn.catalog.config import ChunkConfig, ChunkStrategy
from cairn.catalog.dto import CreateKbSpec, ReindexSpec, UploadSpec
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.catalog.maintenance import RuntimeRefresher
from cairn.catalog.service import CatalogService
from cairn.core.cache import get_cache
from cairn.core.config import get_settings
from cairn.core.db import get_engine, session_scope
from cairn.core.ids import encode_id
from cairn.ingestion.artifacts import decode_embedding_manifest
from cairn.ingestion.pipeline import PipelineArtifactError
from cairn.ingestion.runtime import PipelineRuntime
from cairn.ingestion.runtime_config import get_ingestion_runtime_settings
from cairn.modelgw.catalog import ModelCatalog
from cairn.modelgw.dto import RegisterModelSpec
from cairn.objectstore.local import LocalObjectStore
from cairn.vectorstore.base import Namespace, VectorQuery
from cairn.vectorstore.pgvector import PgVectorStore
from mcp import ClientSession

__all__ = ["pipeline_admin"]

pytestmark = pytest.mark.skipif(
    not os.environ.get("CAIRN_TEST_TEI_URL") or not os.environ.get("CAIRN_TEST_TEI_TOKENIZER"),
    reason="live TEI acceptance requires explicit endpoint and pinned local tokenizer",
)


@pytest.mark.parametrize(
    "strategy,source_format",
    [
        ("markdown", "markdown"),
        ("semantic", "markdown"),
        ("parent_child", "markdown"),
        ("markdown", "pdf"),
    ],
)
async def test_live_model_runtime_indexes_and_replaces_document(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    strategy: ChunkStrategy,
    source_format: str,
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
            chunk_config=(
                None
                if strategy == "parent_child"
                else ChunkConfig(
                    strategy=strategy, child_tokens=64, child_overlap=0, min_chunk_tokens=8
                )
            ),
        ),
    )
    source = (
        b"# Astronomy\n\nThe planet Saturn has prominent rings made of ice and rock. "
        b"Its moons orbit the gas giant. Marine scientists study currents in the ocean."
    )
    if strategy == "parent_child":
        source += b" Saturn has rings of ice and rock and many moons." * 100
    if source_format == "pdf":
        from tests.unit.ingestion.test_pdf import _text_pdf

        source = _text_pdf("The planet Saturn has prominent rings made of ice and rock.")
    mime_type = "application/pdf" if source_format == "pdf" else "text/markdown"
    digest = sha256(source).hexdigest()
    key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{digest}"
    object_store = LocalObjectStore(tmp_path)
    await object_store.put(key, source, content_type=mime_type)
    registration = await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="astronomy.pdf" if source_format == "pdf" else "astronomy.md",
            content_hash=digest,
            size_bytes=len(source),
            mime_type=mime_type,
            object_key=key,
        ),
    )
    assert registration.document is not None
    runtime = PipelineRuntime()
    try:
        original_upsert = PgVectorStore.upsert
        failed_once = False

        async def fail_first_index(store, namespace, points):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise PipelineArtifactError("Injected terminal index failure")
            return await original_upsert(store, namespace, points)

        monkeypatch.setattr(PgVectorStore, "upsert", fail_first_index)
        monkeypatch.setattr("cairn.ingestion.runtime.get_pipeline_runtime", lambda: runtime)
        await _execute_pipeline(runtime.pipeline)
        document = await catalog.get_document(registration.document.id)
        assert document.state == "failed" and document.revision == 1
        await catalog.retry_document(pipeline_admin, document.id)
        assert (await catalog.get_document(document.id)).revision == 1
        await _execute_one(build_worker("index"))
        document = await catalog.get_document(document.id)
        assert document.state == "indexed"
        assert (await catalog.get_kb(kb.id)).active_index_version == 1
        vector_store = PgVectorStore(get_engine())
        namespace = Namespace(kb.id, 1)
        prepared = await runtime.embedding_for(await models.get_ref(model.id))
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
        monkeypatch.setattr("cairn.ingestion.runtime.get_pipeline_runtime", lambda: runtime)
        await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
        workers = [
            build_worker(queue) for queue in ("maintain", "parse", "chunk", "embed", "index")
        ]
        for _attempt in range(20):
            for worker in workers:
                for row in await worker._claim_batch():
                    await worker._execute(row)
            if (await catalog.get_kb(kb.id)).active_index_version == 2:
                break
        rebuilt = await catalog.publish_runtime(kb.id)
        assert rebuilt is not None and rebuilt.index_version == 2
        assert rebuilt.embedding_model.id == model.id
        assert rebuilt.embedding_model.dimension == 384
        rebuilt_hits = await vector_store.search(
            Namespace(kb.id, 2), VectorQuery(dense=ocean_query.values, top_k=3)
        )
        assert rebuilt_hits and "Pacific" in rebuilt_hits[0].content
        assert all("Saturn" not in hit.content for hit in rebuilt_hits)
        assert {hit.id for hit in rebuilt_hits}.isdisjoint(hit.id for hit in ocean_hits)
        manual_text = "The Mariana Trench lies in the western Pacific Ocean."
        await catalog.edit_chunk(pipeline_admin, kb.id, rebuilt_hits[0].id, manual_text)
        await _execute_one(build_worker("embed"))
        edited_points = await vector_store.fetch(Namespace(kb.id, 2), [rebuilt_hits[0].id])
        assert len(edited_points) == 1 and edited_points[0].content == manual_text
        assert edited_points[0].payload["content_hash"] == sha256(manual_text.encode()).hexdigest()
        trench_query = await prepared.service.embed_query(
            prepared.model, "Where is the Mariana Trench?"
        )
        manual_hits = await vector_store.search(
            Namespace(kb.id, 2), VectorQuery(dense=trench_query.values, top_k=3)
        )
        assert manual_hits and "Mariana Trench" in manual_hits[0].content
        monkeypatch.setenv("CAIRN_RETRIEVAL__EMBEDDING_ENDPOINTS", "{}")
        monkeypatch.setenv(
            "CAIRN_RETRIEVAL__TOKENIZER_FILES", json.dumps({"live-minilm": str(tokenizer)})
        )
        get_settings.cache_clear()
        await RuntimeRefresher(models=models, catalog=catalog).refresh(10)
        authz = AuthzService()
        principal = await authz.principal_for_user(pipeline_admin.id)
        assert principal is not None
        _api_key, raw_key = await authz.create_api_key(
            principal, ApiKeySpec(name="live-http-retrieval", scopes=["kb:query"], kb_ids=[kb.id])
        )
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as http,
        ):
            response = await http.post(
                "/v1/retrieval/query",
                headers={"Authorization": f"Bearer {raw_key}"},
                json={
                    "targets": [{"knowledge_base_id": encode_id("kb", kb.id)}],
                    "query": "Where is the Mariana Trench?",
                    "search_mode": "vector",
                    "rerank": {"enabled": False},
                    "options": {"expand_parent": False},
                },
            )
            http.headers["Authorization"] = f"Bearer {raw_key}"
            async with (
                streamable_http_client("http://localhost/mcp", http_client=http) as (
                    reader,
                    writer,
                    _session_id,
                ),
                ClientSession(reader, writer) as mcp_session,
            ):
                await mcp_session.initialize()
                await mcp_session.list_tools()
                tool_result = await mcp_session.call_tool(
                    "search_knowledge_base",
                    {
                        "query": "Where is the Mariana Trench?",
                        "knowledge_base_id": encode_id("kb", kb.id),
                    },
                )
                assert not tool_result.isError, tool_result
                assert tool_result.structuredContent is not None
                assert any(
                    "Mariana Trench" in hit["content"]
                    for hit in tool_result.structuredContent["results"]
                )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["results"] and "Mariana Trench" in body["results"][0]["content"]
        assert body["results"][0]["document_id"] == encode_id("doc", document.id)
        assert body["usage"]["index_versions"][encode_id("kb", kb.id)] == 2
    finally:
        await runtime.close()
        get_ingestion_runtime_settings.cache_clear()
