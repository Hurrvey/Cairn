"""Online-provider credentials and learned sparse vectors on real services.

PostgreSQL, Redis and Qdrant are real; the model is a deterministic stand-in
for bge-m3 that returns dense vectors and lexical weights in one call.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.conftest import qdrant_store
from tests.integration.test_ingestion_pipeline import (
    _execute_pipeline,
    _PipelineTokenizer,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.authz.service import AuthzService
from cairn.catalog.dto import CreateKbSpec, SparseChoice, UploadSpec
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.catalog.service import CatalogService
from cairn.core.cache import get_cache
from cairn.core.config import get_settings
from cairn.core.db import session_scope
from cairn.core.ids import encode_id
from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import ProviderRuntimeProjection, provider_runtime_key
from cairn.embedding.base import HybridOutput, Purpose
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import TokenizerRegistry
from cairn.ingestion.pipeline import IngestionPipeline, PipelineResources, PreparedEmbedding
from cairn.ingestion.registry import get_parser_registry
from cairn.modelgw.catalog import ModelCatalog
from cairn.modelgw.dto import RegisterModelSpec
from cairn.objectstore.local import LocalObjectStore
from cairn.retrieval.dto import RetrievalRequest
from cairn.retrieval.runtime import KnowledgeBaseRuntimeLoader, QueryEncoding
from cairn.retrieval.service import RetrievalService
from cairn.vectorstore.base import Namespace, SparseVector, VectorQuery

__all__ = ["pipeline_admin"]

SECRET = "sk-integration-secret-value"


def _terms(text_value: str) -> SparseVector | None:
    words = {word for word in re.findall(r"[a-z]+", text_value.lower()) if len(word) > 2}
    if not words:
        return None
    weights = {int(sha256(word.encode()).hexdigest()[:6], 16): 1.0 for word in words}
    ordered = sorted(weights.items())
    return SparseVector(tuple(i for i, _ in ordered), tuple(v for _, v in ordered))


class _DeterministicM3:
    """Dense and lexical weights in one call, like the bge-m3 sidecar."""

    cache_namespace = "learned-sparse-test-v1"
    max_batch_size = 16

    def __init__(self) -> None:
        self.calls: list[tuple[int, bool, bool]] = []

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        return (
            await self.embed_hybrid(model, texts, purpose=purpose, dense=True, sparse=False)
        ).dense  # type: ignore[return-value]

    async def embed_hybrid(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        dense: bool,
        sparse: bool,
    ) -> HybridOutput:
        self.calls.append((len(texts), dense, sparse))
        return HybridOutput(
            dense=[[float(len(item)), float(sum(map(ord, item)) % 97), 1.0] for item in texts]
            if dense
            else None,
            sparse=[_terms(item) for item in texts] if sparse else None,
        )


@pytest.mark.anyio
async def test_provider_key_is_sealed_at_rest_in_redis_and_opened_by_the_data_plane(
    pipeline_admin: Principal,
) -> None:
    models = ModelCatalog()
    workspace = pipeline_admin.workspace_id
    provider = await models.create_provider(
        workspace,
        name="aliyun",
        family="dashscope",
        base_url="https://dashscope.aliyuncs.com/api/v1",
        config={"binding_revision": "v1", "max_batch_size": 10},
        api_key=_secret(SECRET),
    )
    assert provider.has_credentials

    async with session_scope() as session:
        rows = (await session.execute(text("SELECT ciphertext, wrapped_dek FROM secret"))).all()
    assert len(rows) == 1
    assert SECRET.encode() not in bytes(rows[0][0]) + bytes(rows[0][1])

    assert await models.publish_provider_runtime(provider.id)
    raw = await get_cache().get(provider_runtime_key(provider.id))
    assert raw is not None and SECRET.encode() not in raw
    projection = ProviderRuntimeProjection.model_validate_json(raw)
    assert projection.credential is not None
    opened = projection.credential.open(get_settings().master_key, workspace)
    assert opened.get_secret_value() == SECRET
    assert (await models.provider_api_key(provider.id)).get_secret_value() == SECRET  # type: ignore[union-attr]

    first_secret = projection.credential.secret_id
    await models.set_provider_credentials(workspace, provider.id, _secret("sk-rotated"))
    assert (await models.provider_api_key(provider.id)).get_secret_value() == "sk-rotated"  # type: ignore[union-attr]
    async with session_scope() as session:
        ids = [row[0] for row in (await session.execute(text("SELECT id FROM secret"))).all()]
    assert first_secret not in ids and len(ids) == 1

    await models.delete_provider(workspace, provider.id)
    async with session_scope() as session:
        remaining = await session.scalar(text("SELECT count(*) FROM secret"))
    assert remaining == 0


@pytest.mark.anyio
async def test_learned_sparse_knowledge_base_indexes_and_queries_model_weights(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    workspace = pipeline_admin.workspace_id
    models = ModelCatalog()
    catalog = CatalogService()
    provider = await models.create_provider(
        workspace,
        name="bge-m3 sidecar",
        family="bge_m3",
        base_url="http://127.0.0.1:9",
        config={"binding_revision": "m3-v1", "allow_private": True},
    )
    registered = await models.register_model(
        workspace,
        RegisterModelSpec(
            provider_id=provider.id,
            model_key="BAAI/bge-m3",
            display_name="bge-m3",
            capability="embedding",
            dimension=3,
            max_input_tokens=512,
            normalize=False,
            tokenizer_id="pipeline-test-tokenizer",
            sparse=True,
        ),
    )
    vectors = await catalog.create_binding(
        pipeline_admin, kind="vector", driver="qdrant", name="sparse-vectors", config={}
    )
    objects = await catalog.create_binding(
        pipeline_admin,
        kind="object",
        driver="local",
        name="sparse-objects",
        config={"path": str(tmp_path)},
    )
    kb = await catalog.create_kb(
        pipeline_admin,
        CreateKbSpec(
            name="Learned sparse",
            embedding_model_id=registered.id,
            vector_binding_id=vectors.id,
            object_binding_id=objects.id,
        ),
    )
    # Automatic choice: the embedding model emits sparse vectors itself.
    assert kb.sparse_kind == "model" and kb.sparse_model_id == registered.id

    source = b"# Saturn\n\nSaturn has icy rings and many moons.\n\n# Mars\n\nMars is a red desert."
    digest = sha256(source).hexdigest()
    object_key = f"{workspace}/{kb.id}/originals/{digest}"
    await LocalObjectStore(tmp_path).put(object_key, source, content_type="text/markdown")
    registration = await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="planets.md",
            content_hash=digest,
            size_bytes=len(source),
            mime_type="text/markdown",
            object_key=object_key,
        ),
    )
    assert registration.document is not None

    model = await models.get_ref(registered.id)
    tokenizer = _PipelineTokenizer()
    tokenizers = TokenizerRegistry()
    tokenizers.register("pipeline-test-tokenizer", tokenizer)
    provider_impl = _DeterministicM3()
    service = EmbeddingService(provider=provider_impl, cache=get_cache(), tokenizers=tokenizers)
    store = qdrant_store()

    async def object_store_for(_binding):  # type: ignore[no-untyped-def]
        return LocalObjectStore(tmp_path)

    async def vector_store_for(_binding):  # type: ignore[no-untyped-def]
        return store

    async def embedding_for(snapshot):  # type: ignore[no-untyped-def]
        assert snapshot == model
        return PreparedEmbedding(
            model=model, tokenizer=tokenizer, service=service, binding_fingerprint="m3-binding"
        )

    pipeline = IngestionPipeline(
        catalog=CatalogIngestionFacade(),
        resources=PipelineResources(
            parsers=get_parser_registry(),
            object_store_for=object_store_for,
            vector_store_for=vector_store_for,
            embedding_for=embedding_for,
        ),
    )
    await _execute_pipeline(pipeline)

    document = await catalog.get_document(registration.document.id)
    assert document.state == "indexed"
    # Dense and sparse came from single calls, never two passes per chunk.
    assert provider_impl.calls and all(
        dense and sparse for _n, dense, sparse in provider_impl.calls
    )

    namespace = Namespace(kb.id, 1)
    spec = await store._spec_for(namespace)
    assert spec.sparse_modifier == "none"
    query_terms = _terms("icy rings")
    hits = await store.search(namespace, VectorQuery(sparse=query_terms, top_k=3))
    assert hits and "rings" in hits[0].payload["content"]
    # Learned weights are used as given: two matching terms, weight 1 each.
    assert hits[0].score == pytest.approx(2.0)

    published = await catalog.publish_runtime(kb.id)
    assert published is not None and published.sparse.model == model

    class _Encoder:
        async def embed_query(self, model_ref, query):  # type: ignore[no-untyped-def]
            vector = await service.embed_query(model_ref, query)
            return vector.values, 1, None

        async def encode_query_for_workspace(  # type: ignore[no-untyped-def]
            self, workspace_id, model_ref, query, *, dense, sparse
        ):
            dense_vector, sparse_vector = await service.embed_query_hybrid(
                model_ref, query, dense=dense, sparse=sparse
            )
            return QueryEncoding(
                dense=dense_vector.values if dense_vector else None,
                sparse=sparse_vector,
                tokens=1,
            )

    retrieval = RetrievalService(runtime_loader=KnowledgeBaseRuntimeLoader(), embeddings=_Encoder())
    # Grants taken after the knowledge base was created, as a fresh request would see them.
    principal = await AuthzService().principal_for_user(pipeline_admin.id)
    assert principal is not None
    response = await retrieval.query(
        principal,
        RetrievalRequest.model_validate(
            {
                "targets": [{"knowledge_base_id": encode_id("kb", kb.id)}],
                "query": "icy rings",
                "search_mode": "fulltext",
                "rerank": {"enabled": False},
                "options": {"expand_parent": False},
            }
        ),
        request_id="req_sparse",
    )
    assert response.results and "rings" in response.results[0].content

    rebuilt_choice = SparseChoice(kind="bm25")
    assert rebuilt_choice.kind == "bm25"
    await store.close()


def _secret(value: str):  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    return SecretStr(value)
