"""In-memory ObjectStore, maintained by the M04 owner."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta

from cairn.core.time import utcnow
from cairn.objectstore.base import ObjectInfo, ObjectPage, Uploadable
from cairn.objectstore.errors import ObjectNotFound, ObjectTooLarge

__all__ = ["FakeObjectStore"]


class FakeObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, ObjectInfo]] = {}

    async def put(
        self,
        key: str,
        data: Uploadable,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        if isinstance(data, bytes):
            body = data
        else:
            chunks = [chunk async for chunk in data]
            body = b"".join(chunks)

        info = ObjectInfo(
            key=key,
            size=len(body),
            etag=hashlib.sha256(body).hexdigest(),
            content_type=content_type,
            last_modified=utcnow(),
            metadata=dict(metadata or {}),
        )
        self._objects[key] = (body, info)
        return info

    async def get(self, key: str) -> AsyncIterator[bytes]:
        entry = self._objects.get(key)
        if entry is None:
            raise ObjectNotFound(f"No object at {key!r}.")
        # Chunked so consumers exercise the streaming path they use in production.
        body = entry[0]
        for offset in range(0, max(len(body), 1), 64 * 1024):
            yield body[offset : offset + 64 * 1024]

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes:
        entry = self._objects.get(key)
        if entry is None:
            raise ObjectNotFound(f"No object at {key!r}.")
        if len(entry[0]) > max_bytes:
            raise ObjectTooLarge(f"Object {key!r} exceeds the {max_bytes} byte limit.")
        return entry[0]

    async def head(self, key: str) -> ObjectInfo | None:
        entry = self._objects.get(key)
        return entry[1] if entry else None

    async def delete(self, key: str) -> bool:
        return self._objects.pop(key, None) is not None

    async def delete_prefix(self, prefix: str) -> int:
        doomed = [key for key in self._objects if key.startswith(prefix)]
        for key in doomed:
            del self._objects[key]
        return len(doomed)

    async def list(
        self, prefix: str, *, limit: int = 1000, cursor: str | None = None
    ) -> ObjectPage:
        keys = sorted(key for key in self._objects if key.startswith(prefix))
        if cursor is not None:
            keys = [key for key in keys if key > cursor]
        page = keys[:limit]
        next_cursor = page[-1] if len(keys) > limit and page else None
        return ObjectPage(
            items=tuple(self._objects[key][1] for key in page), next_cursor=next_cursor
        )

    async def presigned_get(self, key: str, ttl: timedelta) -> str:
        expires = int((utcnow() + ttl).timestamp())
        return f"memory://{key}?expires={expires}"

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None
