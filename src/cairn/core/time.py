"""The single source of time.

Never call ``datetime.now()`` anywhere else in the codebase: tests monkeypatch
``utcnow`` to control the clock, and a stray ``datetime.now()`` silently escapes
that control.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["isoformat", "parse_iso", "utcnow", "utcnow_ms"]


def utcnow() -> datetime:
    """Current time, timezone-aware, UTC. The only clock in the system."""
    return datetime.now(UTC)


def utcnow_ms() -> int:
    """Current time as integer milliseconds since the epoch."""
    return int(utcnow().timestamp() * 1000)


def isoformat(value: datetime) -> str:
    """RFC 3339 with a ``Z`` suffix, as required by the API conventions."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    """Parse an RFC 3339 timestamp, normalising to UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def in_seconds(seconds: float) -> datetime:
    """A point in the future, for expiry computation."""
    return utcnow() + timedelta(seconds=seconds)
