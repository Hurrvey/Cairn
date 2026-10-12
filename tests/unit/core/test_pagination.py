"""Cursor pagination — TC-M00-11."""

from __future__ import annotations

import pytest

from cairn.core.errors import ValidationFailed
from cairn.core.pagination import CursorPage, PageRequest, decode_cursor, encode_cursor


def test_cursor__round_trips() -> None:
    cursor = encode_cursor(created_at="2026-08-28T14:31:07Z", id="doc_01HQ")
    assert decode_cursor(cursor) == {"created_at": "2026-08-28T14:31:07Z", "id": "doc_01HQ"}


def test_cursor__is_url_safe_and_unpadded() -> None:
    cursor = encode_cursor(id="x" * 40)
    assert "=" not in cursor
    assert "+" not in cursor and "/" not in cursor


@pytest.mark.parametrize("value", ["", "!!!not-base64!!!", "eyJ2Ijo5OSwiayI6e319"])
def test_cursor__rejects_malformed_or_wrong_version(value: str) -> None:
    with pytest.raises(ValidationFailed):
        decode_cursor(value)


def test_page_request__enforces_the_limit_ceiling() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PageRequest(limit=1000)


def test_build__signals_more_from_the_extra_row() -> None:
    """Callers fetch limit+1; its presence answers has_more without a COUNT."""
    rows = list(range(11))
    page: CursorPage[int] = CursorPage.build(rows, limit=10, cursor_fields={"id": 10})
    assert page.items == list(range(10))
    assert page.has_more is True
    assert page.next_cursor is not None


def test_build__terminates_cleanly_on_the_last_page() -> None:
    page: CursorPage[int] = CursorPage.build([1, 2, 3], limit=10, cursor_fields={"id": 3})
    assert page.items == [1, 2, 3]
    assert page.has_more is False
    assert page.next_cursor is None
