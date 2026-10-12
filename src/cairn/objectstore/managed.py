"""Keep in-flight reads alive during credential rotation; retire idle clients."""

from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta

from cairn.objectstore.base import ObjectInfo, ObjectPage, ObjectStore, Uploadable
from cairn.objectstore.errors import ObjectStoreTransient


class ManagedObjectStore:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store
        self._active = 0
        self._retired = False
        self._closed = False

    @asynccontextmanager
    async def _lease(self) -> AsyncIterator[None]:
        if self._retired:
            raise ObjectStoreTransient(
                "Storage credentials changed; retry with the current binding."
            )
        self._active += 1
        try:
            yield
        finally:
            self._active -= 1
            await self._close_idle()

    async def _close_idle(self) -> None:
        if self._retired and self._active == 0 and not self._closed:
            self._closed = True
            await self._store.close()

    async def put(
        self,
        key: str,
        data: Uploadable,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        async with self._lease():
            return await self._store.put(key, data, content_type=content_type, metadata=metadata)

    async def get(self, key: str) -> AsyncGenerator[bytes]:
        async with self._lease():
            stream = self._store.get(key)
            try:
                async for chunk in stream:
                    yield chunk
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes:
        async with self._lease():
            return await self._store.get_bytes(key, max_bytes=max_bytes)

    async def head(self, key: str) -> ObjectInfo | None:
        async with self._lease():
            return await self._store.head(key)

    async def delete(self, key: str) -> bool:
        async with self._lease():
            return await self._store.delete(key)

    async def delete_prefix(self, prefix: str) -> int:
        async with self._lease():
            return await self._store.delete_prefix(prefix)

    async def list(
        self, prefix: str, *, limit: int = 1000, cursor: str | None = None
    ) -> ObjectPage:
        async with self._lease():
            return await self._store.list(prefix, limit=limit, cursor=cursor)

    async def presigned_get(self, key: str, ttl: timedelta) -> str:
        async with self._lease():
            return await self._store.presigned_get(key, ttl)

    async def health(self) -> bool:
        async with self._lease():
            return await self._store.health()

    async def close(self) -> None:
        self._retired = True
        await self._close_idle()
