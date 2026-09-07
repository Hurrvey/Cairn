"""The object store contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["ObjectInfo", "ObjectPage", "ObjectStore", "Uploadable"]

#: What ``put`` accepts. Bytes are a convenience for small objects; anything
#: that could be large should arrive as a stream.
Uploadable = bytes | AsyncIterator[bytes]


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    etag: str | None = None
    content_type: str | None = None
    last_modified: datetime | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObjectPage:
    items: tuple[ObjectInfo, ...]
    next_cursor: str | None = None


@runtime_checkable
class ObjectStore(Protocol):
    async def put(
        self,
        key: str,
        data: Uploadable,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo: ...

    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes: ...

    async def head(self, key: str) -> ObjectInfo | None: ...

    async def delete(self, key: str) -> bool: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def list(
        self, prefix: str, *, limit: int = 1000, cursor: str | None = None
    ) -> ObjectPage: ...

    async def presigned_get(self, key: str, ttl: timedelta) -> str: ...

    async def health(self) -> bool: ...

    async def close(self) -> None: ...
