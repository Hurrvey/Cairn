"""Redis-only ACTIVE runtime loading and query encoding (dense and learned sparse)."""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import SecretStr, ValidationError

from cairn.core.cache import BulkCache, Cache, get_cache
from cairn.core.config import RetrievalSettings
from cairn.core.errors import PermissionDenied
from cairn.core.logging import get_logger
from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import (
    ProviderRuntimeProjection,
    provider_runtime_key,
    provider_runtime_tombstone_key,
)
from cairn.core.retrieval_runtime import KnowledgeBaseRuntime
from cairn.core.secrets import SecretUnreadable
from cairn.embedding.providers import (
    HttpEmbeddingProvider,
    InfinityProvider,
    TeiProvider,
    build_provider,
)
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import HuggingFaceTokenizer, Tokenizer, TokenizerRegistry
from cairn.retrieval.errors import KbRuntimeCorrupt, KbRuntimeUnavailable, RetrievalUnsupported
from cairn.vectorstore.base import SparseVector

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QueryEncoding:
    dense: Sequence[float] | None
    sparse: SparseVector | None
    tokens: int
    cached: bool | None = None


def _open_provider(family: str, **kwargs: Any) -> HttpEmbeddingProvider:
    # TEI and Infinity resolve through this module's names so tests can swap them.
    if family == "tei":
        return TeiProvider(**kwargs)
    if family == "infinity":
        return InfinityProvider(**kwargs)
    return build_provider(family, **kwargs)


def _load_tokenizer_files(files: dict[str, str]) -> dict[str, Tokenizer]:
    """Load configured tokenizer files, skipping any that were never prepared.

    Startup-only and synchronous: a few files, read once. A model that needs a
    missing tokenizer fails with a clear error when a query uses it.
    """
    loaded: dict[str, Tokenizer] = {}
    for name, filename in files.items():
        path = Path(filename)
        if not path.is_file():
            log.warning("retrieval.tokenizer_missing", tokenizer=name)
            continue
        loaded[name] = HuggingFaceTokenizer.from_file(path)
    return loaded


class KnowledgeBaseRuntimeLoader:
    def __init__(self, *, cache: Cache | None = None, timeout_s: float = 0.25) -> None:
        self._cache = cache
        self._timeout_s = timeout_s

    @property
    def cache(self) -> Cache:
        return self._cache or get_cache()

    async def load_many(
        self, kb_ids: Sequence[UUID], workspace_id: UUID
    ) -> list[KnowledgeBaseRuntime]:
        return list(await asyncio.gather(*(self.load_one(kb_id, workspace_id) for kb_id in kb_ids)))

    async def load_one(self, kb_id: UUID, workspace_id: UUID) -> KnowledgeBaseRuntime:
        values = await self._read([kb_id])
        raw = values[0]
        if raw is None:
            raise KbRuntimeUnavailable("The knowledge base has no published ACTIVE runtime.")
        try:
            runtime = KnowledgeBaseRuntime.model_validate_json(raw)
        except (ValidationError, ValueError, TypeError) as exc:
            raise KbRuntimeCorrupt("The published knowledge base runtime is invalid.") from exc
        if runtime.workspace_id != workspace_id:
            raise PermissionDenied("The knowledge base does not belong to this workspace.")
        if runtime.id != kb_id:
            raise KbRuntimeCorrupt("The published knowledge base identity is invalid.")
        if runtime.status not in {"active", "indexing"} or runtime.index_version <= 0:
            raise KbRuntimeUnavailable("The knowledge base is not ready for retrieval.")
        model = runtime.embedding_model
        if (
            model.capability != "embedding"
            or model.dimension is None
            or model.dimension <= 0
            or not runtime.vector_binding.driver
        ):
            raise KbRuntimeCorrupt("The published knowledge base runtime is incomplete.")
        return runtime

    async def _read(self, kb_ids: Sequence[UUID]) -> list[bytes | None]:
        keys = [f"kb:runtime:{kb_id}" for kb_id in kb_ids]
        try:
            async with asyncio.timeout(self._timeout_s):
                if isinstance(self.cache, BulkCache):
                    values = await self.cache.mget(keys)
                else:
                    values = await asyncio.gather(*(self.cache.get(key) for key in keys))
        except Exception as exc:
            raise KbRuntimeUnavailable("Knowledge base runtime metadata is unavailable.") from exc

        return list(values)


class QueryEmbeddingRuntime:
    """Startup-owned query embedding providers keyed by immutable provider ID."""

    def __init__(
        self,
        settings: RetrievalSettings,
        *,
        cache: Cache | None = None,
        tokenizers: dict[str, Tokenizer] | None = None,
        master_key: SecretStr | None = None,
    ) -> None:
        self._settings = settings
        self._master_key = master_key
        self._cache = cache or get_cache()
        self._tokenizers = TokenizerRegistry()
        self._tokenizers_supplied = tokenizers is not None
        for name, tokenizer in (tokenizers or {}).items():
            self._tokenizers.register(name, tokenizer)
        self._providers: dict[UUID, Any] = {}
        self._retired_providers: list[Any] = []
        self._services: dict[UUID, EmbeddingService] = {}
        self._fingerprints: dict[UUID, str] = {}
        self._resolution_lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        opened: list[Any] = []
        try:
            if not self._tokenizers_supplied:
                for name, tokenizer in _load_tokenizer_files(
                    self._settings.tokenizer_files
                ).items():
                    self._tokenizers.register(name, tokenizer)
            for provider_id, endpoint in self._settings.embedding_endpoints.items():
                provider_type = TeiProvider if endpoint.dialect == "tei" else InfinityProvider
                provider = provider_type(
                    base_url=endpoint.base_url,
                    namespace=f"{provider_id}:{endpoint.namespace}",
                    api_key=endpoint.api_key,
                    allow_private=endpoint.allow_private,
                    max_batch_size=endpoint.max_batch_size,
                )
                try:
                    await provider.__aenter__()
                except BaseException:
                    with suppress(Exception):
                        await provider.close()
                    raise
                opened.append(provider)
                self._providers[provider_id] = provider
                self._services[provider_id] = EmbeddingService(
                    provider=provider,
                    cache=self._cache,
                    tokenizers=self._tokenizers,
                )
                self._fingerprints[provider_id] = "env:" + endpoint.model_dump_json()
        except BaseException:
            for provider in opened:
                with suppress(Exception):
                    await provider.close()
            self._providers.clear()
            self._services.clear()
            self._fingerprints.clear()
            raise
        self._started = True

    async def embed_query(
        self, model: ModelRef, query: str, *, workspace_id: UUID | None = None
    ) -> tuple[Sequence[float], int, bool | None]:
        encoding = await self.encode_query(
            model, query, workspace_id=workspace_id, dense=True, sparse=False
        )
        assert encoding.dense is not None
        return encoding.dense, encoding.tokens, encoding.cached

    async def encode_query(
        self,
        model: ModelRef,
        query: str,
        *,
        workspace_id: UUID | None = None,
        dense: bool = True,
        sparse: bool = False,
    ) -> QueryEncoding:
        """Encode one query; dense and learned sparse together cost one provider call."""
        if not self._started:
            raise RetrievalUnsupported("Query embedding runtime is not started.")
        if model.provider_id is None:
            raise RetrievalUnsupported(
                "Query embedding requires a configured provider identity for the ACTIVE model."
            )
        service, family = await self._service_for(
            model.provider_id, workspace_id, dynamic=model.dynamic_provider
        )
        if family != model.provider_family:
            raise RetrievalUnsupported(
                "The configured provider dialect does not match the ACTIVE model family."
            )
        if model.tokenizer_id is None:
            raise RetrievalUnsupported("Query embedding requires an explicitly loaded tokenizer.")
        try:
            tokenizer = self._tokenizers.for_model(model)
        except Exception as exc:
            raise RetrievalUnsupported("The ACTIVE model tokenizer is unavailable.") from exc
        normalized = " ".join(unicodedata.normalize("NFKC", query).split())
        if not normalized:
            raise RetrievalUnsupported("Query text must contain non-whitespace text.")
        prepared = (model.query_prefix or "") + normalized
        if model.max_input_tokens is None:
            raise RetrievalUnsupported("The ACTIVE model has no query token budget.")
        token_count = tokenizer.count(prepared)
        if token_count > model.max_input_tokens:
            raise RetrievalUnsupported(
                "Query text exceeds the ACTIVE model token budget; it was not truncated."
            )
        if not sparse:
            vector = await service.embed_query(model, query)
            return QueryEncoding(dense=vector.values, sparse=None, tokens=token_count)
        if not model.sparse or not service.supports_sparse:
            raise RetrievalUnsupported("The ACTIVE sparse model cannot encode sparse queries.")
        dense_vector, sparse_vector = await service.embed_query_hybrid(
            model, query, dense=dense, sparse=True
        )
        return QueryEncoding(
            dense=dense_vector.values if dense_vector is not None else None,
            sparse=sparse_vector,
            tokens=token_count,
        )

    async def embed_query_for_workspace(
        self, workspace_id: UUID, model: ModelRef, query: str
    ) -> tuple[Sequence[float], int, bool | None]:
        return await self.embed_query(model, query, workspace_id=workspace_id)

    async def encode_query_for_workspace(
        self,
        workspace_id: UUID,
        model: ModelRef,
        query: str,
        *,
        dense: bool,
        sparse: bool,
    ) -> QueryEncoding:
        return await self.encode_query(
            model, query, workspace_id=workspace_id, dense=dense, sparse=sparse
        )

    async def _service_for(
        self, provider_id: UUID, workspace_id: UUID | None, *, dynamic: bool
    ) -> tuple[EmbeddingService, str]:
        projection = await self._load_projection(provider_id, require_cache=dynamic)
        if projection is None:
            if dynamic:
                raise RetrievalUnsupported(
                    "The registered provider runtime has not been published."
                )
            endpoint = self._settings.embedding_endpoints.get(provider_id)
            if endpoint is None:
                raise RetrievalUnsupported("Query embedding requires a published provider runtime.")
            service = self._services.get(provider_id)
            if service is None:
                raise RetrievalUnsupported(
                    "The configured query embedding provider is unavailable."
                )
            return service, endpoint.dialect
        if workspace_id is None or projection.workspace_id != workspace_id:
            raise RetrievalUnsupported(
                "The published provider runtime does not belong to the query workspace."
            )
        async with self._resolution_lock:
            if self._fingerprints.get(provider_id) != projection.fingerprint:
                await self._replace_provider(projection)
            service = self._services.get(provider_id)
            if service is None:
                raise RetrievalUnsupported("The published query embedding provider is unavailable.")
            return service, projection.family

    async def _load_projection(
        self, provider_id: UUID, *, require_cache: bool
    ) -> ProviderRuntimeProjection | None:
        try:
            raw = await self._cache.get(provider_runtime_key(provider_id))
        except Exception as exc:
            if not require_cache:
                return None
            raise RetrievalUnsupported("The published provider runtime is unavailable.") from exc
        if raw is None:
            try:
                deleted = await self._cache.get(provider_runtime_tombstone_key(provider_id))
            except Exception as exc:
                if not require_cache:
                    return None
                raise RetrievalUnsupported(
                    "The published provider runtime is unavailable."
                ) from exc
            if deleted is not None:
                raise RetrievalUnsupported("The registered provider has been deleted.")
            return None
        try:
            projection = ProviderRuntimeProjection.model_validate_json(raw)
        except Exception as exc:
            raise RetrievalUnsupported("The published provider runtime is invalid.") from exc
        if projection.id != provider_id:
            raise RetrievalUnsupported("The published provider identity is invalid.")
        return projection

    async def _replace_provider(self, projection: ProviderRuntimeProjection) -> None:
        api_key: SecretStr | None = None
        if projection.credential is not None:
            if self._master_key is None:
                raise RetrievalUnsupported("This process cannot open provider credentials.")
            try:
                api_key = projection.credential.open(self._master_key, projection.workspace_id)
            except SecretUnreadable as exc:
                raise RetrievalUnsupported("The provider credential cannot be read.") from exc
        kwargs: dict[str, Any] = {
            "base_url": projection.base_url,
            "namespace": f"{projection.id}:{projection.binding_revision}",
            "allow_private": projection.allow_private,
            "max_batch_size": projection.max_batch_size,
        }
        if api_key is not None:
            kwargs["api_key"] = api_key
        try:
            provider = _open_provider(projection.family, **kwargs)
        except Exception as exc:
            raise RetrievalUnsupported("The published provider cannot be configured.") from exc
        try:
            await provider.__aenter__()
            service = EmbeddingService(
                provider=provider,
                cache=self._cache,
                tokenizers=self._tokenizers,
            )
        except BaseException:
            with suppress(Exception):
                await provider.close()
            raise
        previous = self._providers.get(projection.id)
        self._providers[projection.id] = provider
        self._services[projection.id] = service
        self._fingerprints[projection.id] = projection.fingerprint
        if previous is not None:
            self._retired_providers.append(previous)

    async def close(self) -> None:
        providers = [*self._providers.values(), *self._retired_providers]
        self._providers.clear()
        self._retired_providers.clear()
        self._services.clear()
        self._fingerprints.clear()
        self._started = False
        for provider in providers:
            with suppress(Exception):
                await provider.close()
