"""Cache / ephemeral state.

Redis is **never a source of truth**. Everything here is reconstructible from
PostgreSQL; a cold or unavailable cache costs latency, not correctness.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from redis.asyncio import Redis

from cairn.core.config import Settings, get_settings

__all__ = ["Cache", "InMemoryCache", "RedisCache", "close_cache", "get_cache"]


@runtime_checkable
class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl: int) -> None: ...
    async def set_if_absent(self, key: str, value: bytes, ttl: int) -> bool: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, ttl: int) -> int: ...
    async def ping(self) -> bool: ...


class RedisCache:
    def __init__(self, client: Redis) -> None:
        self._client = client

    async def get(self, key: str) -> bytes | None:
        # The client is created with decode_responses=False, so this is bytes in
        # practice; the union is narrowed rather than asserted so a future config
        # change cannot silently return str where callers expect bytes.
        value = await self._client.get(key)
        if value is None:
            return None
        return value if isinstance(value, bytes) else str(value).encode()

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        await self._client.set(key, value, ex=ttl)

    async def set_if_absent(self, key: str, value: bytes, ttl: int) -> bool:
        return bool(await self._client.set(key, value, ex=ttl, nx=True))

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def incr(self, key: str, ttl: int) -> int:
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl)
            result = await pipe.execute()
        return int(result[0])

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def close(self) -> None:
        await self._client.aclose()


class InMemoryCache:
    """Process-local cache for unit tests and single-process development.

    No expiry sweeping — entries are checked lazily on read. Not suitable for
    production: it is per-process, so N replicas would each hold a different view.
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[bytes, float]] = {}

    def _now(self) -> float:
        from cairn.core.time import utcnow

        return utcnow().timestamp()

    async def get(self, key: str) -> bytes | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= self._now():
            self._data.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        self._data[key] = (value, self._now() + ttl)

    async def set_if_absent(self, key: str, value: bytes, ttl: int) -> bool:
        if await self.get(key) is not None:
            return False
        await self.set(key, value, ttl)
        return True

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        current = await self.get(key)
        value = int(current) + 1 if current else 1
        await self.set(key, str(value).encode(), ttl)
        return value

    async def ping(self) -> bool:
        return True


_cache: RedisCache | None = None


def get_cache(settings: Settings | None = None) -> Cache:
    global _cache
    if _cache is None:
        cfg = settings or get_settings()
        _cache = RedisCache(Redis.from_url(cfg.redis_url, decode_responses=False))
    return _cache


async def close_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
    _cache = None
