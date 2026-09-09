import asyncio
import math
from collections.abc import Sequence
from dataclasses import replace

import pytest

from cairn.core.cache import InMemoryCache
from cairn.core.config import EmbeddingSettings
from cairn.core.modelref import ModelRef
from cairn.embedding.base import Purpose
from cairn.embedding.errors import (
    EmbeddingCircuitOpen,
    EmbeddingConfigurationError,
    EmbeddingDimensionMismatch,
    EmbeddingInvalidVector,
    EmbeddingProviderError,
    EmbeddingProviderRejected,
    EmbeddingTimeout,
)
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import TokenizerRegistry


class Provider:
    cache_namespace = "account-a:deployment-1"
    max_batch_size = 2

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Purpose]] = []
        self.failures: list[Exception] = []
        self.response: list[list[float]] | None = None
        self.active = 0
        self.peak = 0
        self.delay = 0.0
        self.started = asyncio.Event()

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        self.calls.append((list(texts), purpose))
        self.active += 1
        self.started.set()
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.failures:
                raise self.failures.pop(0)
            return (
                self.response
                if self.response is not None
                else [[float(len(text.encode())), 1, 2] for text in texts]
            )
        finally:
            self.active -= 1


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def cache() -> InMemoryCache:
    return InMemoryCache()


@pytest.fixture
def service(
    provider: Provider, cache: InMemoryCache, tokenizers: TokenizerRegistry
) -> EmbeddingService:
    return EmbeddingService(provider=provider, cache=cache, tokenizers=tokenizers)


async def test_unicode_and_whitespace_variants_hit_query_cache(
    service: EmbeddingService,
    provider: Provider,
    model: ModelRef,
) -> None:
    first = await service.embed_query(model, "  \uff28ello\t\nworld  ")
    second = await service.embed_query(model, "Hello world")
    assert first == second
    assert first.dim == 3
    assert math.hypot(*first.values) == pytest.approx(1, abs=1e-6)
    assert provider.calls == [(["Hello world"], "query")]


async def test_purpose_separation_even_without_prefix(
    service: EmbeddingService,
    provider: Provider,
    model: ModelRef,
) -> None:
    await service.embed_query(model, "same")
    await service.embed_documents(model, ["same"])
    assert [purpose for _, purpose in provider.calls] == ["query", "document"]


async def test_query_prefix_and_config_changes_do_not_reuse_stale_cache(
    service: EmbeddingService,
    provider: Provider,
    model: ModelRef,
) -> None:
    await service.embed_query(model, "hello")
    await service.embed_query(replace(model, query_prefix="query: "), "hello")
    await service.embed_query(replace(model, normalize=False), "hello")
    await service.embed_query(replace(model, model_key="new-revision"), "hello")
    assert len(provider.calls) == 4
    assert provider.calls[1][0] == ["query: hello"]


async def test_provider_accounts_are_separate_cache_namespaces(
    service: EmbeddingService,
    provider: Provider,
    cache: InMemoryCache,
    tokenizers: TokenizerRegistry,
    model: ModelRef,
) -> None:
    await service.embed_query(model, "same")
    other = Provider()
    other.cache_namespace = "account-b:deployment-1"
    separate = EmbeddingService(provider=other, cache=cache, tokenizers=tokenizers)
    await separate.embed_query(model, "same")
    assert len(other.calls) == len(provider.calls) == 1


async def test_partial_hits_batch_limits_deduplication_and_order(
    service: EmbeddingService,
    provider: Provider,
    unnormalized_model: ModelRef,
) -> None:
    model = unnormalized_model
    await service.embed_documents(model, ["bb"])
    provider.calls.clear()
    result = await service.embed_documents(model, ["a", "bb", "ccc", "a", "dddd"], batch_size=100)
    assert [vector.values[0] for vector in result] == [1, 2, 3, 1, 4]
    assert provider.calls == [(["a", "ccc"], "document"), (["dddd"], "document")]
    assert await service.embed_documents(model, []) == []


async def test_token_budget_includes_prefix_and_truncation_precedes_cache_key(
    service: EmbeddingService,
    provider: Provider,
    model: ModelRef,
) -> None:
    short = replace(model, max_input_tokens=8, query_prefix="q: ")
    await service.embed_query(short, "你好世界")
    assert provider.calls == [(["q: 你"], "query")]
    await service.embed_query(short, "你好另一个结尾")
    assert len(provider.calls) == 1
    assert service.count_tokens(model, "你好") == 6
    assert service.truncate_to_tokens(model, "你好", 3) == "你"


@pytest.mark.parametrize("text", ["", " \n ", "\ud800"])
async def test_invalid_input_does_not_reach_provider(
    service: EmbeddingService,
    provider: Provider,
    model: ModelRef,
    text: str,
) -> None:
    with pytest.raises(EmbeddingConfigurationError):
        await service.embed_query(model, text)
    assert provider.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"dimension": None},
        {"dimension": 0},
        {"max_input_tokens": None},
        {"max_input_tokens": 0},
        {"capability": "chat"},
        {"optimal_batch_size": 0},
        {"max_input_tokens": 2, "query_prefix": "query: "},
    ],
)
async def test_invalid_model_config_is_terminal(
    service: EmbeddingService,
    provider: Provider,
    model: ModelRef,
    changes: dict[str, object],
) -> None:
    with pytest.raises(EmbeddingConfigurationError):
        await service.embed_query(replace(model, **changes), "hello")
    assert provider.calls == []


async def test_invalid_batch_size_and_string_instead_of_sequence(
    service: EmbeddingService,
    model: ModelRef,
) -> None:
    with pytest.raises(EmbeddingConfigurationError):
        await service.embed_documents(model, ["ok"], batch_size=0)
    with pytest.raises(EmbeddingConfigurationError):
        await service.embed_documents(model, "not a list")


async def test_cache_corruption_is_recomputed(
    service: EmbeddingService,
    cache: InMemoryCache,
    provider: Provider,
    model: ModelRef,
) -> None:
    expected = await service.embed_query(model, "text")
    key = next(iter(cache._data))
    await cache.set(key, b"invalid", 60)
    assert await service.embed_query(model, "text") == expected
    assert len(provider.calls) == 2


async def test_cache_outage_does_not_hide_provider_success(
    provider: Provider,
    tokenizers: TokenizerRegistry,
    model: ModelRef,
) -> None:
    class BrokenCache(InMemoryCache):
        async def get(self, key: str) -> bytes | None:
            raise ConnectionError("cache down")

        async def set(self, key: str, value: bytes, ttl: int) -> None:
            raise ConnectionError("cache down")

    service = EmbeddingService(provider=provider, cache=BrokenCache(), tokenizers=tokenizers)
    assert (await service.embed_query(model, "text")).dim == 3


@pytest.mark.parametrize(
    "response,error",
    [
        ([[1, 2]], EmbeddingDimensionMismatch),
        ([[float("nan"), 0, 0]], EmbeddingInvalidVector),
        ([], EmbeddingInvalidVector),
        ([[1, 0, 0], [1, 0, 0]], EmbeddingInvalidVector),
    ],
)
async def test_invalid_response_is_not_retried_or_cached(
    service: EmbeddingService,
    provider: Provider,
    cache: InMemoryCache,
    model: ModelRef,
    response: list[list[float]],
    error: type[Exception],
) -> None:
    provider.response = response
    with pytest.raises(error):
        await service.embed_query(model, "hello")
    assert len(provider.calls) == 1
    assert not cache._data


async def test_retries_respect_retry_after(
    provider: Provider,
    tokenizers: TokenizerRegistry,
    cache: InMemoryCache,
    model: ModelRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("cairn.embedding.service._sleep", sleep)
    provider.failures = [EmbeddingProviderError(retry_after=0.1), EmbeddingProviderError()]
    service = EmbeddingService(provider=provider, cache=cache, tokenizers=tokenizers)
    assert (await service.embed_query(model, "text")).dim == 3
    assert len(provider.calls) == 3
    assert sleeps[0] >= 0.1


async def test_rejected_request_is_terminal(
    service: EmbeddingService,
    provider: Provider,
    model: ModelRef,
) -> None:
    provider.failures = [EmbeddingProviderRejected()]
    with pytest.raises(EmbeddingProviderRejected):
        await service.embed_query(model, "text")
    assert len(provider.calls) == 1


async def test_concurrency_is_bounded_and_timeout_includes_waiting_for_slot(
    provider: Provider,
    cache: InMemoryCache,
    tokenizers: TokenizerRegistry,
    model: ModelRef,
) -> None:
    provider.delay = 0.025
    service = EmbeddingService(
        provider=provider,
        cache=cache,
        tokenizers=tokenizers,
        settings=EmbeddingSettings(max_concurrency=1, query_timeout_s=0.04),
    )
    results = await asyncio.gather(
        *(service.embed_query(model, f"q{i}") for i in range(3)),
        return_exceptions=True,
    )
    assert provider.peak == 1
    assert sum(isinstance(result, EmbeddingTimeout) for result in results) >= 1
    assert provider.active == 0


async def test_circuit_opens_then_allows_a_recovery_probe(
    provider: Provider,
    cache: InMemoryCache,
    tokenizers: TokenizerRegistry,
    model: ModelRef,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr("cairn.embedding.service._monotonic", lambda: now[0])
    service = EmbeddingService(
        provider=provider,
        cache=cache,
        tokenizers=tokenizers,
        settings=EmbeddingSettings(max_retries=0, circuit_failure_threshold=2),
    )
    provider.failures = [EmbeddingProviderError(), EmbeddingProviderError()]
    for _ in range(2):
        with pytest.raises(EmbeddingProviderError):
            await service.embed_query(model, "text")
    with pytest.raises(EmbeddingCircuitOpen):
        await service.embed_query(model, "text")
    assert len(provider.calls) == 2
    now[0] = 31
    assert (await service.embed_query(model, "text")).dim == 3


async def test_cancellation_releases_provider_slot(
    service: EmbeddingService,
    provider: Provider,
    model: ModelRef,
) -> None:
    provider.delay = 10
    task = asyncio.create_task(service.embed_query(model, "text"))
    await provider.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.active == 0
    provider.delay = 0
    assert (await service.embed_query(model, "text")).dim == 3


async def test_document_cache_uses_bulk_operations(
    provider: Provider,
    tokenizers: TokenizerRegistry,
    model: ModelRef,
) -> None:
    class CountingCache(InMemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0
            self.writes = 0

        async def mget(self, keys: Sequence[str]) -> list[bytes | None]:
            self.reads += 1
            return await super().mget(keys)

        async def mset(self, values: dict[str, bytes], ttl: int) -> None:
            self.writes += 1
            await super().mset(values, ttl)

    cache = CountingCache()
    provider.max_batch_size = 100
    service = EmbeddingService(provider=provider, cache=cache, tokenizers=tokenizers)
    model = replace(model, optimal_batch_size=100)
    texts = [f"input {i}" for i in range(30)]
    assert len(await service.embed_documents(model, texts)) == 30
    assert cache.reads == cache.writes == 1
    await service.embed_documents(model, texts)
    assert cache.reads == 2
    assert len(provider.calls) == 1
