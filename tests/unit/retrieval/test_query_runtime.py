from __future__ import annotations

from dataclasses import replace
from typing import ClassVar
from uuid import UUID

import pytest

from cairn.core.cache import InMemoryCache
from cairn.core.config import RetrievalEmbeddingEndpointSettings, RetrievalSettings
from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import ProviderRuntimeProjection, provider_runtime_key
from cairn.retrieval.errors import RetrievalUnsupported
from cairn.retrieval.runtime import QueryEmbeddingRuntime

MODEL_ID = UUID("10000000-0000-0000-0000-000000000001")
PROVIDER_ID = UUID("20000000-0000-0000-0000-000000000002")
WORKSPACE_ID = UUID("30000000-0000-0000-0000-000000000003")


class Tokenizer:
    fingerprint = "unit-tokenizer-v1"

    def count(self, text: str) -> int:
        return len(text)

    def truncate(self, text: str, limit: int) -> str:
        return text[:limit]


class Provider:
    max_batch_size = 16
    cache_namespace = "unit-provider-v1"
    instances: ClassVar[list[Provider]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)

    async def __aenter__(self) -> Provider:
        return self

    async def close(self) -> None:
        self.closed = True

    async def embed(self, model: ModelRef, texts: list[str], *, purpose: str) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _text in texts]


def model() -> ModelRef:
    return ModelRef(
        id=MODEL_ID,
        provider_family="tei",
        model_key="unit",
        capability="embedding",
        dimension=3,
        max_input_tokens=10,
        query_prefix="Q: ",
        tokenizer_id="unit-tokenizer",
        provider_id=PROVIDER_ID,
    )


def settings() -> RetrievalSettings:
    return RetrievalSettings(
        embedding_endpoints={
            PROVIDER_ID: RetrievalEmbeddingEndpointSettings(
                dialect="tei",
                base_url="http://127.0.0.1:38742",
                allow_private=True,
                namespace="unit-v1",
                max_batch_size=16,
            )
        },
        tokenizer_files={"unit-tokenizer": "unit-tokenizer.json"},
    )


@pytest.mark.anyio
async def test_query_embedding_rejects_over_budget_prefixed_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cairn.retrieval.runtime.TeiProvider", Provider)
    runtime = QueryEmbeddingRuntime(
        settings(), cache=InMemoryCache(), tokenizers={"unit-tokenizer": Tokenizer()}
    )
    await runtime.start()

    with pytest.raises(RetrievalUnsupported, match="token"):
        await runtime.embed_query(model(), "123456789")

    await runtime.close()


@pytest.mark.anyio
async def test_query_embedding_startup_closes_providers_when_later_provider_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Provider.instances.clear()

    class FailingProvider(Provider):
        async def __aenter__(self) -> FailingProvider:
            if str(self.kwargs.get("namespace")).endswith(":fail-v1"):
                raise RuntimeError("startup failure")
            return self

    monkeypatch.setattr("cairn.retrieval.runtime.TeiProvider", FailingProvider)
    second_provider = UUID("30000000-0000-0000-0000-000000000003")
    config = settings()
    config.embedding_endpoints[second_provider] = config.embedding_endpoints[
        PROVIDER_ID
    ].model_copy(update={"namespace": "fail-v1"})

    with pytest.raises(RuntimeError, match="startup failure"):
        await QueryEmbeddingRuntime(
            config, cache=InMemoryCache(), tokenizers={"unit-tokenizer": Tokenizer()}
        ).start()

    assert Provider.instances and all(instance.closed for instance in Provider.instances)


@pytest.mark.anyio
async def test_legacy_model_without_provider_identity_cannot_use_query_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cairn.retrieval.runtime.TeiProvider", Provider)
    runtime = QueryEmbeddingRuntime(
        settings(), cache=InMemoryCache(), tokenizers={"unit-tokenizer": Tokenizer()}
    )
    await runtime.start()

    with pytest.raises(RetrievalUnsupported, match="provider"):
        await runtime.embed_query(replace(model(), provider_id=None), "Saturn")

    await runtime.close()


@pytest.mark.anyio
async def test_provider_cache_namespace_contains_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Provider.instances.clear()
    monkeypatch.setattr("cairn.retrieval.runtime.TeiProvider", Provider)
    first = QueryEmbeddingRuntime(
        settings(), cache=InMemoryCache(), tokenizers={"unit-tokenizer": Tokenizer()}
    )
    await first.start()

    assert str(PROVIDER_ID) in str(Provider.instances[0].kwargs["namespace"])
    await first.close()


@pytest.mark.anyio
async def test_provider_dialect_must_match_immutable_model_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cairn.retrieval.runtime.TeiProvider", Provider)
    runtime = QueryEmbeddingRuntime(
        settings(), cache=InMemoryCache(), tokenizers={"unit-tokenizer": Tokenizer()}
    )
    await runtime.start()

    with pytest.raises(RetrievalUnsupported, match="dialect"):
        await runtime.embed_query(replace(model(), provider_family="infinity"), "Saturn")

    await runtime.close()


@pytest.mark.anyio
async def test_dynamic_provider_projection_is_workspace_bound_and_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Provider.instances.clear()
    monkeypatch.setattr("cairn.retrieval.runtime.TeiProvider", Provider)
    cache = InMemoryCache()
    projection = ProviderRuntimeProjection(
        id=PROVIDER_ID,
        workspace_id=WORKSPACE_ID,
        family="tei",
        base_url="http://127.0.0.1:38742",
        allow_private=True,
        binding_revision="dynamic-v1",
        max_batch_size=8,
    )
    await cache.set(
        provider_runtime_key(PROVIDER_ID), projection.model_dump_json().encode(), ttl=300
    )
    runtime = QueryEmbeddingRuntime(
        RetrievalSettings(),
        cache=cache,
        tokenizers={"unit-tokenizer": Tokenizer()},
    )
    await runtime.start()

    vector, tokens, _cached = await runtime.embed_query(
        model(), "Saturn", workspace_id=WORKSPACE_ID
    )
    assert tuple(vector) == (1.0, 0.0, 0.0)
    assert tokens == len("Q: Saturn")
    assert Provider.instances[0].kwargs["namespace"] == f"{PROVIDER_ID}:dynamic-v1"

    with pytest.raises(RetrievalUnsupported, match="workspace"):
        await runtime.embed_query(model(), "Saturn", workspace_id=UUID(int=1))

    changed = projection.model_copy(update={"binding_revision": "dynamic-v2"})
    await cache.set(provider_runtime_key(PROVIDER_ID), changed.model_dump_json().encode(), ttl=300)
    await runtime.embed_query(model(), "Saturn", workspace_id=WORKSPACE_ID)

    assert len(Provider.instances) == 2
    assert not Provider.instances[0].closed
    await runtime.close()
    assert all(provider.closed for provider in Provider.instances)


@pytest.mark.anyio
async def test_configured_embedding_deadlines_reach_query_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cairn.core.config import EmbeddingSettings

    monkeypatch.setattr("cairn.retrieval.runtime.TeiProvider", Provider)
    runtime = QueryEmbeddingRuntime(
        settings(),
        cache=InMemoryCache(),
        tokenizers={"unit-tokenizer": Tokenizer()},
        embedding=EmbeddingSettings(query_timeout_s=9),
    )
    await runtime.start()
    service = runtime._services[PROVIDER_ID]
    assert service._settings.query_timeout_s == 9
    await runtime.close()
