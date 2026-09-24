"""Vector driver resolution.

Every vector binding resolves to the deployment's Qdrant (ADR-0009); connection
details are deployment configuration, not binding configuration.
"""

from __future__ import annotations

import warnings
from typing import Any
from uuid import UUID

from cairn.core.logging import get_logger
from cairn.vectorstore.base import VectorStore
from cairn.vectorstore.errors import VectorStoreUnavailable

__all__ = ["VectorBindingRef", "VectorStoreRegistry", "get_vector_registry"]

log = get_logger(__name__)


class VectorBindingRef:
    __slots__ = ("config", "credentials", "driver", "id")

    def __init__(
        self,
        binding_id: UUID,
        driver: str,
        config: dict[str, Any] | None = None,
        credentials: dict[str, str] | None = None,
    ) -> None:
        self.id = binding_id
        self.driver = driver
        self.config = config or {}
        self.credentials = credentials or {}

    def __repr__(self) -> str:  # pragma: no cover
        return f"VectorBindingRef(driver={self.driver!r}, id={self.id})"


class VectorStoreRegistry:
    def __init__(self) -> None:
        self._instances: dict[UUID, VectorStore] = {}

    async def for_binding(self, binding: VectorBindingRef) -> VectorStore:
        cached = self._instances.get(binding.id)
        if cached is not None:
            return cached

        store = await self._build(binding)
        status = await store.health()
        if not status.healthy:
            await store.close()
            raise VectorStoreUnavailable(
                f"The {binding.driver} vector store is not reachable: {status.detail}"
            )
        self._instances[binding.id] = store
        log.info("vectorstore.bound", driver=binding.driver, binding_id=str(binding.id))
        return store

    async def _build(self, binding: VectorBindingRef) -> VectorStore:
        if binding.driver != "qdrant":
            raise VectorStoreUnavailable(f"Unknown vector store driver: {binding.driver!r}")
        from qdrant_client import AsyncQdrantClient

        from cairn.core.config import get_settings
        from cairn.vectorstore.qdrant import QdrantVectorStore

        settings = get_settings().qdrant
        with warnings.catch_warnings():
            # The client warns when an API key travels over plain HTTP. Inside
            # the Compose network that is the configured, intended transport.
            warnings.simplefilter("ignore", UserWarning)
            client = AsyncQdrantClient(
                url=settings.url,
                api_key=settings.api_key.get_secret_value() if settings.api_key else None,
                timeout=settings.timeout_s,
            )
        return QdrantVectorStore(client)

    async def close(self) -> None:
        for store in self._instances.values():
            await store.close()
        self._instances.clear()


_registry: VectorStoreRegistry | None = None


def get_vector_registry() -> VectorStoreRegistry:
    global _registry
    if _registry is None:
        _registry = VectorStoreRegistry()
    return _registry


def reset_vector_registry() -> None:
    global _registry
    _registry = None
