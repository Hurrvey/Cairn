import ipaddress
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

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
from cairn.embedding.providers import InfinityProvider, TeiProvider


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"requests": [], "status": 200, "body": [[1, 2, 3]], "headers": {}}

    async def resolve(host: str, port: int):
        return [ipaddress.ip_address("127.0.0.1" if host == "internal" else "93.184.216.34")]

    async def send(self: object, request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        body = state["body"]
        return httpx.Response(
            state["status"],
            content=body if isinstance(body, bytes) else json.dumps(body).encode(),
            headers=state["headers"],
            request=request,
        )

    monkeypatch.setattr("cairn.core.http._resolve", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    return state


async def test_tei_request_uses_prepared_inputs_without_server_truncation(
    network: dict[str, object],
    model: ModelRef,
) -> None:
    async with TeiProvider(base_url="https://public.test", namespace="binding-a") as provider:
        result = await provider.embed(model, ["prepared"], purpose="query")
    assert result == [[1, 2, 3]]
    request = network["requests"][0]
    assert request.url.path == "/embed"
    assert request.headers["Host"] == "public.test"
    assert request.url.host == "public.test"
    assert json.loads(request.content) == {
        "inputs": ["prepared"],
        "truncate": False,
        "normalize": False,
    }


async def test_infinity_reorders_explicit_indices_and_sends_model(
    network: dict[str, object],
    model: ModelRef,
) -> None:
    network["body"] = {
        "data": [{"index": 1, "embedding": [4, 5, 6]}, {"index": 0, "embedding": [1, 2, 3]}]
    }
    async with InfinityProvider(
        base_url="https://public.test/v1",
        namespace="binding-a",
        api_key=SecretStr("secret-key"),
    ) as provider:
        result = await provider.embed(model, ["a", "b"], purpose="document")
        assert "secret-key" not in repr(provider)
    assert result == [[1, 2, 3], [4, 5, 6]]
    request = network["requests"][0]
    assert request.url.path == "/v1/embeddings"
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert json.loads(request.content) == {
        "input": ["a", "b"],
        "model": "test-model",
        "encoding_format": "float",
    }


@pytest.mark.parametrize("indices", [[0, 0], [-1, 0], [0, 2], [False, 1]])
async def test_infinity_refuses_duplicate_missing_or_invalid_indices(
    network: dict[str, object],
    model: ModelRef,
    indices: list[int],
) -> None:
    network["body"] = {"data": [{"index": i, "embedding": [1, 2, 3]} for i in indices]}
    async with InfinityProvider(base_url="https://public.test", namespace="binding-a") as provider:
        with pytest.raises(EmbeddingInvalidVector):
            await provider.embed(model, ["a", "b"], purpose="document")


@pytest.mark.parametrize(
    "body",
    [b"not json", {"error": "private body"}, [["1", 2, 3]], [[True, 2, 3]], [[1, 2, float("nan")]]],
)
async def test_invalid_tei_response_does_not_leak_payload(
    network: dict[str, object],
    model: ModelRef,
    body: object,
) -> None:
    network["body"] = body
    async with TeiProvider(base_url="https://public.test", namespace="binding-a") as provider:
        with pytest.raises(EmbeddingInvalidVector) as caught:
            await provider.embed(model, ["a"], purpose="document")
    assert "private body" not in str(caught.value)


@pytest.mark.parametrize(
    "status,error",
    [
        (401, EmbeddingProviderRejected),
        (422, EmbeddingProviderRejected),
        (429, EmbeddingProviderError),
        (503, EmbeddingProviderError),
    ],
)
async def test_http_error_classification_and_retry_after(
    network: dict[str, object],
    model: ModelRef,
    status: int,
    error: type[Exception],
) -> None:
    network.update(status=status, body=b"private error", headers={"Retry-After": "5"})
    async with TeiProvider(base_url="https://public.test", namespace="binding-a") as provider:
        with pytest.raises(error) as caught:
            await provider.embed(model, ["a"], purpose="query")
    assert "private error" not in str(caught.value)
    if isinstance(caught.value, EmbeddingProviderError):
        assert caught.value.retry_after == 5


async def test_http_date_retry_after(network: dict[str, object], model: ModelRef) -> None:
    network.update(
        status=429,
        headers={"Retry-After": format_datetime(datetime.now(UTC) + timedelta(seconds=30))},
    )
    async with TeiProvider(base_url="https://public.test", namespace="binding-a") as provider:
        with pytest.raises(EmbeddingProviderError) as caught:
            await provider.embed(model, ["a"], purpose="query")
    assert 28 <= caught.value.retry_after <= 30


async def test_private_endpoint_requires_explicit_binding_opt_in(
    network: dict[str, object],
    model: ModelRef,
) -> None:
    async with TeiProvider(base_url="http://internal:8080", namespace="binding-a") as provider:
        with pytest.raises(BlockedAddress):
            await provider.embed(model, ["a"], purpose="query")
    assert network["requests"] == []
    async with TeiProvider(
        base_url="http://internal:8080", namespace="binding-a", allow_private=True
    ) as provider:
        assert await provider.embed(model, ["a"], purpose="query") == [[1, 2, 3]]


async def test_redirects_are_not_followed_with_provider_credentials(
    network: dict[str, object],
    model: ModelRef,
) -> None:
    network.update(status=307, headers={"Location": "http://internal/private"})
    async with TeiProvider(base_url="https://public.test", namespace="binding-a") as provider:
        with pytest.raises(EmbeddingProviderRejected):
            await provider.embed(model, ["a"], purpose="query")
    assert len(network["requests"]) == 1


async def test_response_size_and_batch_size_are_bounded(
    network: dict[str, object],
    model: ModelRef,
) -> None:
    async with TeiProvider(
        base_url="https://public.test",
        namespace="binding-a",
        max_response_bytes=4,
        max_batch_size=1,
    ) as provider:
        with pytest.raises(EmbeddingConfigurationError):
            await provider.embed(model, ["a", "b"], purpose="document")
        assert network["requests"] == []
        with pytest.raises(EmbeddingInvalidVector):
            await provider.embed(model, ["a"], purpose="query")


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/model",
        "https://user:secret@public.test",
        "https://public.test?token=secret",
        "https://public.test/#fragment",
    ],
)
def test_invalid_provider_url(url: str) -> None:
    with pytest.raises(EmbeddingConfigurationError):
        TeiProvider(base_url=url, namespace="binding-a")
