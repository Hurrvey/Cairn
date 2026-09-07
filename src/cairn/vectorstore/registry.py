"""Vector driver resolution.

pgvector shares the application's engine rather than opening its own pool: on
the ``small`` preset the vectors live in the same database as everything else,
and a second pool would double the connection count for no benefit.
"""

from __future__ import annotations

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
            raise VectorStoreUnavailable(
                f"The {binding.driver} vector store is not reachable: {status.detail}"
            )
        self._instances[binding.id] = store
        log.info("vectorstore.bound", driver=binding.driver, binding_id=str(binding.id))
        return store

    async def _build(self, binding: VectorBindingRef) -> VectorStore:
        if binding.driver == "pgvector":
            from cairn.core.db import get_engine
            from cairn.vectorstore.pgvector import PgVectorStore, ensure_extension

            engine = get_engine()
            await ensure_extension(engine)
            return PgVectorStore(
                engine,
                default_text_search_config=str(binding.config.get("text_search_config", "simple")),
            )
        if binding.driver == "qdrant":
            # T-M05-10, Phase 5. An explicit error beats silently using pgvector:
            # a deployment sized for Qdrant would hit pgvector's ceiling in
            # production rather than at configuration time.
            raise VectorStoreUnavailable(
                "The Qdrant driver is not implemented yet (T-M05-10). "
                "Use the 'pgvector' driver until it lands."
            )
        raise VectorStoreUnavailable(f"Unknown vector store driver: {binding.driver!r}")

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
