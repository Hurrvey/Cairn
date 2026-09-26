"""Cache-first dense and sparse embedding with bounded provider work and stable input order."""

from __future__ import annotations

import asyncio
import json
import math
import secrets
import struct
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from hashlib import sha256
from itertools import pairwise
from time import monotonic as _monotonic
from typing import TypeVar

from cairn.core.cache import BulkCache, Cache
from cairn.core.config import EmbeddingSettings
from cairn.core.logging import get_logger
from cairn.core.modelref import ModelRef
from cairn.embedding import metrics
from cairn.embedding.base import (
    EmbeddingProvider,
    HybridEmbeddingProvider,
    HybridOutput,
    Purpose,
    Vector,
)
from cairn.embedding.errors import (
    EmbeddingCircuitOpen,
    EmbeddingConfigurationError,
    EmbeddingInvalidVector,
    EmbeddingProviderError,
    EmbeddingTimeout,
)
from cairn.embedding.tokenizers import Tokenizer, TokenizerRegistry
from cairn.vectorstore.base import SparseVector

log = get_logger(__name__)
_sleep = asyncio.sleep

_T = TypeVar("_T")
_R = TypeVar("_R")

_DENSE_PREFIX = "emb:v1:"
_SPARSE_PREFIX = "sps:v1:"

#: Dense vectors and learned sparse vectors, each ``None`` when not requested.
HybridEmbeddings = tuple[list[Vector] | None, list[SparseVector | None] | None]


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

    @property
    def supports_sparse(self) -> bool:
        return isinstance(self._provider, HybridEmbeddingProvider)

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

    async def embed_query_hybrid(
        self, model: ModelRef, text: str, *, dense: bool = True, sparse: bool = True
    ) -> tuple[Vector | None, SparseVector | None]:
        vectors, weights = await self._run_hybrid(
            model, [text], purpose="query", dense=dense, sparse=sparse, batch_size=1
        )
        return (
            vectors[0] if vectors is not None else None,
            weights[0] if weights is not None else None,
        )

    async def embed_documents_hybrid(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        dense: bool = True,
        sparse: bool = True,
        batch_size: int | None = None,
    ) -> HybridEmbeddings:
        return await self._run_hybrid(
            model, texts, purpose="document", dense=dense, sparse=sparse, batch_size=batch_size
        )

    def _timeout(self, purpose: Purpose) -> float:
        return (
            self._settings.query_timeout_s
            if purpose == "query"
            else self._settings.document_timeout_s
        )

    async def _run(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        batch_size: int | None,
    ) -> list[Vector]:
        with metrics.duration.labels(purpose=purpose).time():
            try:
                async with asyncio.timeout(self._timeout(purpose)):
                    return await self._embed(model, texts, purpose=purpose, batch_size=batch_size)
            except TimeoutError as exc:
                raise EmbeddingTimeout() from exc

    async def _run_hybrid(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        dense: bool,
        sparse: bool,
        batch_size: int | None,
    ) -> HybridEmbeddings:
        with metrics.duration.labels(purpose=purpose).time():
            try:
                async with asyncio.timeout(self._timeout(purpose)):
                    return await self._embed_hybrid(
                        model,
                        texts,
                        purpose=purpose,
                        dense=dense,
                        sparse=sparse,
                        batch_size=batch_size,
                    )
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

    def _digest(self, model: ModelRef, tokenizer: Tokenizer, text: str, purpose: Purpose) -> str:
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
        return sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()

    def _key(self, model: ModelRef, tokenizer: Tokenizer, text: str, purpose: Purpose) -> str:
        return _DENSE_PREFIX + self._digest(model, tokenizer, text, purpose)

    def _check(self, model: ModelRef, texts: Sequence[str], batch_size: int | None) -> None:
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

    def _batch_size(self, model: ModelRef, batch_size: int | None) -> int:
        return min(
            batch_size or model.optimal_batch_size,
            model.optimal_batch_size,
            self._provider.max_batch_size,
        )

    async def _embed(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        batch_size: int | None,
    ) -> list[Vector]:
        self._check(model, texts, batch_size)
        tokenizer = self._tokenizers.for_model(model)
        size = self._batch_size(model, batch_size)
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
            await self._write_cache(writes)
        return [found[key] for key in keys]

    async def _embed_hybrid(
        self,
        model: ModelRef,
        texts: Sequence[str],
        *,
        purpose: Purpose,
        dense: bool,
        sparse: bool,
        batch_size: int | None,
    ) -> HybridEmbeddings:
        self._check(model, texts, batch_size)
        if not dense and not sparse:
            raise EmbeddingConfigurationError("Request at least one embedding output.")
        if sparse and (not model.sparse or not self.supports_sparse):
            raise EmbeddingConfigurationError("This model does not produce sparse vectors.")
        tokenizer = self._tokenizers.for_model(model)
        size = self._batch_size(model, batch_size)
        prepared = [self._prepare(model, text, tokenizer, purpose) for text in texts]
        digests = [self._digest(model, tokenizer, text, purpose) for text in prepared]
        unique = dict(zip(digests, prepared, strict=True))
        order = list(unique)

        dense_found: dict[str, Vector] = {}
        sparse_found: dict[str, SparseVector | None] = {}
        for offset in range(0, len(order), 256):
            block = order[offset : offset + 256]
            if dense:
                keys = [_DENSE_PREFIX + digest for digest in block]
                for digest, cached in zip(block, await self._cached(keys, model), strict=True):
                    if cached is not None:
                        dense_found[digest] = cached
            if sparse:
                keys = [_SPARSE_PREFIX + digest for digest in block]
                for digest, (hit, cached_weights) in zip(
                    block, await self._cached_sparse(keys), strict=True
                ):
                    if hit:
                        sparse_found[digest] = cached_weights

        # Ask only for what is missing, grouped so each call requests one
        # combination of outputs. When dense and sparse are both missing — the
        # common case — one call returns both.
        groups: dict[tuple[bool, bool], list[str]] = {}
        for digest in order:
            need = (dense and digest not in dense_found, sparse and digest not in sparse_found)
            if need[0] or need[1]:
                groups.setdefault(need, []).append(digest)
        for (need_dense, need_sparse), members in groups.items():
            for offset in range(0, len(members), size):
                batch = members[offset : offset + size]
                batch_texts = [unique[digest] for digest in batch]
                writes: dict[str, bytes] = {}
                vectors: list[Vector] | None
                if need_sparse:
                    vectors, weights = await self._invoke_hybrid(
                        model, batch_texts, purpose, dense=need_dense
                    )
                    for digest, sparse_vector in zip(batch, weights, strict=True):
                        sparse_found[digest] = sparse_vector
                        writes[_SPARSE_PREFIX + digest] = _pack_sparse(sparse_vector)
                else:
                    vectors = await self._invoke(model, batch_texts, purpose)
                if vectors is not None:
                    for digest, vector in zip(batch, vectors, strict=True):
                        dense_found[digest] = vector
                        writes[_DENSE_PREFIX + digest] = vector.to_bytes()
                await self._write_cache(writes)

        return (
            [dense_found[digest] for digest in digests] if dense else None,
            [sparse_found[digest] for digest in digests] if sparse else None,
        )

    async def _write_cache(self, writes: dict[str, bytes]) -> None:
        if not self._settings.cache_enabled or not writes:
            return
        try:
            if isinstance(self._cache, BulkCache):
                await self._cache.mset(writes, self._settings.cache_ttl_s)
            else:
                for key, raw in writes.items():
                    await self._cache.set(key, raw, self._settings.cache_ttl_s)
        except Exception as exc:
            log.warning("embedding.cache_write_failed", error=type(exc).__name__)

    async def _read_cache(self, keys: list[str]) -> list[bytes | None]:
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
        return list(entries)

    async def _cached(self, keys: list[str], model: ModelRef) -> list[Vector | None]:
        if not self._settings.cache_enabled:
            metrics.cache_requests.labels(outcome="disabled").inc(len(keys))
            return [None] * len(keys)
        assert model.dimension is not None
        results: list[Vector | None] = []
        for raw in await self._read_cache(keys):
            vector = None
            if raw is not None:
                try:
                    vector = Vector.from_bytes(raw, dim=model.dimension, normalized=model.normalize)
                except EmbeddingInvalidVector:
                    metrics.cache_requests.labels(outcome="corrupt").inc()
            metrics.cache_requests.labels(outcome="hit" if vector else "miss").inc()
            results.append(vector)
        return results

    async def _cached_sparse(self, keys: list[str]) -> list[tuple[bool, SparseVector | None]]:
        """``(hit, vector)`` per key; a hit may be ``None`` — a text with no terms."""
        if not self._settings.cache_enabled:
            metrics.cache_requests.labels(outcome="disabled").inc(len(keys))
            return [(False, None)] * len(keys)
        results: list[tuple[bool, SparseVector | None]] = []
        for raw in await self._read_cache(keys):
            entry: tuple[bool, SparseVector | None] = (False, None)
            if raw is not None:
                try:
                    entry = (True, _unpack_sparse(raw))
                except EmbeddingInvalidVector:
                    metrics.cache_requests.labels(outcome="corrupt").inc()
            metrics.cache_requests.labels(outcome="hit" if entry[0] else "miss").inc()
            results.append(entry)
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
        dimension = model.dimension
        assert dimension is not None

        def validate(raw: Sequence[Sequence[float]]) -> list[Vector]:
            if len(raw) != len(texts):
                raise EmbeddingInvalidVector()
            return [
                Vector.from_values(row, dim=dimension, normalize=model.normalize) for row in raw
            ]

        return await self._call(
            purpose, lambda: self._provider.embed(model, texts, purpose=purpose), validate
        )

    async def _invoke_hybrid(
        self, model: ModelRef, texts: list[str], purpose: Purpose, *, dense: bool
    ) -> tuple[list[Vector] | None, list[SparseVector | None]]:
        provider = self._provider
        if not isinstance(provider, HybridEmbeddingProvider):  # pragma: no cover - checked earlier
            raise EmbeddingConfigurationError("This model does not produce sparse vectors.")
        dimension = model.dimension
        assert dimension is not None

        def validate(
            output: HybridOutput,
        ) -> tuple[list[Vector] | None, list[SparseVector | None]]:
            if output.sparse is None or len(output.sparse) != len(texts):
                raise EmbeddingInvalidVector()
            vectors = None
            if dense:
                if output.dense is None or len(output.dense) != len(texts):
                    raise EmbeddingInvalidVector()
                vectors = [
                    Vector.from_values(row, dim=dimension, normalize=model.normalize)
                    for row in output.dense
                ]
            return vectors, list(output.sparse)

        return await self._call(
            purpose,
            lambda: provider.embed_hybrid(model, texts, purpose=purpose, dense=dense, sparse=True),
            validate,
        )

    async def _call(
        self,
        purpose: Purpose,
        request: Callable[[], Awaitable[_T]],
        validate: Callable[[_T], _R],
    ) -> _R:
        """One provider request under the concurrency bound, retry policy and circuit."""
        for attempt in range(self._settings.max_retries + 1):
            try:
                async with self._semaphore:
                    probe = self._admit()
                    try:
                        result = validate(await request())
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
                    return result
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


def _pack_sparse(vector: SparseVector | None) -> bytes:
    if vector is None:
        return struct.pack("<I", 0)
    count = len(vector.indices)
    return struct.pack(f"<I{count}I{count}f", count, *vector.indices, *vector.values)


def _unpack_sparse(raw: bytes) -> SparseVector | None:
    if len(raw) < 4:
        raise EmbeddingInvalidVector()
    (count,) = struct.unpack_from("<I", raw)
    if len(raw) != 4 + 8 * count:
        raise EmbeddingInvalidVector()
    if count == 0:
        return None
    indices = struct.unpack_from(f"<{count}I", raw, 4)
    values = struct.unpack_from(f"<{count}f", raw, 4 + 4 * count)
    if any(b <= a for a, b in pairwise(indices)) or not all(
        math.isfinite(value) and value > 0 for value in values
    ):
        raise EmbeddingInvalidVector()
    return SparseVector(indices=tuple(indices), values=tuple(values))
