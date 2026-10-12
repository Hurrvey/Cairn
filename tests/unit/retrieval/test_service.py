from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from uuid import UUID

import pytest

from cairn.authz.model import Principal
from cairn.core.cache import InMemoryCache
from cairn.core.errors import PermissionDenied, UpstreamUnavailable, ValidationFailed
from cairn.core.ids import encode_id
from cairn.core.modelref import ModelRef
from cairn.core.retrieval_runtime import BindingRefModel, KnowledgeBaseRuntime, RetrievalConfig
from cairn.embedding.sparse import encode_query
from cairn.retrieval.dto import RetrievalRequest
from cairn.retrieval.errors import (
    KbRuntimeUnavailable,
    RetrievalReadModelInvalid,
    RetrievalUnsupported,
)
from cairn.retrieval.rerank import RerankUnavailable
from cairn.retrieval.runtime import KnowledgeBaseRuntimeLoader
from cairn.retrieval.service import RetrievalService
from cairn.vectorstore.base import Hit, Namespace, VectorQuery

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
ALLOWED_KB_ID = UUID("20000000-0000-0000-0000-000000000002")
OTHER_KB_ID = UUID("30000000-0000-0000-0000-000000000003")
MODEL_ID = UUID("40000000-0000-0000-0000-000000000004")
PROVIDER_ID = UUID("50000000-0000-0000-0000-000000000005")
BINDING_ID = UUID("60000000-0000-0000-0000-000000000006")
DOCUMENT_ID = UUID("70000000-0000-0000-0000-000000000007")
CHUNK_ID = UUID("80000000-0000-0000-0000-000000000008")
SECOND_CHUNK_ID = UUID("80000000-0000-0000-0000-000000000009")
PARENT_CHUNK_ID = UUID("80000000-0000-0000-0000-000000000010")


def runtime(
    *,
    kb_id: UUID = ALLOWED_KB_ID,
    workspace_id: UUID = WORKSPACE_ID,
    status: str = "active",
    index_version: int = 1,
    config: RetrievalConfig | None = None,
    parent_snapshots_safe: bool = True,
) -> KnowledgeBaseRuntime:
    return KnowledgeBaseRuntime(
        id=kb_id,
        workspace_id=workspace_id,
        index_version=index_version,
        embedding_model=ModelRef(
            id=MODEL_ID,
            provider_family="tei",
            model_key="unit",
            capability="embedding",
            dimension=3,
            max_input_tokens=128,
            tokenizer_id="unit",
            provider_id=PROVIDER_ID,
        ),
        metric="cosine",
        vector_binding=BindingRefModel(id=BINDING_ID, driver="qdrant"),
        retrieval_config=config
        or RetrievalConfig(rerank={"enabled": False}, expand_parent=False, candidate_k=10),
        config_version=1,
        status=status,
        parent_snapshots_safe=parent_snapshots_safe,
    )


def principal(*kb_ids: UUID) -> Principal:
    permissions = {kb_id: frozenset({"kb:query"}) for kb_id in kb_ids}
    return Principal(
        type="api_key",
        id=UUID("90000000-0000-0000-0000-000000000009"),
        workspace_id=WORKSPACE_ID,
        role="user",
        username="retrieval-key",
        resource_permissions=permissions,
        accessible_kb_ids=frozenset(kb_ids),
    )


class Loader:
    def __init__(
        self,
        runtimes: dict[UUID, KnowledgeBaseRuntime],
        failures: dict[UUID, Exception] | None = None,
    ) -> None:
        self.runtimes = runtimes
        self.failures = failures or {}
        self.calls: list[tuple[UUID, UUID]] = []

    async def load_one(self, kb_id: UUID, workspace_id: UUID) -> KnowledgeBaseRuntime:
        self.calls.append((kb_id, workspace_id))
        failure = self.failures.get(kb_id)
        if failure is not None:
            raise failure
        return self.runtimes[kb_id]


class Store:
    def __init__(
        self,
        *,
        fail: bool = False,
        token_count: int | None = 7,
        score: float = 0.8,
        payload_version: int | None = None,
    ) -> None:
        self.fail = fail
        self.token_count = token_count
        self.score = score
        self.payload_version = payload_version
        self.calls: list[tuple[Namespace, VectorQuery]] = []

    async def search(self, namespace: Namespace, query: VectorQuery) -> list[Hit]:
        self.calls.append((namespace, query))
        if self.fail:
            raise RuntimeError("query text must never appear in an error")
        return [
            Hit(
                id=CHUNK_ID,
                score=self.score,
                payload={
                    "chunk_id": str(CHUNK_ID),
                    "document_id": str(DOCUMENT_ID),
                    "kb_id": str(namespace.kb_id),
                    "index_version": self.payload_version or namespace.index_version,
                    "content": "Saturn has rings.",
                    "token_count": self.token_count,
                    "page": 4,
                    "bbox": [1.0, 2.0, 3.0, 4.0],
                    "source_url": "https://example.test/saturn",
                },
            )
        ]


class Registry:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls = 0

    async def for_binding(self, binding: object) -> Store:
        self.calls += 1
        return self.store


class Embeddings:
    def __init__(self) -> None:
        self.calls: list[tuple[ModelRef, str]] = []

    async def embed_query(
        self, model: ModelRef, query: str
    ) -> tuple[list[float], int, bool | None]:
        self.calls.append((model, query))
        return [1.0, 1.0, 1.0], 2, False


def hit_payload(
    chunk_id: UUID,
    *,
    content: str,
    token_count: int = 7,
    parent_id: UUID | None = None,
    parent_snapshot: dict[str, object] | None = None,
    manual_edit_generation: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "chunk_id": str(chunk_id),
        "document_id": str(DOCUMENT_ID),
        "kb_id": str(ALLOWED_KB_ID),
        "index_version": 1,
        "revision": 2,
        "content": content,
        "token_count": token_count,
        "parent_id": str(parent_id) if parent_id is not None else None,
    }
    if parent_snapshot is not None:
        payload["_parent_snapshot"] = parent_snapshot
    if manual_edit_generation is not None:
        payload["manual_edit_generation"] = manual_edit_generation
    return payload


class AdvancedStore(Store):
    def __init__(self, *, parent_payloads: list[Hit] | None = None) -> None:
        super().__init__()
        self.parent_payloads = parent_payloads

    async def search(self, namespace: Namespace, query: VectorQuery) -> list[Hit]:
        self.calls.append((namespace, query))
        if self.parent_payloads is not None:
            return self.parent_payloads
        if query.dense is not None:
            return [
                Hit(CHUNK_ID, 10.0, hit_payload(CHUNK_ID, content="dense first")),
                Hit(SECOND_CHUNK_ID, 0.0, hit_payload(SECOND_CHUNK_ID, content="dense second")),
            ]
        return [
            Hit(SECOND_CHUNK_ID, 100.0, hit_payload(SECOND_CHUNK_ID, content="sparse first")),
            Hit(CHUNK_ID, 50.0, hit_payload(CHUNK_ID, content="sparse second")),
        ]


class RecordingReranker:
    def __init__(
        self,
        *,
        supported: bool = True,
        scores: list[float] | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.supported = supported
        self.scores = scores or [0.1, 0.9]
        self.failure = failure
        self.calls: list[tuple[UUID, str, str, list[str], float]] = []

    def supports(self, model_id: str | None) -> bool:
        return self.supported and model_id == "reranker-v1"

    async def score(
        self,
        workspace_id: UUID,
        model_id: str,
        query: str,
        documents: list[str],
        *,
        timeout_s: float,
    ) -> list[float]:
        self.calls.append((workspace_id, model_id, query, list(documents), timeout_s))
        if self.failure is not None:
            raise self.failure
        return self.scores[: len(documents)]


def request(mode: str, *, vector: bool = False, **extra: object) -> RetrievalRequest:
    payload: dict[str, object] = {
        "targets": [{"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)}],
        "query": "Saturn",
        "search_mode": mode,
        "rerank": {"enabled": False},
        "options": {"expand_parent": False},
        **extra,
    }
    if vector:
        payload["query_vector"] = [1.0, 1.0, 1.0]
    return RetrievalRequest.model_validate(payload)


@pytest.mark.anyio
async def test_loader_accepts_indexing_runtime_but_uses_published_active_version() -> None:
    cache = InMemoryCache()
    rebuilding = runtime(status="indexing", index_version=1)
    await cache.set(f"kb:runtime:{ALLOWED_KB_ID}", rebuilding.model_dump_json().encode(), 300)

    loaded = await KnowledgeBaseRuntimeLoader(cache=cache, timeout_s=0.1).load_many(
        [ALLOWED_KB_ID], WORKSPACE_ID
    )

    assert loaded == [rebuilding]
    assert loaded[0].index_version == 1


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", ["missing", "wrong_kb", "foreign_workspace", "deleting"])
async def test_loader_rejects_missing_or_incoherent_runtime(invalid: str) -> None:
    cache = InMemoryCache()
    value = {
        "wrong_kb": runtime(kb_id=OTHER_KB_ID),
        "foreign_workspace": runtime(workspace_id=OTHER_KB_ID),
        "deleting": runtime(status="deleting"),
    }.get(invalid)
    if value is not None:
        await cache.set(f"kb:runtime:{ALLOWED_KB_ID}", value.model_dump_json().encode(), 300)

    with pytest.raises((KbRuntimeUnavailable, PermissionDenied)):
        await KnowledgeBaseRuntimeLoader(cache=cache, timeout_s=0.1).load_many(
            [ALLOWED_KB_ID], WORKSPACE_ID
        )


@pytest.mark.anyio
async def test_authorizes_every_target_before_runtime_or_storage_access() -> None:
    loader = Loader({ALLOWED_KB_ID: runtime()})
    registry = Registry(Store())
    service = RetrievalService(runtime_loader=loader, vectors=registry, embeddings=Embeddings())
    req = RetrievalRequest(
        targets=[
            {"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)},
            {"knowledge_base_id": encode_id("kb", OTHER_KB_ID)},
        ],
        query="restricted information",
    )

    with pytest.raises(PermissionDenied):
        await service.query(principal(ALLOWED_KB_ID), req, request_id="req_test")

    assert loader.calls == []
    assert registry.calls == 0


@pytest.mark.anyio
async def test_all_zero_target_weights_are_rejected_before_runtime_access() -> None:
    loader = Loader({ALLOWED_KB_ID: runtime()})
    service = RetrievalService(
        runtime_loader=loader,
        vectors=Registry(Store()),
        embeddings=Embeddings(),
    )
    req = RetrievalRequest(
        targets=[{"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID), "weight": 0}],
        query="Saturn",
        search_mode="fulltext",
    )

    with pytest.raises(ValidationFailed, match="target weight"):
        await service.query(principal(ALLOWED_KB_ID), req, request_id="req_test")

    assert loader.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("mode,expected_calls", [("fulltext", 1), ("vector", 1), ("hybrid", 2)])
async def test_supplied_vector_modes_search_active_namespace_without_embedding(
    mode: str, expected_calls: int
) -> None:
    store = Store()
    embeddings = Embeddings()
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime(status="indexing", index_version=1)}),
        vectors=Registry(store),
        embeddings=embeddings,
    )

    response = await service.query(
        principal(ALLOWED_KB_ID),
        request(mode, vector=mode != "fulltext"),
        request_id="req_test",
    )

    assert len(store.calls) == expected_calls
    assert all(namespace == Namespace(ALLOWED_KB_ID, 1) for namespace, _ in store.calls)
    assert embeddings.calls == []
    assert response.results[0].document_id == encode_id("doc", DOCUMENT_ID)
    assert response.results[0].token_count == 7
    assert response.results[0].source is not None
    assert response.results[0].source.url == "https://example.test/saturn"
    assert response.usage.index_versions == {encode_id("kb", ALLOWED_KB_ID): 1}


@pytest.mark.anyio
async def test_fulltext_searches_with_a_bm25_query_vector() -> None:
    store = Store()
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
    )

    await service.query(principal(ALLOWED_KB_ID), request("fulltext"), request_id="req_test")

    query = store.calls[0][1]
    assert query.dense is None
    assert query.sparse == encode_query("Saturn")


@pytest.mark.anyio
async def test_fulltext_query_without_terms_searches_nothing() -> None:
    store = Store()
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
    )

    response = await service.query(
        principal(ALLOWED_KB_ID), request("fulltext", query="?!"), request_id="req_test"
    )

    assert store.calls == []
    assert response.results == []


@pytest.mark.anyio
async def test_vector_only_request_degrades_hybrid_default_before_spawning_searches() -> None:
    store = Store()
    service = RetrievalService(
        runtime_loader=Loader(
            {
                ALLOWED_KB_ID: runtime(
                    config=RetrievalConfig(rerank={"enabled": False}, expand_parent=False)
                )
            }
        ),
        vectors=Registry(store),
        embeddings=Embeddings(),
    )
    req = RetrievalRequest(
        targets=[{"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)}],
        query_vector=[1.0, 1.0, 1.0],
        rerank={"enabled": False},
        options={"expand_parent": False},
    )

    response = await service.query(principal(ALLOWED_KB_ID), req, request_id="req_test")

    assert len(store.calls) == 1
    assert store.calls[0][1].dense == [1.0, 1.0, 1.0]
    assert store.calls[0][1].sparse is None
    assert [notice.stage for notice in response.degraded] == ["sparse"]


@pytest.mark.anyio
async def test_single_mode_score_uses_target_weight_not_hybrid_component_weight() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(Store(score=0.8)),
        embeddings=Embeddings(),
    )

    response = await service.query(
        principal(ALLOWED_KB_ID), request("fulltext"), request_id="req_test"
    )

    assert response.results[0].score == pytest.approx(0.8)


@pytest.mark.anyio
async def test_rrf_uses_rank_and_target_weight_not_dense_sparse_weights() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(Store()),
        embeddings=Embeddings(),
    )

    response = await service.query(
        principal(ALLOWED_KB_ID), request("hybrid", vector=True), request_id="req_test"
    )

    assert response.results[0].score == pytest.approx(2 / 61)


@pytest.mark.anyio
async def test_weighted_fusion_normalizes_stages_and_honors_explicit_weights() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore()),
        embeddings=Embeddings(),
    )

    response = await service.query(
        principal(ALLOWED_KB_ID),
        request(
            "hybrid",
            vector=True,
            top_k=2,
            fusion={"method": "weighted"},
            weights={"dense": 0.75, "sparse": 0.25},
        ),
        request_id="req_test",
    )

    assert [hit.chunk_id for hit in response.results] == [
        encode_id("chk", CHUNK_ID),
        encode_id("chk", SECOND_CHUNK_ID),
    ]
    assert [hit.score for hit in response.results] == pytest.approx([0.75, 0.25])
    assert response.results[0].scores["dense_normalized"] == 1.0
    assert response.results[0].scores["sparse_normalized"] == 0.0


@pytest.mark.anyio
async def test_weighted_fusion_rejects_all_zero_weights() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore()),
        embeddings=Embeddings(),
    )

    with pytest.raises(ValidationFailed, match="non-zero"):
        await service.query(
            principal(ALLOWED_KB_ID),
            request(
                "hybrid",
                vector=True,
                fusion={"method": "weighted"},
                weights={"dense": 0.0, "sparse": 0.0},
            ),
            request_id="req_test",
        )


@pytest.mark.anyio
async def test_configured_unsupported_defaults_degrade_but_explicit_request_fails() -> None:
    configured = runtime(config=RetrievalConfig())
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: configured}),
        vectors=Registry(Store()),
        embeddings=Embeddings(),
    )
    minimal = RetrievalRequest(
        targets=[{"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)}],
        query="Saturn",
        search_mode="fulltext",
    )

    response = await service.query(principal(ALLOWED_KB_ID), minimal, request_id="req_test")

    assert {notice.stage for notice in response.degraded} == {"rerank"}

    with pytest.raises(ValidationFailed):
        await service.query(
            principal(ALLOWED_KB_ID),
            RetrievalRequest(
                targets=[{"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)}],
                query="Saturn",
                search_mode="fulltext",
                rerank={"enabled": True},
                options={"expand_parent": False},
            ),
            request_id="req_test",
        )


@pytest.mark.anyio
async def test_configured_threshold_and_dedupe_apply_without_degradation() -> None:
    config = RetrievalConfig(
        score_threshold=0.9,
        dedupe="by_document",
        rerank={"enabled": False},
        expand_parent=False,
    )
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime(config=config)}),
        vectors=Registry(Store(score=0.8)),
        embeddings=Embeddings(),
    )

    response = await service.query(
        principal(ALLOWED_KB_ID), request("fulltext"), request_id="req_test"
    )

    assert response.results == []
    assert response.degraded == []


@pytest.mark.anyio
async def test_request_weights_are_rejected_when_weighted_fusion_is_unsupported() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(Store()),
        embeddings=Embeddings(),
    )

    with pytest.raises(ValidationFailed):
        await service.query(
            principal(ALLOWED_KB_ID),
            request("hybrid", vector=True, weights={"dense": 0.4, "sparse": 0.6}),
            request_id="req_test",
        )


def parent_snapshot(
    *,
    kb_id: UUID = ALLOWED_KB_ID,
    token_count: int = 20,
    index_version: int = 1,
    revision: int = 2,
) -> dict[str, object]:
    return {
        "id": str(PARENT_CHUNK_ID),
        "content": "Expanded parent context",
        "token_count": token_count,
        "document_id": str(DOCUMENT_ID),
        "kb_id": str(kb_id),
        "index_version": index_version,
        "revision": revision,
        "metadata": {"language": "en", "heading_path": ["Parent"]},
    }


@pytest.mark.anyio
async def test_parent_expansion_deduplicates_siblings_using_best_child_score() -> None:
    hits = [
        Hit(
            CHUNK_ID,
            0.9,
            hit_payload(
                CHUNK_ID,
                content="best child",
                parent_id=PARENT_CHUNK_ID,
                parent_snapshot=parent_snapshot(),
            ),
        ),
        Hit(
            SECOND_CHUNK_ID,
            0.8,
            hit_payload(
                SECOND_CHUNK_ID,
                content="sibling child",
                parent_id=PARENT_CHUNK_ID,
                parent_snapshot=parent_snapshot(),
            ),
        ),
    ]
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=hits)),
        embeddings=Embeddings(),
    ).query(
        principal(ALLOWED_KB_ID),
        request("fulltext", top_k=2, options={"expand_parent": True}),
        request_id="req_test",
    )

    assert len(response.results) == 1
    assert response.results[0].chunk_id == encode_id("chk", PARENT_CHUNK_ID)
    assert response.results[0].content == "Expanded parent context"
    assert response.results[0].score == pytest.approx(0.9)
    assert response.results[0].matched_child_ids == [
        encode_id("chk", CHUNK_ID),
        encode_id("chk", SECOND_CHUNK_ID),
    ]


@pytest.mark.anyio
async def test_parent_expansion_missing_snapshot_retains_child_with_degradation() -> None:
    hit = Hit(
        CHUNK_ID,
        0.9,
        hit_payload(CHUNK_ID, content="legacy child", parent_id=PARENT_CHUNK_ID),
    )
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=[hit])),
        embeddings=Embeddings(),
    ).query(
        principal(ALLOWED_KB_ID),
        request("fulltext", options={"expand_parent": True}),
        request_id="req_test",
    )

    assert response.results[0].chunk_id == encode_id("chk", CHUNK_ID)
    assert any(
        notice.stage == "expand_parent" and notice.reason == "parent_snapshot_missing"
        for notice in response.degraded
    )


@pytest.mark.anyio
async def test_parent_expansion_fails_closed_when_runtime_snapshot_safety_is_false() -> None:
    hit = Hit(
        CHUNK_ID,
        0.9,
        hit_payload(
            CHUNK_ID,
            content="corrected child",
            parent_id=PARENT_CHUNK_ID,
            parent_snapshot=parent_snapshot(),
        ),
    )
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime(parent_snapshots_safe=False)}),
        vectors=Registry(AdvancedStore(parent_payloads=[hit])),
        embeddings=Embeddings(),
    ).query(
        principal(ALLOWED_KB_ID),
        request("fulltext", options={"expand_parent": True}),
        request_id="req_test",
    )

    assert response.results[0].chunk_id == encode_id("chk", CHUNK_ID)
    assert response.results[0].content == "corrected child"
    assert any(notice.reason == "parent_snapshots_unsafe" for notice in response.degraded)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "snapshot",
    [
        parent_snapshot(kb_id=OTHER_KB_ID),
        parent_snapshot(index_version=2),
        parent_snapshot(revision=3),
    ],
)
async def test_parent_expansion_rejects_cross_scope_snapshot(
    snapshot: dict[str, object],
) -> None:
    hit = Hit(
        CHUNK_ID,
        0.9,
        hit_payload(
            CHUNK_ID,
            content="child",
            parent_id=PARENT_CHUNK_ID,
            parent_snapshot=snapshot,
        ),
    )
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=[hit])),
        embeddings=Embeddings(),
    )

    with pytest.raises(RetrievalReadModelInvalid):
        await service.query(
            principal(ALLOWED_KB_ID),
            request("fulltext", options={"expand_parent": True}),
            request_id="req_test",
        )


@pytest.mark.anyio
async def test_parent_expansion_retains_manually_edited_child() -> None:
    hit = Hit(
        CHUNK_ID,
        0.9,
        hit_payload(
            CHUNK_ID,
            content="corrected child",
            parent_id=PARENT_CHUNK_ID,
            parent_snapshot=parent_snapshot(),
            manual_edit_generation=2,
        ),
    )
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=[hit])),
        embeddings=Embeddings(),
    ).query(
        principal(ALLOWED_KB_ID),
        request("fulltext", options={"expand_parent": True}),
        request_id="req_test",
    )

    assert response.results[0].chunk_id == encode_id("chk", CHUNK_ID)
    assert response.results[0].content == "corrected child"
    assert any(notice.reason == "manual_child_edit" for notice in response.degraded)


@pytest.mark.anyio
async def test_parent_token_count_is_budgeted_after_expansion() -> None:
    hit = Hit(
        CHUNK_ID,
        0.9,
        hit_payload(
            CHUNK_ID,
            content="small child",
            parent_id=PARENT_CHUNK_ID,
            parent_snapshot=parent_snapshot(token_count=120),
        ),
    )
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=[hit])),
        embeddings=Embeddings(),
    ).query(
        principal(ALLOWED_KB_ID),
        request(
            "fulltext",
            options={"expand_parent": True, "max_context_tokens": 100},
        ),
        request_id="req_test",
    )

    assert response.results == []
    assert response.truncated_to_token_budget is True


def rerank_hits() -> list[Hit]:
    return [
        Hit(CHUNK_ID, 0.9, hit_payload(CHUNK_ID, content="first fused")),
        Hit(SECOND_CHUNK_ID, 0.8, hit_payload(SECOND_CHUNK_ID, content="second fused")),
    ]


@pytest.mark.anyio
async def test_rerank_reorders_bounded_candidates_and_reports_usage() -> None:
    reranker = RecordingReranker(scores=[0.1, 0.9])
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=rerank_hits())),
        embeddings=Embeddings(),
        reranker=reranker,
    ).query(
        principal(ALLOWED_KB_ID),
        request(
            "fulltext",
            top_k=2,
            rerank={"enabled": True, "model_id": "reranker-v1", "top_n": 1},
        ),
        request_id="req_test",
    )

    assert [hit.chunk_id for hit in response.results] == [encode_id("chk", SECOND_CHUNK_ID)]
    # The reranker's score is final; the fused score stays visible.
    assert response.results[0].score == pytest.approx(0.9)
    assert response.results[0].scores["rerank"] == pytest.approx(0.9)
    assert response.results[0].scores["fused"] == pytest.approx(0.8)
    assert response.results[0].scores["fused"] == pytest.approx(0.8)
    assert response.results[0].scores["rerank"] == pytest.approx(0.9)
    assert response.usage.rerank_units == 2
    assert reranker.calls[0][3] == ["first fused", "second fused"]


@pytest.mark.anyio
async def test_rerank_provider_failure_degrades_to_fused_order() -> None:
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=rerank_hits())),
        embeddings=Embeddings(),
        reranker=RecordingReranker(failure=RerankUnavailable("offline")),
    ).query(
        principal(ALLOWED_KB_ID),
        request(
            "fulltext",
            top_k=2,
            rerank={"enabled": True, "model_id": "reranker-v1", "top_n": 2},
        ),
        request_id="req_test",
    )

    assert [hit.chunk_id for hit in response.results] == [
        encode_id("chk", CHUNK_ID),
        encode_id("chk", SECOND_CHUNK_ID),
    ]
    assert response.usage.rerank_units == 0
    assert any(
        notice.stage == "rerank" and notice.reason == "unavailable" for notice in response.degraded
    )


@pytest.mark.anyio
async def test_rerank_cross_workspace_denial_propagates() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=rerank_hits())),
        embeddings=Embeddings(),
        reranker=RecordingReranker(failure=PermissionDenied("cross-workspace")),
    )

    with pytest.raises(PermissionDenied):
        await service.query(
            principal(ALLOWED_KB_ID),
            request(
                "fulltext",
                rerank={"enabled": True, "model_id": "reranker-v1"},
            ),
            request_id="req_test",
        )


@pytest.mark.anyio
async def test_explicit_rerank_requires_query_text_and_supported_model() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=rerank_hits())),
        embeddings=Embeddings(),
        reranker=RecordingReranker(supported=False),
    )
    vector_only = RetrievalRequest(
        targets=[{"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)}],
        query_vector=[1.0, 1.0, 1.0],
        search_mode="vector",
        rerank={"enabled": True, "model_id": "reranker-v1"},
        options={"expand_parent": False},
    )

    with pytest.raises(ValidationFailed, match="query text"):
        await service.query(principal(ALLOWED_KB_ID), vector_only, request_id="req_test")
    with pytest.raises(RetrievalUnsupported, match="not configured"):
        await service.query(
            principal(ALLOWED_KB_ID),
            request(
                "fulltext",
                rerank={"enabled": True, "model_id": "reranker-v1"},
            ),
            request_id="req_test",
        )


@pytest.mark.anyio
async def test_explicit_rerank_disabled_overrides_configured_default() -> None:
    reranker = RecordingReranker()
    configured = RetrievalConfig(
        rerank={"enabled": True, "model_id": "reranker-v1"},
        expand_parent=False,
    )
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime(config=configured)}),
        vectors=Registry(AdvancedStore(parent_payloads=rerank_hits())),
        embeddings=Embeddings(),
        reranker=reranker,
    ).query(
        principal(ALLOWED_KB_ID),
        request("fulltext", rerank={"enabled": False}),
        request_id="req_test",
    )

    assert reranker.calls == []
    assert not any(notice.stage == "rerank" for notice in response.degraded)


@pytest.mark.anyio
async def test_configured_rerank_uses_single_runtime_model() -> None:
    reranker = RecordingReranker(scores=[0.1, 0.9])
    configured = RetrievalConfig(
        rerank={"enabled": True, "model_id": "reranker-v1", "top_n": 1},
        expand_parent=False,
    )
    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime(config=configured)}),
        vectors=Registry(AdvancedStore(parent_payloads=rerank_hits())),
        embeddings=Embeddings(),
        reranker=reranker,
    ).query(
        principal(ALLOWED_KB_ID),
        RetrievalRequest(
            targets=[{"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)}],
            query="Saturn",
            search_mode="fulltext",
            top_k=2,
            options={"expand_parent": False},
        ),
        request_id="req_test",
    )

    assert [hit.chunk_id for hit in response.results] == [encode_id("chk", SECOND_CHUNK_ID)]
    assert response.usage.rerank_units == 2


@pytest.mark.anyio
async def test_rerank_cancellation_propagates() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(AdvancedStore(parent_payloads=rerank_hits())),
        embeddings=Embeddings(),
        reranker=RecordingReranker(failure=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await service.query(
            principal(ALLOWED_KB_ID),
            request(
                "fulltext",
                rerank={"enabled": True, "model_id": "reranker-v1"},
            ),
            request_id="req_test",
        )


@pytest.mark.anyio
async def test_all_failed_targets_raise_upstream_instead_of_masked_empty_success() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(Store(fail=True)),
        embeddings=Embeddings(),
    )

    with pytest.raises(UpstreamUnavailable) as caught:
        await service.query(principal(ALLOWED_KB_ID), request("fulltext"), request_id="req_test")

    assert "query text" not in caught.value.detail.lower()


@pytest.mark.anyio
async def test_token_budget_fails_when_legacy_hit_has_unknown_count() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(Store(token_count=None)),
        embeddings=Embeddings(),
    )

    with pytest.raises(RetrievalReadModelInvalid):
        await service.query(
            principal(ALLOWED_KB_ID),
            request("fulltext", options={"expand_parent": False, "max_context_tokens": 100}),
            request_id="req_test",
        )


@pytest.mark.anyio
async def test_nonstrict_runtime_failure_returns_other_target_and_names_failure() -> None:
    other = runtime(kb_id=OTHER_KB_ID)
    loader = Loader(
        {OTHER_KB_ID: other},
        failures={ALLOWED_KB_ID: KbRuntimeUnavailable("runtime missing")},
    )
    service = RetrievalService(
        runtime_loader=loader,
        vectors=Registry(Store()),
        embeddings=Embeddings(),
    )
    req = RetrievalRequest(
        targets=[
            {"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)},
            {"knowledge_base_id": encode_id("kb", OTHER_KB_ID)},
        ],
        query="Saturn",
        search_mode="fulltext",
        rerank={"enabled": False},
        options={"expand_parent": False},
    )

    response = await service.query(
        principal(ALLOWED_KB_ID, OTHER_KB_ID), req, request_id="req_test"
    )

    assert {hit.knowledge_base_id for hit in response.results} == {encode_id("kb", OTHER_KB_ID)}
    assert [failure.knowledge_base_id for failure in response.partial_failures] == [
        encode_id("kb", ALLOWED_KB_ID)
    ]


class SelectiveEmbeddings(Embeddings):
    async def embed_query(
        self, model: ModelRef, query: str
    ) -> tuple[list[float], int, bool | None]:
        if model.id == MODEL_ID:
            raise RuntimeError("first provider unavailable")
        return await super().embed_query(model, query)


@pytest.mark.anyio
async def test_nonstrict_embedding_failure_preserves_other_model_target() -> None:
    other = runtime(kb_id=OTHER_KB_ID).model_copy(
        update={
            "embedding_model": replace(
                runtime().embedding_model,
                id=UUID("41000000-0000-0000-0000-000000000004"),
                provider_id=UUID("51000000-0000-0000-0000-000000000005"),
                model_key="other",
            )
        }
    )
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime(), OTHER_KB_ID: other}),
        vectors=Registry(Store()),
        embeddings=SelectiveEmbeddings(),
    )
    req = RetrievalRequest(
        targets=[
            {"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)},
            {"knowledge_base_id": encode_id("kb", OTHER_KB_ID)},
        ],
        query="Saturn",
        search_mode="vector",
        rerank={"enabled": False},
        options={"expand_parent": False},
    )

    response = await service.query(
        principal(ALLOWED_KB_ID, OTHER_KB_ID), req, request_id="req_test"
    )

    assert {hit.knowledge_base_id for hit in response.results} == {encode_id("kb", OTHER_KB_ID)}
    assert [failure.knowledge_base_id for failure in response.partial_failures] == [
        encode_id("kb", ALLOWED_KB_ID)
    ]


@pytest.mark.anyio
async def test_query_embedding_input_rejection_remains_client_error() -> None:
    class RejectingEmbeddings(Embeddings):
        async def embed_query(
            self, model: ModelRef, query: str
        ) -> tuple[list[float], int, bool | None]:
            raise RetrievalUnsupported("Query exceeds the model token budget.")

    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(Store()),
        embeddings=RejectingEmbeddings(),
    )

    with pytest.raises(RetrievalUnsupported, match="token"):
        await service.query(principal(ALLOWED_KB_ID), request("vector"), request_id="req_test")


@pytest.mark.anyio
async def test_unknown_embedding_cache_state_remains_unknown() -> None:
    class UnknownCacheEmbeddings(Embeddings):
        async def embed_query(
            self, model: ModelRef, query: str
        ) -> tuple[list[float], int, bool | None]:
            self.calls.append((model, query))
            return [1.0, 1.0, 1.0], 2, None

    response = await RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(Store()),
        embeddings=UnknownCacheEmbeddings(),
    ).query(principal(ALLOWED_KB_ID), request("vector"), request_id="req_test")

    assert response.usage.cached_embedding is None


class SlowLoader(Loader):
    async def load_one(self, kb_id: UUID, workspace_id: UUID) -> KnowledgeBaseRuntime:
        await asyncio.sleep(1)
        return await super().load_one(kb_id, workspace_id)


@pytest.mark.anyio
async def test_overall_request_deadline_cancels_runtime_resolution() -> None:
    service = RetrievalService(
        runtime_loader=SlowLoader({ALLOWED_KB_ID: runtime()}),
        vectors=Registry(Store()),
        embeddings=Embeddings(),
        request_timeout_s=0.01,
    )

    with pytest.raises(UpstreamUnavailable):
        await service.query(principal(ALLOWED_KB_ID), request("fulltext"), request_id="req_test")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "store",
    [Store(payload_version=2), Store(score=math.nan), Store(score=math.inf)],
)
async def test_malformed_or_cross_namespace_hit_fails_target(store: Store) -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: runtime(index_version=1)}),
        vectors=Registry(store),
        embeddings=Embeddings(),
    )

    with pytest.raises(UpstreamUnavailable):
        await service.query(principal(ALLOWED_KB_ID), request("fulltext"), request_id="req_test")
