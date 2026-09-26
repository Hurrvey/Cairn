"""Production composition for ingestion workers."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from cairn.catalog.dto import BindingRef
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.core.cache import get_cache
from cairn.core.config import get_settings
from cairn.core.errors import ValidationFailed
from cairn.core.logging import get_logger
from cairn.core.modelref import ModelRef
from cairn.core.secrets import SecretUnreadable
from cairn.embedding.providers import HttpEmbeddingProvider, build_provider
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import (
    HuggingFaceTokenizer,
    TiktokenTokenizer,
    Tokenizer,
    TokenizerRegistry,
)
from cairn.ingestion.pipeline import (
    CustomExecutorResolver,
    IngestionPipeline,
    PipelineResources,
    PreparedEmbedding,
)
from cairn.ingestion.registry import get_parser_registry
from cairn.ingestion.runtime_config import get_ingestion_runtime_settings
from cairn.modelgw.catalog import ModelCatalog, get_model_catalog
from cairn.modelgw.dto import EmbeddingRuntimeRef
from cairn.objectstore.base import ObjectStore
from cairn.objectstore.registry import ObjectBindingRef, get_object_registry
from cairn.vectorstore.base import VectorStore
from cairn.vectorstore.registry import VectorBindingRef, get_vector_registry

log = get_logger(__name__)


class PipelineRuntime:
    def __init__(
        self,
        *,
        models: ModelCatalog | None = None,
        custom_executor_for: CustomExecutorResolver | None = None,
    ) -> None:
        self._models = models or get_model_catalog()
        self._tokenizers = _load_tokenizers()
        self._providers: dict[UUID, HttpEmbeddingProvider] = {}
        self._retired_providers: list[HttpEmbeddingProvider] = []
        self._embeddings: dict[UUID, tuple[str, PreparedEmbedding]] = {}
        self._resolution_lock = asyncio.Lock()
        self._closed = False
        settings = get_ingestion_runtime_settings()
        self.pipeline = IngestionPipeline(
            catalog=CatalogIngestionFacade(),
            resources=PipelineResources(
                parsers=get_parser_registry(),
                object_store_for=self.object_store_for,
                vector_store_for=self.vector_store_for,
                embedding_for=self.embedding_for,
                max_source_bytes=settings.max_source_bytes,
                max_artifact_bytes=settings.max_artifact_bytes,
                custom_executor_for=custom_executor_for,
            ),
        )

    async def object_store_for(self, binding: BindingRef) -> ObjectStore:
        return await get_object_registry().for_binding(
            ObjectBindingRef(binding.id, binding.driver, binding.config)
        )

    async def vector_store_for(self, binding: BindingRef) -> VectorStore:
        return await get_vector_registry().for_binding(
            VectorBindingRef(binding.id, binding.driver, binding.config)
        )

    async def embedding_for(self, model: ModelRef) -> PreparedEmbedding:
        async with self._resolution_lock:
            if self._closed:
                raise RuntimeError("pipeline runtime is closed")
            return await self._resolve_embedding(model)

    async def _resolve_embedding(self, model: ModelRef) -> PreparedEmbedding:
        runtime = await self._models.get_embedding_runtime(model.id)
        if runtime.model != model:
            raise ValidationFailed(
                "The registered embedding model no longer matches the index version snapshot."
            )
        if runtime.base_url is None:
            raise ValidationFailed("The embedding provider has no configured endpoint.")
        revision = runtime.config.get("binding_revision")
        if not isinstance(revision, str) or not revision:
            raise ValidationFailed("The embedding provider requires binding_revision.")
        allow_private = runtime.config.get("allow_private", False)
        max_batch_size = runtime.config.get("max_batch_size", 64)
        if type(allow_private) is not bool or type(max_batch_size) is not int:
            raise ValidationFailed("The embedding provider runtime configuration is invalid.")
        tokenizer = self._tokenizers.for_model(runtime.model)
        fingerprint = _binding_fingerprint(runtime, tokenizer)
        # The client also depends on WHICH key it holds; a rotated key must
        # replace the client without changing the embedding identity above.
        client_key = fingerprint + (
            f":{runtime.credential.secret_id}" if runtime.credential is not None else ""
        )
        cached = self._embeddings.get(model.id)
        if cached is not None and cached[0] == client_key:
            return cached[1]
        api_key = None
        if runtime.credential is not None:
            if runtime.workspace_id is None:
                raise ValidationFailed("The embedding provider credential has no workspace.")
            try:
                api_key = runtime.credential.open(get_settings().master_key, runtime.workspace_id)
            except SecretUnreadable as exc:
                raise ValidationFailed("The embedding provider credential cannot be read.") from exc
        try:
            provider = build_provider(
                runtime.provider_family,
                base_url=runtime.base_url,
                namespace=revision,
                api_key=api_key,
                allow_private=allow_private,
                max_batch_size=max_batch_size,
            )
        except Exception as exc:
            raise ValidationFailed(
                f"Embedding provider family {runtime.provider_family!r} has no worker adapter."
            ) from exc
        try:
            await provider.__aenter__()
            prepared = PreparedEmbedding(
                model=runtime.model,
                tokenizer=tokenizer,
                service=EmbeddingService(
                    provider=provider,
                    cache=get_cache(),
                    tokenizers=self._tokenizers,
                    settings=get_settings().embedding,
                ),
                binding_fingerprint=fingerprint,
            )
        except BaseException:
            with suppress(Exception):
                await provider.close()
            raise
        previous = self._providers.get(model.id)
        self._providers[model.id] = provider
        self._embeddings[model.id] = (client_key, prepared)
        if previous is not None:
            self._retired_providers.append(previous)
        return prepared

    async def close(self) -> None:
        async with self._resolution_lock:
            self._closed = True
            providers = [*self._providers.values(), *self._retired_providers]
            self._providers.clear()
            self._retired_providers.clear()
            self._embeddings.clear()
        for provider in providers:
            with suppress(Exception):
                await provider.close()
        await get_object_registry().close()
        await get_vector_registry().close()


def _binding_fingerprint(runtime: EmbeddingRuntimeRef, tokenizer: Tokenizer) -> str:
    model = asdict(runtime.model)
    model.pop("dynamic_provider", None)
    model["id"] = str(runtime.model.id)
    model["provider_id"] = (
        str(runtime.model.provider_id) if runtime.model.provider_id is not None else None
    )
    identity = {
        "model": model,
        "provider": {
            "id": str(runtime.provider_id),
            "family": runtime.provider_family,
            "base_url": runtime.base_url,
            "config": runtime.config,
            "has_credentials": runtime.has_credentials,
        },
        "tokenizer": tokenizer.fingerprint,
    }
    return sha256(
        json.dumps(identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _load_tokenizers() -> TokenizerRegistry:
    registry = TokenizerRegistry()
    for name, binding in get_ingestion_runtime_settings().tokenizer_bindings().items():
        kind, separator, value = binding.partition(":")
        if not separator or not value:
            raise ValidationFailed(f"Tokenizer binding {name!r} is invalid.")
        tokenizer: Tokenizer
        if kind == "tiktoken":
            tokenizer = TiktokenTokenizer.from_encoding(value)
        elif kind == "hf":
            path = Path(value)
            if not path.is_file():
                # An optional model's tokenizer that was never prepared; models
                # that need it fail with TokenizerUnavailable when used.
                log.warning("ingestion.tokenizer_missing", tokenizer=name)
                continue
            tokenizer = HuggingFaceTokenizer.from_file(path)
        else:
            raise ValidationFailed(f"Tokenizer binding {name!r} uses an unknown loader.")
        registry.register(name, tokenizer)
    return registry


_runtime: PipelineRuntime | None = None


def get_pipeline_runtime() -> PipelineRuntime:
    global _runtime
    if _runtime is None:
        _runtime = PipelineRuntime()
    return _runtime


async def close_pipeline_runtime() -> None:
    global _runtime
    if _runtime is not None:
        await _runtime.close()
    _runtime = None
