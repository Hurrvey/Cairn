"""Driver resolution.

A ``storage_binding`` row names a driver and its configuration; this turns that
into a connected client, cached per binding so every knowledge base sharing a
backend also shares its connection pool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from uuid import UUID

from cairn.core.config import get_settings
from cairn.core.logging import get_logger
from cairn.core.secrets import SealedCredential
from cairn.objectstore.base import ObjectStore
from cairn.objectstore.errors import ObjectStoreUnavailable
from cairn.objectstore.local import LocalObjectStore
from cairn.objectstore.managed import ManagedObjectStore

__all__ = ["ObjectBindingRef", "ObjectStoreRegistry", "get_object_registry"]

log = get_logger(__name__)


class ObjectBindingRef:
    """What the catalog hands over: driver plus non-secret configuration.

    Credentials arrive separately, decrypted at point of use, so a binding can
    be logged without leaking anything (NFR-SEC-03).
    """

    __slots__ = ("config", "credential", "credentials", "driver", "id", "workspace_id")

    def __init__(
        self,
        binding_id: UUID,
        driver: str,
        config: dict[str, Any],
        credentials: dict[str, str] | None = None,
        *,
        credential: SealedCredential | None = None,
        workspace_id: UUID | None = None,
    ) -> None:
        self.id = binding_id
        self.driver = driver
        self.config = config
        self.credentials = credentials or {}
        self.credential = credential
        self.workspace_id = workspace_id

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"ObjectBindingRef(driver={self.driver!r}, id={self.id})"


class ObjectStoreRegistry:
    def __init__(self) -> None:
        self._instances: dict[UUID, ObjectStore] = {}
        self._fingerprints: dict[UUID, str] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._closed = False

    async def for_binding(self, binding: ObjectBindingRef) -> ObjectStore:
        async with self._locks.setdefault(binding.id, asyncio.Lock()):
            if self._closed:
                raise ObjectStoreUnavailable("Object storage is shutting down.")
            return await self._resolve(binding)

    async def _resolve(self, binding: ObjectBindingRef) -> ObjectStore:
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "driver": binding.driver,
                    "config": binding.config,
                    "credential": binding.credential.model_dump(mode="json")
                    if binding.credential
                    else binding.credentials,
                    "workspace": str(binding.workspace_id),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cached = self._instances.get(binding.id)
        if cached is not None and self._fingerprints.get(binding.id) == fingerprint:
            return cached

        if binding.driver in {"s3", "minio", "oss", "cos"}:
            from cairn.objectstore.s3 import S3Config, S3ObjectStore

            credentials = binding.credentials
            if binding.credential is not None:
                if binding.workspace_id is None:
                    raise ObjectStoreUnavailable("Storage credential workspace is missing.")
                credentials = json.loads(
                    binding.credential.open(
                        get_settings().master_key, binding.workspace_id
                    ).get_secret_value()
                )
            store: ObjectStore = await S3ObjectStore.open(
                S3Config.model_validate(binding.config), credentials
            )
        else:
            store = self._build(binding)
        try:
            if not await store.health():
                raise ObjectStoreUnavailable(
                    f"The {binding.driver} object store for binding {binding.id} is not reachable."
                )
        except BaseException:
            await store.close()
            raise
        store = ManagedObjectStore(store)
        self._instances[binding.id] = store
        self._fingerprints[binding.id] = fingerprint
        if cached is not None:
            await cached.close()
        log.info("objectstore.bound", driver=binding.driver, binding_id=str(binding.id))
        return store

    def _build(self, binding: ObjectBindingRef) -> ObjectStore:
        if binding.driver == "local":
            root = binding.config.get("path") or get_settings().objectstore.local_path
            return LocalObjectStore(root)
        raise ObjectStoreUnavailable(f"Unknown object store driver: {binding.driver!r}")

    async def close(self) -> None:
        self._closed = True
        for id_, lock in list(self._locks.items()):
            async with lock:
                store = self._instances.get(id_)
                if store is not None:
                    await store.close()
        self._instances.clear()
        self._fingerprints.clear()


_registry: ObjectStoreRegistry | None = None


def get_object_registry() -> ObjectStoreRegistry:
    global _registry
    if _registry is None:
        _registry = ObjectStoreRegistry()
    return _registry


def reset_object_registry() -> None:
    global _registry
    _registry = None
