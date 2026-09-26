"""Embedding provider adapters with pinned DNS, bounded responses and no redirects.

    async with build_provider("tei", base_url=url, namespace=revision) as provider:
        service = EmbeddingService(provider=provider, ...)

Keep the client open for the binding's lifetime to reuse its connection pool.
Private-network access is an explicit composition-time opt-in for trusted local
model servers; it must never be copied from an ordinary user request, and the
hosted vendor families never get it at all.

Wire formats (checked against the vendors' official SDK sources):

* ``tei`` — ``POST /embed`` ``{"inputs", "truncate": false, "normalize": false}``.
* ``infinity`` / ``openai_compatible`` / ``volcengine`` — OpenAI-style
  ``POST /embeddings`` ``{"model", "input", "encoding_format": "float"}``.
* ``bge_m3`` — the sidecar's ``POST /v1/encode`` ``{"inputs", "dense", "sparse"}``.
* ``dashscope`` — ``POST /services/embeddings/text-embedding/text-embedding``
  ``{"model", "input": {"texts"}, "parameters": {"text_type", "output_type",
  "dimension"}}``, answering ``output.embeddings[] = {text_index, embedding,
  sparse_embedding: [{index, value, token}]}``.
"""

from __future__ import annotations

import json
import math
import struct
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any, ClassVar, Self

import httpx
from pydantic import SecretStr

from cairn.core.errors import UpstreamUnavailable, ValidationFailed
from cairn.core.http import SafeTransport, read_capped
from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import FAMILY_BATCH_LIMITS, HOSTED_FAMILIES, SPARSE_FAMILIES
from cairn.embedding.base import HybridOutput, Purpose
from cairn.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingInvalidVector,
    EmbeddingProviderError,
    EmbeddingProviderRejected,
)
from cairn.vectorstore.base import SparseVector

__all__ = [
    "HOSTED_FAMILIES",
    "SPARSE_FAMILIES",
    "BgeM3Provider",
    "DashScopeProvider",
    "HttpEmbeddingProvider",
    "InfinityProvider",
    "OpenAICompatibleProvider",
    "TeiProvider",
    "build_provider",
    "parse_sparse",
]


#: A 8192-token chunk has far fewer distinct terms; more means a broken response.
MAX_SPARSE_TERMS = 16_384
_MAX_INDEX = 2**32


def _retry_after(header: str | None) -> float | None:
    if header is None:
        return None
    try:
        delay = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            delay = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0, delay) if math.isfinite(delay) else None


def _dense_rows(rows: object, expected: int) -> list[list[float]]:
    if not isinstance(rows, list) or len(rows) != expected:
        raise EmbeddingInvalidVector()
    vectors: list[list[float]] = []
    for row in rows:
        if not isinstance(row, list) or not row:
            raise EmbeddingInvalidVector()
        if any(type(value) not in (int, float) for value in row):
            raise EmbeddingInvalidVector()
        try:
            values = [float(value) for value in row]
        except OverflowError as exc:
            raise EmbeddingInvalidVector() from exc
        if not all(math.isfinite(value) for value in values):
            raise EmbeddingInvalidVector()
        vectors.append(values)
    return vectors


def _indexed(entries: object, expected: int, *, index_key: str) -> list[dict[str, Any]]:
    """Order ``[{index, ...}]`` entries by their explicit index, refusing gaps and repeats."""
    if not isinstance(entries, list):
        raise EmbeddingInvalidVector()
    indexed: dict[int, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise EmbeddingInvalidVector()
        index = entry.get(index_key)
        if type(index) is not int or index in indexed or not 0 <= index < expected:
            raise EmbeddingInvalidVector()
        indexed[index] = entry
    if len(indexed) != expected:
        raise EmbeddingInvalidVector()
    return [indexed[index] for index in range(expected)]


def parse_sparse(entries: object) -> SparseVector | None:
    """Validate ``[{index, value}]`` term weights into a canonical sparse vector.

    Weights are rounded to float32 — what the index stores — so a vector read
    back from the cache is identical to the one first returned. Non-positive
    weights carry no lexical evidence and are dropped; a repeated index keeps
    its largest weight. An empty result is ``None``: the text has no terms.
    """
    if not isinstance(entries, list) or len(entries) > MAX_SPARSE_TERMS:
        raise EmbeddingInvalidVector()
    weights: dict[int, float] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise EmbeddingInvalidVector()
        index, value = entry.get("index"), entry.get("value")
        if type(index) is not int or not 0 <= index < _MAX_INDEX:
            raise EmbeddingInvalidVector()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingInvalidVector()
        if not math.isfinite(value):
            raise EmbeddingInvalidVector()
        weight = struct.unpack("<f", struct.pack("<f", float(value)))[0]
        if weight > 0:
            weights[index] = max(weight, weights.get(index, 0.0))
    if not weights:
        return None
    ordered = sorted(weights.items())
    return SparseVector(
        indices=tuple(index for index, _ in ordered),
        values=tuple(value for _, value in ordered),
    )


class _HTTPProvider:
    #: Stable identity of the wire format; part of the cache namespace.
    dialect: ClassVar[str]
    path: ClassVar[str]
    default_batch_size: ClassVar[int] = 64

    def __init__(
        self,
        *,
        base_url: str,
        namespace: str,
        api_key: SecretStr | None = None,
        allow_private: bool = False,
        max_batch_size: int | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        max_batch_size = self.default_batch_size if max_batch_size is None else max_batch_size
        try:
            url = httpx.URL(base_url)
        except httpx.InvalidURL as exc:
            raise EmbeddingConfigurationError("Invalid embedding endpoint URL.") from exc
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or not namespace
            or max_batch_size <= 0
            or max_response_bytes <= 0
        ):
            raise EmbeddingConfigurationError("Invalid embedding endpoint configuration.")
        self._url = str(url).rstrip("/") + self.path
        self._allow_private = allow_private
        self._api_key = api_key
        self._max_response_bytes = max_response_bytes
        self.max_batch_size = max_batch_size
        self.cache_namespace = sha256(
            json.dumps([namespace, str(url), self.dialect]).encode()
        ).hexdigest()
        self._client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid; never shows the key
        return f"{type(self).__name__}(url={self._url!r})"

    async def __aenter__(self) -> Self:
        if self._client is not None:
            raise RuntimeError("provider is already open")
        headers = {"User-Agent": "Cairn/0.1"}
        if self._api_key is not None:
            headers["Authorization"] = "Bearer " + self._api_key.get_secret_value()
        self._client = httpx.AsyncClient(
            transport=SafeTransport(allow_private=self._allow_private, retries=0),
            headers=headers,
            timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        raise NotImplementedError

    def _check_batch(self, texts: Sequence[str]) -> None:
        if self._client is None:
            raise EmbeddingConfigurationError("The provider must be opened at startup.")
        if isinstance(texts, (str, bytes)) or not 0 < len(texts) <= self.max_batch_size:
            raise EmbeddingConfigurationError("Provider batch size is invalid.")

    async def _post(self, payload: dict[str, object]) -> object:
        assert self._client is not None
        try:
            async with self._client.stream("POST", self._url, json=payload) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise EmbeddingProviderError(
                        retry_after=_retry_after(response.headers.get("Retry-After"))
                    )
                if not 200 <= response.status_code < 300:
                    raise EmbeddingProviderRejected()
                try:
                    raw = await read_capped(response, self._max_response_bytes)
                except ValidationFailed as exc:
                    raise EmbeddingInvalidVector() from exc
        except (httpx.TransportError, UpstreamUnavailable) as exc:
            raise EmbeddingProviderError() from exc
        try:
            return json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise EmbeddingInvalidVector() from exc


#: Public name for the adapters' shared base: an opened client plus ``embed``.
HttpEmbeddingProvider = _HTTPProvider


class TeiProvider(_HTTPProvider):
    dialect = "tei"
    path = "/embed"

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        self._check_batch(texts)
        body = await self._post({"inputs": list(texts), "truncate": False, "normalize": False})
        return _dense_rows(body, len(texts))


class _OpenAIStyleProvider(_HTTPProvider):
    path = "/embeddings"
    #: Whether this family understands the OpenAI ``dimensions`` parameter.
    sends_dimensions: ClassVar[bool] = False

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        self._check_batch(texts)
        payload: dict[str, object] = {
            "input": list(texts),
            "model": model.model_key,
            "encoding_format": "float",
        }
        if self.sends_dimensions and model.send_dimension and model.dimension:
            payload["dimensions"] = model.dimension
        body = await self._post(payload)
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise EmbeddingInvalidVector()
        entries = _indexed(body["data"], len(texts), index_key="index")
        return _dense_rows([entry.get("embedding") for entry in entries], len(texts))


class InfinityProvider(_OpenAIStyleProvider):
    dialect = "infinity"


class OpenAICompatibleProvider(_OpenAIStyleProvider):
    """OpenAI, Volcengine Ark, Zhipu, SiliconFlow, vLLM, Ollama and friends."""

    dialect = "openai"
    sends_dimensions = True
    default_batch_size = 16


class BgeM3Provider(_HTTPProvider):
    """The bundled bge-m3 sidecar (``apps/bge_m3``): dense and lexical weights in one pass."""

    dialect = "bge_m3"
    path = "/v1/encode"
    default_batch_size = 16

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        output = await self.embed_hybrid(model, texts, purpose=purpose, dense=True, sparse=False)
        assert output.dense is not None
        return output.dense

    async def embed_hybrid(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        dense: bool,
        sparse: bool,
    ) -> HybridOutput:
        self._check_batch(texts)
        if not dense and not sparse:
            raise EmbeddingConfigurationError("Request at least one embedding output.")
        body = await self._post({"inputs": list(texts), "dense": dense, "sparse": sparse})
        if not isinstance(body, dict):
            raise EmbeddingInvalidVector()
        dense_rows = _dense_rows(body.get("dense"), len(texts)) if dense else None
        sparse_rows: list[SparseVector | None] | None = None
        if sparse:
            raw = body.get("sparse")
            if not isinstance(raw, list) or len(raw) != len(texts):
                raise EmbeddingInvalidVector()
            sparse_rows = [parse_sparse(row) for row in raw]
        return HybridOutput(dense=dense_rows, sparse=sparse_rows)


class DashScopeProvider(_HTTPProvider):
    """Alibaba Cloud Model Studio (DashScope) native text embedding API."""

    dialect = "dashscope"
    path = "/services/embeddings/text-embedding/text-embedding"
    #: The v3/v4 limit per request.
    default_batch_size = 10

    async def embed(
        self, model: ModelRef, texts: Sequence[str], *, purpose: Purpose
    ) -> list[list[float]]:
        output = await self.embed_hybrid(model, texts, purpose=purpose, dense=True, sparse=False)
        assert output.dense is not None
        return output.dense

    async def embed_hybrid(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        dense: bool,
        sparse: bool,
    ) -> HybridOutput:
        self._check_batch(texts)
        if not dense and not sparse:
            raise EmbeddingConfigurationError("Request at least one embedding output.")
        parameters: dict[str, object] = {
            # Asymmetric retrieval: queries and passages are encoded differently.
            "text_type": purpose,
            "output_type": "dense&sparse" if dense and sparse else "dense" if dense else "sparse",
        }
        if model.send_dimension and model.dimension:
            parameters["dimension"] = model.dimension
        body = await self._post(
            {"model": model.model_key, "input": {"texts": list(texts)}, "parameters": parameters}
        )
        output = body.get("output") if isinstance(body, dict) else None
        if not isinstance(output, dict):
            raise EmbeddingInvalidVector()
        entries = _indexed(output.get("embeddings"), len(texts), index_key="text_index")
        dense_rows = (
            _dense_rows([entry.get("embedding") for entry in entries], len(texts))
            if dense
            else None
        )
        sparse_rows = (
            [parse_sparse(entry.get("sparse_embedding")) for entry in entries] if sparse else None
        )
        return HybridOutput(dense=dense_rows, sparse=sparse_rows)


_FAMILIES: dict[str, type[_HTTPProvider]] = {
    "tei": TeiProvider,
    "infinity": InfinityProvider,
    "bge_m3": BgeM3Provider,
    "dashscope": DashScopeProvider,
    "volcengine": OpenAICompatibleProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def build_provider(
    family: str,
    *,
    base_url: str,
    namespace: str,
    api_key: SecretStr | None = None,
    allow_private: bool = False,
    max_batch_size: int | None = None,
) -> HttpEmbeddingProvider:
    """The one place a provider family becomes an adapter."""
    provider_type = _FAMILIES.get(family)
    if provider_type is None:
        raise EmbeddingConfigurationError(f"Embedding provider family {family!r} has no adapter.")
    if family in HOSTED_FAMILIES:
        if api_key is None:
            raise EmbeddingConfigurationError(f"The {family} provider requires an API key.")
        allow_private = False
    limit = FAMILY_BATCH_LIMITS.get(family)
    if limit is not None:
        max_batch_size = min(max_batch_size or limit, limit)
    return provider_type(
        base_url=base_url,
        namespace=namespace,
        api_key=api_key,
        allow_private=allow_private,
        max_batch_size=max_batch_size,
    )
