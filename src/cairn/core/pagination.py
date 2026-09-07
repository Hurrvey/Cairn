"""Cursor pagination.

Offset pagination is deliberately not offered (FR-I-11). It silently skips or
duplicates rows when the underlying set changes between pages — which, for a
document list being actively ingested, is the normal case rather than an edge
case — and it degrades linearly on deep pages.
"""

from __future__ import annotations

import base64
import json
from typing import Any, TypeVar

from pydantic import BaseModel, Field

from cairn.core.errors import ValidationFailed

__all__ = ["CursorPage", "PageRequest", "decode_cursor", "encode_cursor"]

T = TypeVar("T")

_CURSOR_VERSION = 1
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


def encode_cursor(**fields: Any) -> str:
    """Opaque, URL-safe cursor. Clients MUST NOT parse this."""
    payload = json.dumps({"v": _CURSOR_VERSION, "k": fields}, separators=(",", ":"), default=str)
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, TypeError) as exc:
        raise ValidationFailed("The pagination cursor is malformed.") from exc

    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise ValidationFailed("The pagination cursor is from an unsupported version.")
    keys = payload.get("k")
    if not isinstance(keys, dict):
        raise ValidationFailed("The pagination cursor is malformed.")
    return keys


class PageRequest(BaseModel):
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = None

    def keys(self) -> dict[str, Any]:
        return decode_cursor(self.cursor) if self.cursor else {}


class CursorPage[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False

    @classmethod
    def build(
        cls, items: list[T], *, limit: int, cursor_fields: dict[str, Any] | None = None
    ) -> CursorPage[T]:
        """Build a page from ``limit + 1`` fetched rows.

        Callers fetch one extra row; its presence is what tells us whether more
        exist, without a second COUNT query.
        """
        has_more = len(items) > limit
        page_items = items[:limit]
        next_cursor = (
            encode_cursor(**cursor_fields) if has_more and cursor_fields is not None else None
        )
        return cls(items=page_items, next_cursor=next_cursor, has_more=has_more)
