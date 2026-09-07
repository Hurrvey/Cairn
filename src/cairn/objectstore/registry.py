"""Driver resolution.

A ``storage_binding`` row names a driver and its configuration; this turns that
into a connected client, cached per binding so every knowledge base sharing a
backend also shares its connection pool.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from cairn.core.config import get_settings
from cairn.core.logging import get_logger
from cairn.objectstore.base import ObjectStore
from cairn.objectstore.errors import ObjectStoreUnavailable
from cairn.objectstore.local import LocalObjectStore

__all__ = ["ObjectBindingRef", "ObjectStoreRegistry", "get_object_registry"]

log = get_logger(__name__)


class ObjectBindingRef:
    """What the catalog hands over: driver plus non-secret configuration.

    Credentials arrive separately, decrypted at point of use, so a binding can
    be logged without leaking anything (NFR-SEC-03).
    """

    __slots__ = ("config", "credentials", "driver", "id")

    def __init__(
        self,
        binding_id: UUID,
        driver: str,
        config: dict[str, Any],
        credentials: dict[str, str] | None = None,
    ) -> None:
        self.id = binding_id
        self.driver = driver
        self.config = config
        self.credentials = credentials or {}

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"ObjectBindingRef(driver={self.driver!r}, id={self.id})"


class ObjectStoreRegistry:
    def __init__(self) -> None:
        self._instances: dict[UUID, ObjectStore] = {}

    async def for_binding(self, binding: ObjectBindingRef) -> ObjectStore:
        cached = self._instances.get(binding.id)
        if cached is not None:
            return cached

        store = self._build(binding)
        if not await store.health():
            raise ObjectStoreUnavailable(
                f"The {binding.driver} object store for binding {binding.id} is not reachable."
            )
        self._instances[binding.id] = store
        log.info("objectstore.bound", driver=binding.driver, binding_id=str(binding.id))
        return store

    def _build(self, binding: ObjectBindingRef) -> ObjectStore:
        if binding.driver == "local":
            root = binding.config.get("path") or get_settings().objectstore.local_path
            return LocalObjectStore(root)
        if binding.driver in ("s3", "minio", "oss", "cos"):
            # T-M04-03. Deliberately an explicit error rather than a silent
            # fallback to local: a deployment that thinks it is writing to S3
            # and is actually writing to a container filesystem loses data on
            # the next restart.
            raise ObjectStoreUnavailable(
                f"The {binding.driver!r} object store driver is not implemented yet "
                "(T-M04-03). Use the 'local' driver until it lands."
            )
        raise ObjectStoreUnavailable(f"Unknown object store driver: {binding.driver!r}")

    async def close(self) -> None:
        for store in self._instances.values():
            await store.close()
        self._instances.clear()


_registry: ObjectStoreRegistry | None = None


def get_object_registry() -> ObjectStoreRegistry:
    global _registry
    if _registry is None:
        _registry = ObjectStoreRegistry()
    return _registry


def reset_object_registry() -> None:
    global _registry
    _registry = None
