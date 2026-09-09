"""Embedding cache and cross-instance reuse with real Redis, without a GPU."""

from collections.abc import Sequence
from uuid import uuid4

import tiktoken
from redis.asyncio import Redis

from cairn.core.cache import RedisCache
from cairn.core.modelref import ModelRef
from cairn.embedding.base import Purpose
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import TiktokenTokenizer, TokenizerRegistry


class Provider:
    max_batch_size = 8

    def __init__(self) -> None:
        self.cache_namespace = uuid4().hex
        self.calls = 0

    async def embed(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
    ) -> list[list[float]]:
        self.calls += 1
        return [[len(text), 1, 2] for text in texts]


async def test_redis_bulk_cache_preserves_missing_slots_and_sets_ttl(redis_url: str) -> None:
    client = Redis.from_url(redis_url)
    cache = RedisCache(client)
    prefix = "test:embedding:" + uuid4().hex
    keys = [f"{prefix}:{index}" for index in range(3)]
    try:
        await cache.mset({keys[0]: b"\x00\xff", keys[2]: b"last"}, 60)
        assert await cache.mget(keys) == [b"\x00\xff", None, b"last"]
        assert await cache.mget([]) == []
        await cache.mset({}, 60)
        assert 0 < await client.ttl(keys[0]) <= 60
        assert 0 < await client.ttl(keys[2]) <= 60
    finally:
        await client.delete(*keys)
        await cache.close()


async def test_embedding_service_reuses_redis_vectors_across_instances(redis_url: str) -> None:
    provider = Provider()
    encoding = tiktoken.Encoding(
        name="test-byte",
        pat_str=r"(?s).",
        mergeable_ranks={bytes([value]): value for value in range(256)},
        special_tokens={},
    )
    tokenizers = TokenizerRegistry()
    tokenizers.register("test", TiktokenTokenizer(encoding))
    model = ModelRef(
        id=uuid4(),
        provider_family="tei",
        model_key="model",
        capability="embedding",
        dimension=3,
        max_input_tokens=128,
        tokenizer_id="test",
        normalize=False,
    )
    first_cache = RedisCache(Redis.from_url(redis_url))
    second_cache = RedisCache(Redis.from_url(redis_url))
    try:
        first = EmbeddingService(provider=provider, cache=first_cache, tokenizers=tokenizers)
        second = EmbeddingService(provider=provider, cache=second_cache, tokenizers=tokenizers)
        await first.embed_documents(model, ["one", "three"])
        result = await second.embed_documents(model, ["three", "two", "one", "three"])
        assert [vector.values[0] for vector in result] == [5, 3, 3, 5]
        assert provider.calls == 2
        assert (await first.embed_query(model, "one")).values == (3, 1, 2)
        assert (await second.embed_query(model, "one")).values == (3, 1, 2)
        assert provider.calls == 3
    finally:
        await first_cache.close()
        await second_cache.close()
