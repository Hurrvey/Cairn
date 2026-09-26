"""Bounded live embedding probe used by model administration composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from cairn.core.cache import Cache, get_cache
from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import ProviderRuntimeProjection
from cairn.embedding.errors import EmbeddingInvalidVector
from cairn.embedding.providers import build_provider
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import HuggingFaceTokenizer, Tokenizer, TokenizerRegistry

__all__ = ["EmbeddingModelProbe", "ProbeResult"]

_PROBE_TEXT = "Cairn health probe"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    dimensions: int
    tokens: int
    #: Terms in the probe text's learned sparse vector; ``None`` for dense-only models.
    sparse_terms: int | None = None


class EmbeddingModelProbe:
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        provider_types: Mapping[str, Callable[..., Any]] | None = None,
        tokenizer_loader: Callable[[Path], Tokenizer] | None = None,
        master_key: SecretStr | None = None,
    ) -> None:
        self._cache = cache
        self._provider_types = dict(provider_types or {})
        self._tokenizer_loader = tokenizer_loader or HuggingFaceTokenizer.from_file
        self._master_key = master_key

    @property
    def cache(self) -> Cache:
        return self._cache or get_cache()

    def _provider(self, runtime: ProviderRuntimeProjection) -> Any:
        api_key = None
        if runtime.credential is not None:
            if self._master_key is None:
                raise EmbeddingInvalidVector()
            api_key = runtime.credential.open(self._master_key, runtime.workspace_id)
        namespace = f"{runtime.id}:{runtime.binding_revision}"
        injected = self._provider_types.get(runtime.family)
        if injected is not None:
            return injected(
                base_url=runtime.base_url,
                namespace=namespace,
                allow_private=runtime.allow_private,
                max_batch_size=runtime.max_batch_size,
            )
        return build_provider(
            runtime.family,
            base_url=runtime.base_url,
            namespace=namespace,
            api_key=api_key,
            allow_private=runtime.allow_private,
            max_batch_size=runtime.max_batch_size,
        )

    async def probe(
        self,
        model: ModelRef,
        provider_runtime: ProviderRuntimeProjection,
        tokenizer_path: Path,
    ) -> ProbeResult:
        tokenizer = self._tokenizer_loader(tokenizer_path)
        registry = TokenizerRegistry()
        assert model.tokenizer_id is not None
        registry.register(model.tokenizer_id, tokenizer)
        provider = self._provider(provider_runtime)
        try:
            await provider.__aenter__()
            service = EmbeddingService(provider=provider, cache=self.cache, tokenizers=registry)
            tokens = tokenizer.count((model.query_prefix or "") + _PROBE_TEXT)
            if not model.sparse:
                vector = await service.embed_query(model, _PROBE_TEXT)
                return ProbeResult(dimensions=len(vector.values), tokens=tokens)
            dense, sparse = await service.embed_query_hybrid(model, _PROBE_TEXT)
            if dense is None or sparse is None:
                # A model registered as sparse that returns no terms for plain
                # text would silently disable keyword search for its users.
                raise EmbeddingInvalidVector()
            return ProbeResult(
                dimensions=len(dense.values), tokens=tokens, sparse_terms=len(sparse.indices)
            )
        finally:
            with suppress(Exception):
                await provider.close()

    async def detect_dimension(
        self, model: ModelRef, provider_runtime: ProviderRuntimeProjection
    ) -> int:
        """Ask the provider for one vector and report its width, before registration."""
        provider = self._provider(provider_runtime)
        try:
            await provider.__aenter__()
            rows = await provider.embed(
                replace(model, send_dimension=False), [_PROBE_TEXT], purpose="document"
            )
        finally:
            with suppress(Exception):
                await provider.close()
        if len(rows) != 1 or not rows[0]:
            raise EmbeddingInvalidVector()
        return len(rows[0])
