"""Hybrid (dense + learned sparse) embedding: grouping, caching and validation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from cairn.core.cache import InMemoryCache
from cairn.core.modelref import ModelRef
from cairn.embedding.base import HybridOutput, Purpose
from cairn.embedding.errors import EmbeddingConfigurationError, EmbeddingInvalidVector
from cairn.embedding.service import EmbeddingService, _pack_sparse, _unpack_sparse
from cairn.embedding.tokenizers import TokenizerRegistry
from cairn.vectorstore.base import SparseVector


class HybridProvider:
    cache_namespace = "sidecar:bge-m3"
    max_batch_size = 2

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool, bool]] = []
        self.sparse_response: list[SparseVector | None] | None = None

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        self.calls.append((list(texts), True, False))
        return [[float(len(text)), 1, 2] for text in texts]

    async def embed_hybrid(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        dense: bool,
        sparse: bool,
    ) -> HybridOutput:
        self.calls.append((list(texts), dense, sparse))
        return HybridOutput(
            dense=[[float(len(text)), 1, 2] for text in texts] if dense else None,
            sparse=self.sparse_response
            if self.sparse_response is not None
            else [SparseVector((len(text),), (1.0,)) for text in texts]
            if sparse
            else None,
        )


class DenseProvider:
    cache_namespace = "dense-only"
    max_batch_size = 4

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        return [[1.0, 1, 2] for _ in texts]


@pytest.fixture
def sparse_model(model: ModelRef) -> ModelRef:
    return replace(model, sparse=True, provider_family="bge_m3")


async def test_one_call_returns_both_outputs_and_both_are_cached(
    tokenizers: TokenizerRegistry, sparse_model: ModelRef
) -> None:
    provider = HybridProvider()
    service = EmbeddingService(provider=provider, cache=InMemoryCache(), tokenizers=tokenizers)

    dense, sparse = await service.embed_documents_hybrid(sparse_model, ["a", "bb", "a"])
    assert dense is not None and sparse is not None
    assert [vector.values[0] for vector in dense] == pytest.approx(
        [vector.values[0] for vector in (dense[0], dense[1], dense[0])]
    )
    assert sparse == [
        SparseVector((1,), (1.0,)),
        SparseVector((2,), (1.0,)),
        SparseVector((1,), (1.0,)),
    ]
    assert provider.calls == [(["a", "bb"], True, True)]

    again = await service.embed_documents_hybrid(sparse_model, ["bb", "a"])
    assert again[1] == [sparse[1], sparse[0]]
    assert len(provider.calls) == 1


async def test_only_missing_outputs_are_requested(
    tokenizers: TokenizerRegistry, sparse_model: ModelRef
) -> None:
    provider = HybridProvider()
    service = EmbeddingService(provider=provider, cache=InMemoryCache(), tokenizers=tokenizers)

    await service.embed_documents(sparse_model, ["cached-dense"])
    await service.embed_documents_hybrid(sparse_model, ["cached-dense", "new"])
    assert provider.calls[1:] == [
        (["cached-dense"], False, True),
        (["new"], True, True),
    ] or provider.calls[1:] == [(["new"], True, True), (["cached-dense"], False, True)]

    dense, sparse = await service.embed_query_hybrid(sparse_model, "query", dense=False)
    assert dense is None and sparse == SparseVector((5,), (1.0,))
    assert provider.calls[-1] == (["query"], False, True)


async def test_text_without_terms_is_cached_as_no_terms(
    tokenizers: TokenizerRegistry, sparse_model: ModelRef
) -> None:
    provider = HybridProvider()
    provider.sparse_response = [None]
    service = EmbeddingService(provider=provider, cache=InMemoryCache(), tokenizers=tokenizers)
    assert (await service.embed_documents_hybrid(sparse_model, ["?"], dense=False))[1] == [None]
    assert (await service.embed_documents_hybrid(sparse_model, ["?"], dense=False))[1] == [None]
    assert len(provider.calls) == 1


async def test_wrong_sparse_row_count_is_invalid(
    tokenizers: TokenizerRegistry, sparse_model: ModelRef
) -> None:
    provider = HybridProvider()
    provider.sparse_response = []
    service = EmbeddingService(provider=provider, cache=InMemoryCache(), tokenizers=tokenizers)
    with pytest.raises(EmbeddingInvalidVector):
        await service.embed_documents_hybrid(sparse_model, ["x"])


async def test_sparse_requires_a_sparse_model_and_provider(
    tokenizers: TokenizerRegistry, model: ModelRef, sparse_model: ModelRef
) -> None:
    hybrid = EmbeddingService(
        provider=HybridProvider(), cache=InMemoryCache(), tokenizers=tokenizers
    )
    with pytest.raises(EmbeddingConfigurationError):
        await hybrid.embed_documents_hybrid(model, ["x"])
    dense_only = EmbeddingService(
        provider=DenseProvider(), cache=InMemoryCache(), tokenizers=tokenizers
    )
    assert dense_only.supports_sparse is False
    with pytest.raises(EmbeddingConfigurationError):
        await dense_only.embed_documents_hybrid(sparse_model, ["x"])
    with pytest.raises(EmbeddingConfigurationError):
        await hybrid.embed_documents_hybrid(sparse_model, ["x"], dense=False, sparse=False)


def test_sparse_cache_encoding_round_trips_and_rejects_corruption() -> None:
    vector = SparseVector((1, 5, 9), (0.5, 0.25, 2.0))
    assert _unpack_sparse(_pack_sparse(vector)) == vector
    assert _unpack_sparse(_pack_sparse(None)) is None
    with pytest.raises(EmbeddingInvalidVector):
        _unpack_sparse(_pack_sparse(vector)[:-1])
    with pytest.raises(EmbeddingInvalidVector):
        _unpack_sparse(_pack_sparse(SparseVector((5, 1), (1.0, 1.0))))
