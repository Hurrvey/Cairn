"""The bge-m3 sidecar, live, through Cairn's provider and embedding service.

Skipped unless ``CAIRN_TEST_BGE_M3_URL`` points at a running sidecar and
``CAIRN_TEST_BGE_M3_TOKENIZER`` at bge-m3's ``tokenizer.json``.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from cairn.core.cache import InMemoryCache
from cairn.core.modelref import ModelRef
from cairn.embedding.providers import BgeM3Provider
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import HuggingFaceTokenizer, TokenizerRegistry

URL = os.environ.get("CAIRN_TEST_BGE_M3_URL")
TOKENIZER = os.environ.get("CAIRN_TEST_BGE_M3_TOKENIZER")

pytestmark = pytest.mark.skipif(
    not URL or not TOKENIZER, reason="set CAIRN_TEST_BGE_M3_URL and CAIRN_TEST_BGE_M3_TOKENIZER"
)

MODEL = ModelRef(
    id=uuid4(),
    provider_family="bge_m3",
    model_key="BAAI/bge-m3",
    capability="embedding",
    dimension=1024,
    max_input_tokens=8192,
    optimal_batch_size=16,
    tokenizer_id="bge-m3",
    sparse=True,
)


@pytest.fixture
async def service():  # type: ignore[no-untyped-def]
    assert URL and TOKENIZER
    tokenizers = TokenizerRegistry()
    tokenizers.register("bge-m3", HuggingFaceTokenizer.from_file(Path(TOKENIZER)))
    async with BgeM3Provider(base_url=URL, namespace="live", allow_private=True) as provider:
        yield EmbeddingService(provider=provider, cache=InMemoryCache(), tokenizers=tokenizers)


async def test_dense_and_learned_sparse_in_one_pass(service: EmbeddingService) -> None:
    documents = [
        "土星环主要由冰和岩石组成, 从地球上用小型望远镜即可看到。",
        "The XR-2200 controller requires firmware 4.1 before installation.",
        "Mars is called the red planet because of iron oxide on its surface.",
    ]
    dense, sparse = await service.embed_documents_hybrid(MODEL, documents)
    assert dense is not None and sparse is not None
    assert all(vector.dim == 1024 for vector in dense)
    assert all(weights is not None and weights.indices for weights in sparse)

    query_dense, query_sparse = await service.embed_query_hybrid(MODEL, "土星的光环是什么做的?")
    assert query_dense is not None and query_sparse is not None
    similarities = [
        sum(a * b for a, b in zip(query_dense.values, vector.values, strict=True))
        for vector in dense
    ]
    assert similarities.index(max(similarities)) == 0

    def overlap(document: int) -> float:
        stored = sparse[document]
        assert stored is not None
        weights = dict(zip(stored.indices, stored.values, strict=True))
        return sum(
            value * weights.get(index, 0.0)
            for index, value in zip(query_sparse.indices, query_sparse.values, strict=True)
        )

    # Learned lexical weights match the Chinese document and nothing else.
    assert overlap(0) > 0 and overlap(1) == 0 and overlap(2) == 0


async def test_product_code_is_a_lexical_match(service: EmbeddingService) -> None:
    _dense, sparse = await service.embed_documents_hybrid(
        MODEL,
        ["The XR-2200 controller requires firmware 4.1.", "General hardware maintenance notes."],
        dense=False,
    )
    _query_dense, query = await service.embed_query_hybrid(MODEL, "XR-2200", dense=False)
    assert sparse is not None and query is not None
    code_terms = set(sparse[0].indices) if sparse[0] else set()
    other_terms = set(sparse[1].indices) if sparse[1] else set()
    assert set(query.indices) & code_terms
    assert not set(query.indices) & other_terms
