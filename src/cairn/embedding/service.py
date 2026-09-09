"""Cache-first dense embedding with bounded provider work and stable input order."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import unicodedata
from collections import deque
from collections.abc import Sequence
from hashlib import sha256
from time import monotonic as _monotonic

from cairn.core.cache import BulkCache, Cache
from cairn.core.config import EmbeddingSettings
from cairn.core.logging import get_logger
from cairn.core.modelref import ModelRef
from cairn.embedding import metrics
from cairn.embedding.base import EmbeddingProvider, Purpose, Vector
from cairn.embedding.errors import (
    EmbeddingCircuitOpen,
    EmbeddingConfigurationError,
    EmbeddingInvalidVector,
    EmbeddingProviderError,
    EmbeddingTimeout,
)
from cairn.embedding.tokenizers import Tokenizer, TokenizerRegistry

log = get_logger(__name__)
_sleep = asyncio.sleep


class EmbeddingService:
    """One reusable service per provider binding, shared by queries and workers.

    Composition code supplies the provider/cache/tokenizers once. The service
    never resolves a model or credential from a database on the request path.
    """

    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        cache: Cache,
        tokenizers: TokenizerRegistry,
        settings: EmbeddingSettings | None = None,
    ) -> None:
        if not provider.cache_namespace or provider.max_batch_size <= 0:
            raise EmbeddingConfigurationError()
        self._provider = provider
        self._cache = cache
        self._tokenizers = tokenizers
        self._settings = settings or EmbeddingSettings()
        self._semaphore = asyncio.Semaphore(self._settings.max_concurrency)
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._probing = False

    def count_tokens(self, model: ModelRef, text: str) -> int:
        return self._tokenizers.for_model(model).count(text)

    def truncate_to_tokens(self, model: ModelRef, text: str, limit: int) -> str:
        return self._tokenizers.for_model(model).truncate(text, limit)

    async def embed_query(self, model: ModelRef, text: str) -> Vector:
        return (await self._run(model, [text], purpose="query", batch_size=1))[0]

    async def embed_documents(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        batch_size: int | None = None,
    ) -> list[Vector]:
        return await self._run(model, texts, purpose="document", batch_size=batch_size)

    async def _run(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        batch_size: int | None,
    ) -> list[Vector]:
        timeout = (
            self._settings.query_timeout_s
            if purpose == "query"
            else self._settings.document_timeout_s
        )
        with metrics.duration.labels(purpose=purpose).time():
            try:
                async with asyncio.timeout(timeout):
                    return await self._embed(model, texts, purpose=purpose, batch_size=batch_size)
            except TimeoutError as exc:
                raise EmbeddingTimeout() from exc

    def _prepare(self, model: ModelRef, text: str, tokenizer: Tokenizer, purpose: Purpose) -> str:
        if not isinstance(text, str):
            raise EmbeddingConfigurationError("Embedding input must contain strings.")
        normalized = " ".join(unicodedata.normalize("NFKC", text).split())
        try:
            normalized.encode("utf-8")
        except UnicodeError as exc:
            raise EmbeddingConfigurationError("Embedding input is not valid Unicode.") from exc
        if not normalized:
            raise EmbeddingConfigurationError("Embedding input must not be empty.")
        prefix = (model.query_prefix or "") if purpose == "query" else ""
        # Prefix is configuration and is kept verbatim, including its separator.
        prepared = prefix + normalized
        assert model.max_input_tokens is not None
        if tokenizer.count(prepared) > model.max_input_tokens:
            try:
                prepared = tokenizer.truncate(prepared, model.max_input_tokens)
            except ValueError as exc:
                raise EmbeddingConfigurationError("The token budget is too small.") from exc
            if not prepared.startswith(prefix) or not prepared[len(prefix) :].strip():
                raise EmbeddingConfigurationError("The prefix leaves no room for input text.")
            metrics.truncated_inputs.inc()
            log.warning("embedding.truncated", purpose=purpose, max_tokens=model.max_input_tokens)
        return prepared

    def _key(self, model: ModelRef, tokenizer: Tokenizer, text: str, purpose: Purpose) -> str:
        identity = [
            self._provider.cache_namespace,
            str(model.id),
            model.provider_family,
            model.model_key,
            model.dimension,
            model.normalize,
            model.max_input_tokens,
            model.query_prefix,
            model.tokenizer_id,
            tokenizer.fingerprint,
            purpose,
            text,
        ]
        return "emb:v1:" + sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()

    async def _embed(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        batch_size: int | None,
    ) -> list[Vector]:
        if (
            model.capability != "embedding"
            or not model.dimension
            or model.dimension <= 0
            or not model.max_input_tokens
            or model.max_input_tokens <= 0
            or model.optimal_batch_size <= 0
            or (batch_size is not None and batch_size <= 0)
            or isinstance(texts, (str, bytes))
        ):
            raise EmbeddingConfigurationError()
        tokenizer = self._tokenizers.for_model(model)
        size = min(
            batch_size or model.optimal_batch_size,
            model.optimal_batch_size,
            self._provider.max_batch_size,
        )
        prepared = [self._prepare(model, text, tokenizer, purpose) for text in texts]
        keys = [self._key(model, tokenizer, text, purpose) for text in prepared]
        unique = dict(zip(keys, prepared, strict=True))
        found: dict[str, Vector] = {}
        missing: list[str] = []
        unique_keys = list(unique)
        for offset in range(0, len(unique_keys), 256):
            block = unique_keys[offset : offset + 256]
            for key, cached in zip(block, await self._cached(block, model), strict=True):
                if cached is not None:
                    found[key] = cached
                else:
                    missing.append(key)
        for offset in range(0, len(missing), size):
            batch_keys = missing[offset : offset + size]
            vectors = await self._invoke(model, [unique[key] for key in batch_keys], purpose)
            writes = {}
            for key, vector in zip(batch_keys, vectors, strict=True):
                found[key] = vector
                writes[key] = vector.to_bytes()
            if self._settings.cache_enabled:
                try:
                    if isinstance(self._cache, BulkCache):
                        await self._cache.mset(writes, self._settings.cache_ttl_s)
                    else:
                        for key, raw in writes.items():
                            await self._cache.set(key, raw, self._settings.cache_ttl_s)
                except Exception as exc:
                    log.warning("embedding.cache_write_failed", error=type(exc).__name__)
        return [found[key] for key in keys]

    async def _cached(self, keys: list[str], model: ModelRef) -> list[Vector | None]:
        if not self._settings.cache_enabled:
            metrics.cache_requests.labels(outcome="disabled").inc(len(keys))
            return [None] * len(keys)
        assert model.dimension is not None
        try:
            if len(keys) > 1 and isinstance(self._cache, BulkCache):
                entries = await self._cache.mget(keys)
            else:
                entries = [await self._cache.get(key) for key in keys]
            if len(entries) != len(keys):
                raise ValueError("cache returned the wrong number of entries")
        except Exception as exc:
            metrics.cache_requests.labels(outcome="error").inc(len(keys))
            log.warning("embedding.cache_read_failed", error=type(exc).__name__)
            entries = [None] * len(keys)
        results: list[Vector | None] = []
        for raw in entries:
            vector = None
            if raw is not None:
                try:
                    vector = Vector.from_bytes(raw, dim=model.dimension, normalized=model.normalize)
                except EmbeddingInvalidVector:
                    metrics.cache_requests.labels(outcome="corrupt").inc()
            metrics.cache_requests.labels(outcome="hit" if vector else "miss").inc()
            results.append(vector)
        return results

    def _admit(self) -> bool:
        if self._opened_at is None:
            return False
        if self._probing or _monotonic() - self._opened_at < self._settings.circuit_reset_s:
            raise EmbeddingCircuitOpen()
        self._probing = True
        return True

    def _failed(self) -> None:
        now = _monotonic()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self._settings.circuit_window_s:
            self._failures.popleft()
        if self._probing or len(self._failures) >= self._settings.circuit_failure_threshold:
            self._opened_at = now

    async def _invoke(self, model: ModelRef, texts: list[str], purpose: Purpose) -> list[Vector]:
        assert model.dimension is not None
        for attempt in range(self._settings.max_retries + 1):
            try:
                async with self._semaphore:
                    probe = self._admit()
                    try:
                        raw = await self._provider.embed(model, texts, purpose=purpose)
                        if len(raw) != len(texts):
                            raise EmbeddingInvalidVector()
                        vectors = [
                            Vector.from_values(row, dim=model.dimension, normalize=model.normalize)
                            for row in raw
                        ]
                    except EmbeddingProviderError:
                        metrics.provider_requests.labels(purpose=purpose, outcome="error").inc()
                        self._failed()
                        raise
                    finally:
                        if probe:
                            self._probing = False
                    metrics.provider_requests.labels(purpose=purpose, outcome="ok").inc()
                    if probe:
                        self._opened_at = None
                        self._failures.clear()
                    return vectors
            except EmbeddingCircuitOpen:
                raise
            except EmbeddingProviderError as exc:
                if attempt >= self._settings.max_retries:
                    raise
                requested = exc.retry_after or 0
                if not math.isfinite(requested) or requested < 0:
                    requested = 0
                delay = max(
                    requested, min(0.1 * 2**attempt, 2) * (0.5 + secrets.randbelow(1000) / 1000)
                )
                await _sleep(delay)
        raise AssertionError("unreachable retry loop")
