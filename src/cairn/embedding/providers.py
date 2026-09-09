"""TEI and Infinity adapters with pinned DNS, bounded responses and no redirects.

    async with TeiProvider(base_url=url, namespace=binding_revision) as provider:
        service = EmbeddingService(provider=provider, ...)

Keep the client open for the binding's lifetime to reuse its connection pool.
Private-network access is an explicit composition-time opt-in for trusted local
model servers; it must never be copied from an ordinary user request.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Literal, Self

import httpx
from pydantic import SecretStr

from cairn.core.errors import UpstreamUnavailable, ValidationFailed
from cairn.core.http import SafeTransport, read_capped
from cairn.core.modelref import ModelRef
from cairn.embedding.base import Purpose
from cairn.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingInvalidVector,
    EmbeddingProviderError,
    EmbeddingProviderRejected,
)


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


class _HTTPProvider:
    def __init__(
        self,
        *,
        base_url: str,
        namespace: str,
        dialect: Literal["tei", "infinity"],
        api_key: SecretStr | None = None,
        allow_private: bool = False,
        max_batch_size: int = 64,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
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
        self._url = str(url).rstrip("/") + ("/embed" if dialect == "tei" else "/embeddings")
        self._dialect = dialect
        self._allow_private = allow_private
        self._api_key = api_key
        self._max_response_bytes = max_response_bytes
        self.max_batch_size = max_batch_size
        self.cache_namespace = sha256(
            json.dumps([namespace, str(url), dialect]).encode()
        ).hexdigest()
        self._client: httpx.AsyncClient | None = None

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
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
    ) -> list[list[float]]:
        if self._client is None:
            raise EmbeddingConfigurationError("The provider must be opened at startup.")
        if isinstance(texts, (str, bytes)) or not 0 < len(texts) <= self.max_batch_size:
            raise EmbeddingConfigurationError("Provider batch size is invalid.")
        payload: dict[str, object]
        if self._dialect == "tei":
            payload = {"inputs": list(texts), "truncate": False, "normalize": False}
        else:
            payload = {"input": list(texts), "model": model.model_key, "encoding_format": "float"}
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
            body = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise EmbeddingInvalidVector() from exc
        rows: object = body
        if self._dialect == "infinity":
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise EmbeddingInvalidVector()
            indexed: dict[int, object] = {}
            for entry in body["data"]:
                if not isinstance(entry, dict):
                    raise EmbeddingInvalidVector()
                index = entry.get("index")
                if type(index) is not int or index in indexed or not 0 <= index < len(texts):
                    raise EmbeddingInvalidVector()
                indexed[index] = entry.get("embedding")
            if len(indexed) != len(texts):
                raise EmbeddingInvalidVector()
            rows = [indexed[index] for index in range(len(texts))]
        if not isinstance(rows, list) or len(rows) != len(texts):
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


class TeiProvider(_HTTPProvider):
    def __init__(
        self,
        *,
        base_url: str,
        namespace: str,
        api_key: SecretStr | None = None,
        allow_private: bool = False,
        max_batch_size: int = 64,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        super().__init__(
            base_url=base_url,
            namespace=namespace,
            dialect="tei",
            api_key=api_key,
            allow_private=allow_private,
            max_batch_size=max_batch_size,
            max_response_bytes=max_response_bytes,
        )


class InfinityProvider(_HTTPProvider):
    def __init__(
        self,
        *,
        base_url: str,
        namespace: str,
        api_key: SecretStr | None = None,
        allow_private: bool = False,
        max_batch_size: int = 64,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        super().__init__(
            base_url=base_url,
            namespace=namespace,
            dialect="infinity",
            api_key=api_key,
            allow_private=allow_private,
            max_batch_size=max_batch_size,
            max_response_bytes=max_response_bytes,
        )
