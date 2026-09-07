"""Rate limiting (FR-I-07, NFR-SEC-13).

Headers go on **every** response, not only 429s. A machine client that can see
its own budget throttles itself; one that cannot retries into a wall and takes
the service down with it.
"""

from __future__ import annotations

from dataclasses import dataclass

from cairn.authz.model import Principal
from cairn.core.cache import Cache, get_cache
from cairn.core.errors import RateLimitExceeded
from cairn.core.logging import get_logger
from cairn.core.telemetry import ratelimit_rejected_total
from cairn.core.time import utcnow

__all__ = ["RateLimitState", "RateLimiter", "get_rate_limiter"]

log = get_logger(__name__)

_WINDOW_SECONDS = 60
DEFAULT_RPM = 600


@dataclass(frozen=True, slots=True)
class RateLimitState:
    limit: int
    remaining: int
    reset_seconds: int

    def headers(self) -> dict[str, str]:
        """RFC 9331 field names."""
        return {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.reset_seconds),
        }


class RateLimiter:
    def __init__(self, cache: Cache | None = None, *, default_rpm: int = DEFAULT_RPM) -> None:
        self._cache = cache
        self._default_rpm = default_rpm

    @property
    def cache(self) -> Cache:
        return self._cache or get_cache()

    async def check(self, principal: Principal) -> RateLimitState:
        limit = principal.rate_limit_rpm or self._default_rpm
        return await self._consume(f"rl:principal:{principal.id}", limit, principal.type)

    async def check_ip(self, ip: str, *, limit: int) -> RateLimitState:
        return await self._consume(f"rl:ip:{ip}", limit, "anonymous")

    async def _consume(self, key: str, limit: int, principal_type: str) -> RateLimitState:
        now = int(utcnow().timestamp())
        window = now // _WINDOW_SECONDS
        reset = (window + 1) * _WINDOW_SECONDS - now

        try:
            count = await self.cache.incr(f"{key}:{window}", _WINDOW_SECONDS * 2)
        except Exception as exc:
            # Fail OPEN. A rate limiter is a protection, not a gate: an
            # unavailable Redis must not take down every authenticated request.
            # Availability failures are already visible via the readiness probe.
            log.warning("ratelimit.unavailable", error=type(exc).__name__)
            return RateLimitState(limit=limit, remaining=limit, reset_seconds=reset)

        state = RateLimitState(limit=limit, remaining=max(0, limit - count), reset_seconds=reset)

        if count > limit:
            ratelimit_rejected_total.labels(principal_type=principal_type).inc()
            raise RateLimitExceeded(
                "Rate limit exceeded. Retry after the window resets.", retry_after=reset
            )
        return state


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def reset_rate_limiter() -> None:
    global _limiter
    _limiter = None
