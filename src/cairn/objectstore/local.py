"""Filesystem object store.

For development and the ``small`` deployment preset. Production uses S3 or a
compatible service (MinIO, OSS, COS) via :mod:`cairn.objectstore.s3`.

Two properties are load-bearing even here, because tests run against this driver
and would otherwise let a bug through to the S3 one:

* ``put`` is atomic — write to a temp file, then rename. A crash mid-write must
  not leave a truncated object that later reads as valid.
* Keys are confined to the root. A key containing ``..`` cannot escape.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anyio

from cairn.core.errors import ValidationFailed
from cairn.core.logging import get_logger
from cairn.core.time import utcnow
from cairn.objectstore.base import ObjectInfo, ObjectPage, Uploadable
from cairn.objectstore.errors import ObjectNotFound, ObjectTooLarge

__all__ = ["LocalObjectStore"]

log = get_logger(__name__)

_CHUNK = 1024 * 1024


class LocalObjectStore:
    def __init__(self, root: str | Path, *, signing_key: bytes | None = None) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._signing_key = signing_key or os.urandom(32)

    # --- path safety --------------------------------------------------------

    def _path(self, key: str) -> Path:
        if not key or key.startswith("/"):
            raise ValidationFailed("Object key must be a non-empty relative path.")
        candidate = (self._root / key).resolve()
        # `..` in a key would otherwise write anywhere the process can reach.
        if not candidate.is_relative_to(self._root):
            raise ValidationFailed("Object key escapes the storage root.")
        return candidate

    # --- writes -------------------------------------------------------------

    async def put(
        self,
        key: str,
        data: Uploadable,
        *,
        content_type: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        path = self._path(key)
        await anyio.to_thread.run_sync(lambda: path.parent.mkdir(parents=True, exist_ok=True))

        digest = hashlib.sha256()
        size = 0

        # Temp file in the same directory so the rename is atomic (same filesystem).
        fd, temp_name = await anyio.to_thread.run_sync(
            lambda: tempfile.mkstemp(dir=str(path.parent), suffix=".partial")
        )
        temp_path = Path(temp_name)
        try:
            async with await anyio.open_file(fd, "wb", closefd=True) as handle:
                if isinstance(data, bytes):
                    await handle.write(data)
                    digest.update(data)
                    size = len(data)
                else:
                    async for chunk in data:
                        await handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            await anyio.to_thread.run_sync(lambda: shutil.move(str(temp_path), str(path)))
        except BaseException:
            await anyio.to_thread.run_sync(lambda: temp_path.unlink(missing_ok=True))
            raise

        return ObjectInfo(
            key=key,
            size=size,
            etag=digest.hexdigest(),
            content_type=content_type,
            last_modified=utcnow(),
            metadata=dict(metadata or {}),
        )

    # --- reads --------------------------------------------------------------

    async def get(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFound(f"No object at {key!r}.")
        async with await anyio.open_file(path, "rb") as handle:
            while chunk := await handle.read(_CHUNK):
                yield chunk

    async def get_bytes(self, key: str, *, max_bytes: int) -> bytes:
        info = await self.head(key)
        if info is None:
            raise ObjectNotFound(f"No object at {key!r}.")
        if info.size > max_bytes:
            raise ObjectTooLarge(
                f"Object {key!r} is {info.size} bytes, above the {max_bytes} byte limit."
            )
        chunks: list[bytes] = []
        total = 0
        async for chunk in self.get(key):
            total += len(chunk)
            if total > max_bytes:
                raise ObjectTooLarge(f"Object {key!r} exceeds the {max_bytes} byte limit.")
            chunks.append(chunk)
        return b"".join(chunks)

    async def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        stat = await anyio.to_thread.run_sync(path.stat)
        return ObjectInfo(
            key=key,
            size=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    # --- deletes ------------------------------------------------------------

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False  # Deletion is idempotent; a missing key is not an error.
        await anyio.to_thread.run_sync(path.unlink)
        return True

    async def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix) if prefix else self._root
        if not base.exists():
            return 0

        def _remove() -> int:
            removed = 0
            if base.is_file():
                base.unlink()
                return 1
            for path in sorted(base.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                    removed += 1
                elif path.is_dir():
                    # rmdir on a non-empty directory is expected: children are
                    # removed first by the reverse-sorted walk, but a directory
                    # holding an unrelated file must survive.
                    with contextlib.suppress(OSError):
                        path.rmdir()
            with contextlib.suppress(OSError):
                base.rmdir()
            return removed

        return await anyio.to_thread.run_sync(_remove)

    # --- listing ------------------------------------------------------------

    async def list(
        self, prefix: str, *, limit: int = 1000, cursor: str | None = None
    ) -> ObjectPage:
        base = self._root / prefix if prefix else self._root

        def _collect() -> list[ObjectInfo]:
            if not base.exists():
                return []
            found: list[ObjectInfo] = []
            for path in sorted(base.rglob("*")):
                if not path.is_file() or path.name.endswith(".partial"):
                    continue
                stat = path.stat()
                found.append(
                    ObjectInfo(
                        key=str(path.relative_to(self._root)).replace(os.sep, "/"),
                        size=stat.st_size,
                        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    )
                )
            return found

        items = await anyio.to_thread.run_sync(_collect)
        if cursor is not None:
            items = [item for item in items if item.key > cursor]
        page = items[:limit]
        next_cursor = page[-1].key if len(items) > limit and page else None
        return ObjectPage(items=tuple(page), next_cursor=next_cursor)

    # --- presigning ---------------------------------------------------------

    async def presigned_get(self, key: str, ttl: timedelta) -> str:
        """HMAC-signed local URL. **Development only.**

        Real deployments use S3 presigning. This exists so the upload/download
        code path is identical in development rather than branching on driver.
        """
        expires = int((utcnow() + ttl).timestamp())
        signature = hmac.new(
            self._signing_key, f"{key}:{expires}".encode(), hashlib.sha256
        ).hexdigest()
        return f"/internal/objects/{key}?expires={expires}&signature={signature}"

    def verify_presigned(self, key: str, expires: int, signature: str) -> bool:
        if expires < int(utcnow().timestamp()):
            return False
        expected = hmac.new(
            self._signing_key, f"{key}:{expires}".encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def health(self) -> bool:
        return self._root.is_dir() and os.access(self._root, os.W_OK)

    async def close(self) -> None:
        return None
