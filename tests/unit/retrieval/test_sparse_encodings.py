"""Query encoding across targets when sparse vectors come from a model."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from tests.unit.retrieval.test_service import (
    ALLOWED_KB_ID,
    MODEL_ID,
    PROVIDER_ID,
    Loader,
    Registry,
    Store,
    principal,
    request,
    runtime,
)

from cairn.core.errors import UpstreamUnavailable
from cairn.core.modelref import ModelRef
from cairn.core.retrieval_runtime import KnowledgeBaseRuntime
from cairn.core.sparse import SparseSpec
from cairn.retrieval.runtime import QueryEncoding
from cairn.retrieval.service import RetrievalService
from cairn.vectorstore.base import SparseVector

SPARSE_MODEL = ModelRef(
    id=UUID("40000000-0000-0000-0000-00000000000a"),
    provider_family="bge_m3",
    model_key="BAAI/bge-m3",
    capability="embedding",
    dimension=3,
    max_input_tokens=128,
    tokenizer_id="unit",
    provider_id=PROVIDER_ID,
    sparse=True,
)
TERMS = SparseVector((11, 42), (0.5, 0.25))


class Encoder:
    """Records every encoding request; can fail the sparse half."""

    def __init__(self, *, fail_sparse: bool = False) -> None:
        self.fail_sparse = fail_sparse
        self.dense_calls: list[ModelRef] = []
        self.encode_calls: list[tuple[ModelRef, bool, bool]] = []

    async def embed_query(
        self, model: ModelRef, query: str
    ) -> tuple[list[float], int, bool | None]:
        self.dense_calls.append(model)
        return [1.0, 1.0, 1.0], 2, False

    async def encode_query_for_workspace(
        self, workspace_id: UUID, model: ModelRef, query: str, *, dense: bool, sparse: bool
    ) -> QueryEncoding:
        self.encode_calls.append((model, dense, sparse))
        if self.fail_sparse:
            raise RuntimeError("sparse encoder down")
        return QueryEncoding(dense=[1.0, 1.0, 1.0] if dense else None, sparse=TERMS, tokens=3)


def sparse_runtime(model: ModelRef, **kwargs: object) -> KnowledgeBaseRuntime:
    base = runtime(**kwargs)  # type: ignore[arg-type]
    return base.model_copy(update={"sparse": SparseSpec(kind="model", model=model)})


async def test_one_model_call_serves_dense_and_sparse() -> None:
    own = replace(SPARSE_MODEL, id=MODEL_ID)
    kb = sparse_runtime(own).model_copy(update={"embedding_model": own})
    store = Store()
    encoder = Encoder()
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: kb}), vectors=Registry(store), embeddings=encoder
    )

    await service.query(principal(ALLOWED_KB_ID), request("hybrid"), request_id="req_test")

    assert encoder.encode_calls == [(own, True, True)]
    assert encoder.dense_calls == []
    queries = [query for _namespace, query in store.calls]
    assert any(query.sparse == TERMS for query in queries)
    assert any(query.dense == [1.0, 1.0, 1.0] for query in queries)


async def test_separate_sparse_model_is_encoded_on_its_own() -> None:
    kb = sparse_runtime(SPARSE_MODEL)
    store = Store()
    encoder = Encoder()
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: kb}), vectors=Registry(store), embeddings=encoder
    )

    await service.query(principal(ALLOWED_KB_ID), request("hybrid"), request_id="req_test")

    assert encoder.dense_calls == [kb.embedding_model]
    assert encoder.encode_calls == [(SPARSE_MODEL, False, True)]


async def test_hybrid_degrades_to_vector_search_when_the_sparse_encoder_fails() -> None:
    store = Store()
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: sparse_runtime(SPARSE_MODEL)}),
        vectors=Registry(store),
        embeddings=Encoder(fail_sparse=True),
    )

    response = await service.query(
        principal(ALLOWED_KB_ID), request("hybrid"), request_id="req_test"
    )

    assert [query.sparse for _namespace, query in store.calls] == [None]
    assert any(
        notice.stage == "sparse" and notice.reason == "unavailable" for notice in response.degraded
    )
    assert response.results


async def test_fulltext_fails_the_target_when_the_sparse_encoder_fails() -> None:
    service = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: sparse_runtime(SPARSE_MODEL)}),
        vectors=Registry(Store()),
        embeddings=Encoder(fail_sparse=True),
    )

    with pytest.raises(UpstreamUnavailable):
        await service.query(principal(ALLOWED_KB_ID), request("fulltext"), request_id="req_test")
