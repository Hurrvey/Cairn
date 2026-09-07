"""Idempotency for resource-creating requests (FR-I-10).

The body fingerprint matters: without it, a client that reuses a key with a
different payload silently receives the *first* resource back, which is a far
worse outcome than an error.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from cairn.core.cache import Cache
from cairn.core.errors import IdempotencyKeyReused, RequestInProgress

__all__ = ["IdempotencyGuard", "fingerprint", "idempotent"]

_TTL_SECONDS = 86_400  # 24h — matches the documented replay window


def fingerprint(*, method: str, path: str, principal_id: str, body: bytes) -> str:
    digest = hashlib.sha256()
    for part in (method, path, principal_id):
        digest.update(part.encode())
        digest.update(b"\x00")
    digest.update(body)
    return digest.hexdigest()


@dataclass
class IdempotencyGuard:
    key: str | None
    replayed: bool
    stored_response: dict[str, Any] | None

    async def store(self, response: dict[str, Any]) -> None:
        """Persist the response so a replay returns it verbatim."""
        if self._cache is None or self.key is None:
            return
        record = {"state": "completed", "fp": self._fingerprint, "response": response}
        await self._cache.set(self.key, json.dumps(record).encode(), _TTL_SECONDS)

    _cache: Cache | None = None
    _fingerprint: str = ""


@asynccontextmanager
async def idempotent(
    cache: Cache,
    *,
    key: str | None,
    method: str,
    path: str,
    principal_id: str,
    body: bytes,
) -> AsyncIterator[IdempotencyGuard]:
    """Guard a creating request against duplicate execution.

    * unseen key                    -> proceed, then ``guard.store(response)``
    * same key, same body, done     -> replay the stored response
    * same key, same body, running  -> 409 REQUEST_IN_PROGRESS
    * same key, different body      -> 422 IDEMPOTENCY_KEY_REUSED
    """
    if key is None:
        yield IdempotencyGuard(key=None, replayed=False, stored_response=None)
        return

    cache_key = f"idem:{principal_id}:{key}"
    fp = fingerprint(method=method, path=path, principal_id=principal_id, body=body)

    claimed = await cache.set_if_absent(
        cache_key, json.dumps({"state": "in_progress", "fp": fp}).encode(), _TTL_SECONDS
    )

    if not claimed:
        raw = await cache.get(cache_key)
        record = json.loads(raw) if raw else {}
        if record.get("fp") != fp:
            raise IdempotencyKeyReused(
                "This idempotency key was already used with a different request body."
            )
        if record.get("state") == "in_progress":
            raise RequestInProgress("A request with this idempotency key is still running.")
        guard = IdempotencyGuard(key=cache_key, replayed=True, stored_response=record["response"])
        guard._cache = cache
        guard._fingerprint = fp
        yield guard
        return

    guard = IdempotencyGuard(key=cache_key, replayed=False, stored_response=None)
    guard._cache = cache
    guard._fingerprint = fp
    try:
        yield guard
    except Exception:
        # Release the claim so a corrected retry is not blocked for 24 hours.
        await cache.delete(cache_key)
        raise
