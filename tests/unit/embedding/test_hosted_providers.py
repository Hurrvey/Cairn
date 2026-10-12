"""Wire formats of the hosted and sidecar embedding families."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr

from cairn.core.errors import BlockedAddress
from cairn.core.modelref import ModelRef
from cairn.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingInvalidVector,
    EmbeddingProviderError,
    EmbeddingProviderRejected,
)
from cairn.embedding.providers import (
    BgeM3Provider,
    DashScopeProvider,
    OpenAICompatibleProvider,
    build_provider,
    parse_sparse,
)
from cairn.vectorstore.base import SparseVector


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"requests": [], "status": 200, "body": {}, "headers": {}}

    async def resolve(host: str, port: int):  # type: ignore[no-untyped-def]
        return [ipaddress.ip_address("10.0.0.5" if host == "sidecar" else "93.184.216.34")]

    async def send(self: object, request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)  # type: ignore[attr-defined]
        return httpx.Response(
            state["status"],  # type: ignore[arg-type]
            content=json.dumps(state["body"]).encode(),
            headers=state["headers"],  # type: ignore[arg-type]
            request=request,
        )

    monkeypatch.setattr("cairn.core.http._resolve", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return state


def sent(network: dict[str, object], index: int = 0) -> tuple[httpx.Request, dict[str, object]]:
    request = network["requests"][index]  # type: ignore[index]
    return request, json.loads(request.content)


async def test_dashscope_requests_dense_and_sparse_in_one_call(
    network: dict[str, object], model: ModelRef
) -> None:
    dash = replace(model, provider_family="dashscope", model_key="text-embedding-v4", sparse=True)
    network["body"] = {
        "output": {
            "embeddings": [
                {
                    "text_index": 1,
                    "embedding": [4, 5, 6],
                    "sparse_embedding": [{"index": 9, "value": 0.25, "token": "b"}],
                },
                {
                    "text_index": 0,
                    "embedding": [1, 2, 3],
                    "sparse_embedding": [
                        {"index": 7, "value": 0.5, "token": "a"},
                        {"index": 3, "value": 0.75, "token": "c"},
                    ],
                },
            ]
        },
        "usage": {"total_tokens": 4},
    }
    async with DashScopeProvider(
        base_url="https://dashscope.aliyuncs.com/api/v1",
        namespace="v1",
        api_key=SecretStr("sk-test"),
    ) as provider:
        output = await provider.embed_hybrid(
            dash, ["a c", "b"], purpose="query", dense=True, sparse=True
        )
    assert output.dense == [[1, 2, 3], [4, 5, 6]]
    assert output.sparse == [SparseVector((3, 7), (0.75, 0.5)), SparseVector((9,), (0.25,))]
    request, body = sent(network)
    assert request.url.path == "/api/v1/services/embeddings/text-embedding/text-embedding"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert body == {
        "model": "text-embedding-v4",
        "input": {"texts": ["a c", "b"]},
        "parameters": {"text_type": "query", "output_type": "dense&sparse"},
    }


async def test_dashscope_sparse_only_and_requested_dimension(
    network: dict[str, object], model: ModelRef
) -> None:
    dash = replace(model, provider_family="dashscope", sparse=True, send_dimension=True)
    network["body"] = {
        "output": {
            "embeddings": [{"text_index": 0, "sparse_embedding": [{"index": 1, "value": 1}]}]
        }
    }
    async with DashScopeProvider(
        base_url="https://dashscope.aliyuncs.com/api/v1", namespace="v1", api_key=SecretStr("k")
    ) as provider:
        output = await provider.embed_hybrid(
            dash, ["x"], purpose="document", dense=False, sparse=True
        )
    assert output.dense is None and output.sparse == [SparseVector((1,), (1.0,))]
    _request, body = sent(network)
    assert body["parameters"] == {
        "text_type": "document",
        "output_type": "sparse",
        "dimension": 3,
    }


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, EmbeddingProviderRejected),
        (401, EmbeddingProviderRejected),
        (429, EmbeddingProviderError),
    ],
)
async def test_dashscope_errors_are_classified(
    network: dict[str, object], model: ModelRef, status: int, error: type[Exception]
) -> None:
    network.update(status=status, body={"code": "InvalidApiKey", "message": "secret detail"})
    async with DashScopeProvider(
        base_url="https://dashscope.aliyuncs.com/api/v1", namespace="v1", api_key=SecretStr("k")
    ) as provider:
        with pytest.raises(error) as caught:
            await provider.embed(model, ["x"], purpose="query")
    assert "secret detail" not in str(caught.value)


async def test_openai_compatible_sends_dimensions_only_when_asked(
    network: dict[str, object], model: ModelRef
) -> None:
    network["body"] = {"data": [{"index": 0, "embedding": [1, 2, 3]}]}
    async with OpenAICompatibleProvider(
        base_url="https://ark.cn-beijing.volces.com/api/v3", namespace="v1", api_key=SecretStr("k")
    ) as provider:
        await provider.embed(model, ["x"], purpose="document")
        await provider.embed(replace(model, send_dimension=True), ["x"], purpose="document")
    request, first = sent(network, 0)
    assert request.url.path == "/api/v3/embeddings"
    assert first == {"input": ["x"], "model": "test-model", "encoding_format": "float"}
    assert sent(network, 1)[1]["dimensions"] == 3


async def test_bge_m3_sidecar_hybrid(network: dict[str, object], model: ModelRef) -> None:
    network["body"] = {
        "dense": [[1, 2, 3]],
        "sparse": [[{"index": 5, "value": 0.3}, {"index": 2, "value": 0.0}]],
    }
    async with BgeM3Provider(
        base_url="http://sidecar:8000", namespace="bge", allow_private=True
    ) as provider:
        output = await provider.embed_hybrid(
            replace(model, sparse=True), ["x"], purpose="document", dense=True, sparse=True
        )
        assert await provider.embed(model, ["x"], purpose="query") == [[1, 2, 3]]
    assert output.dense == [[1, 2, 3]]
    assert output.sparse is not None and output.sparse[0] is not None
    assert output.sparse[0].indices == (5,)
    request, body = sent(network)
    assert request.url.path == "/v1/encode"
    assert body == {"inputs": ["x"], "dense": True, "sparse": True}
    assert sent(network, 1)[1] == {"inputs": ["x"], "dense": True, "sparse": False}


def test_parse_sparse_canonicalises_and_bounds() -> None:
    parsed = parse_sparse(
        [{"index": 9, "value": 0.5}, {"index": 2, "value": 0.25}, {"index": 9, "value": 0.75}]
    )
    assert parsed == SparseVector((2, 9), (0.25, 0.75))
    assert parse_sparse([{"index": 1, "value": -1}]) is None
    for bad in (
        [{"index": -1, "value": 1}],
        [{"index": 2**32, "value": 1}],
        [{"index": True, "value": 1}],
        [{"index": 1, "value": "1"}],
        [{"index": 1, "value": float("inf")}],
        "not a list",
        [{"index": i, "value": 1} for i in range(16_385)],
    ):
        with pytest.raises(EmbeddingInvalidVector):
            parse_sparse(bad)


def test_factory_protects_hosted_families() -> None:
    with pytest.raises(EmbeddingConfigurationError):
        build_provider(
            "dashscope", base_url="https://dashscope.aliyuncs.com/api/v1", namespace="v1"
        )
    provider = build_provider(
        "volcengine",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        namespace="v1",
        api_key=SecretStr("k"),
        allow_private=True,
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._allow_private is False
    assert provider.max_batch_size == 16
    assert (
        build_provider(
            "dashscope", base_url="https://x.test", namespace="v1", api_key=SecretStr("k")
        ).max_batch_size
        == 10
    )
    with pytest.raises(EmbeddingConfigurationError):
        build_provider("unknown", base_url="https://x.test", namespace="v1")


async def test_hosted_family_never_reaches_a_private_address(
    network: dict[str, object], model: ModelRef
) -> None:
    provider = build_provider(
        "volcengine", base_url="http://sidecar:8000", namespace="v1", api_key=SecretStr("k")
    )
    async with provider:  # type: ignore[attr-defined]
        with pytest.raises(BlockedAddress):
            await provider.embed(model, ["x"], purpose="query")
    assert network["requests"] == []
