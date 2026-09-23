"""Bounded live embedding probe used by model administration composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from cairn.core.cache import Cache, get_cache
from cairn.core.modelref import ModelRef
from cairn.core.provider_runtime import ProviderRuntimeProjection
from cairn.embedding.providers import InfinityProvider, TeiProvider
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import HuggingFaceTokenizer, Tokenizer, TokenizerRegistry

__all__ = ["EmbeddingModelProbe"]

_PROBE_TEXT = "Cairn health probe"


class EmbeddingModelProbe:
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        provider_types: Mapping[str, Callable[..., Any]] | None = None,
        tokenizer_loader: Callable[[Path], Tokenizer] | None = None,
    ) -> None:
        self._cache = cache
        self._provider_types = dict(
            provider_types or {"tei": TeiProvider, "infinity": InfinityProvider}
        )
        self._tokenizer_loader = tokenizer_loader or HuggingFaceTokenizer.from_file

    @property
    def cache(self) -> Cache:
        return self._cache or get_cache()

    async def probe(
        self,
        model: ModelRef,
        provider_runtime: ProviderRuntimeProjection,
        tokenizer_path: Path,
    ) -> tuple[int, int]:
        tokenizer = self._tokenizer_loader(tokenizer_path)
        registry = TokenizerRegistry()
        assert model.tokenizer_id is not None
        registry.register(model.tokenizer_id, tokenizer)
        provider_type = self._provider_types[provider_runtime.family]
        provider = provider_type(
            base_url=provider_runtime.base_url,
            namespace=f"{provider_runtime.id}:{provider_runtime.binding_revision}",
            allow_private=provider_runtime.allow_private,
            max_batch_size=provider_runtime.max_batch_size,
        )
        try:
            await provider.__aenter__()
            vector = await EmbeddingService(
                provider=provider,
                cache=self.cache,
                tokenizers=registry,
            ).embed_query(model, _PROBE_TEXT)
            return len(vector.values), tokenizer.count((model.query_prefix or "") + _PROBE_TEXT)
        finally:
            with suppress(Exception):
                await provider.close()
