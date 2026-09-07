"""Driver conformance — every ObjectStore implementation, same assertions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from tests.fakes.objectstore import FakeObjectStore

from cairn.core.errors import ValidationFailed
from cairn.objectstore.errors import ObjectNotFound, ObjectTooLarge
from cairn.objectstore.keys import ObjectKeys
from cairn.objectstore.local import LocalObjectStore


@pytest.fixture(params=["fake", "local"])
async def store(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[object]:
    instance = FakeObjectStore() if request.param == "fake" else LocalObjectStore(tmp_path)
    yield instance
    await instance.close()


async def collect(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


async def stream_of(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# --- round trip --------------------------------------------------------------


async def test_put_head_get_round_trips(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M04-01."""
    info = await store.put("a/b/c.txt", b"hello world", content_type="text/plain")
    assert info.size == 11

    head = await store.head("a/b/c.txt")
    assert head is not None and head.size == 11

    assert await collect(store.get("a/b/c.txt")) == b"hello world"


async def test_streaming_put_reassembles_exactly(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M04-02 — the path a 200 MB PDF takes. Buffering it instead would be
    1.6 GB of RSS across eight concurrent uploads."""
    await store.put("big.bin", stream_of(b"aaa", b"bbb", b"ccc"))
    assert await collect(store.get("big.bin")) == b"aaabbbccc"


async def test_put_overwrites(store) -> None:  # type: ignore[no-untyped-def]
    await store.put("k", b"first")
    await store.put("k", b"second")
    assert await collect(store.get("k")) == b"second"


async def test_binary_and_unicode_keys_round_trip(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M04-12 — a document titled in Chinese must not break key handling."""
    await store.put("ws/kb/文档 名称.pdf", bytes(range(256)))
    assert await collect(store.get("ws/kb/文档 名称.pdf")) == bytes(range(256))


# --- reads that must fail ----------------------------------------------------


async def test_get_missing_raises(store) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ObjectNotFound):
        await collect(store.get("nope"))


async def test_head_missing_returns_none(store) -> None:  # type: ignore[no-untyped-def]
    assert await store.head("nope") is None


async def test_get_bytes_enforces_the_cap(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M04-03. The cap is required, not optional, so no call site can
    accidentally materialise an object of unknown size."""
    await store.put("big", b"x" * 5000)
    with pytest.raises(ObjectTooLarge):
        await store.get_bytes("big", max_bytes=1024)
    assert await store.get_bytes("big", max_bytes=10_000) == b"x" * 5000


# --- deletes -----------------------------------------------------------------


async def test_delete_is_idempotent(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M04-04 — a purge that is retried must not fail on the second pass."""
    await store.put("k", b"v")
    assert await store.delete("k") is True
    assert await store.delete("k") is False


async def test_delete_prefix_removes_all_and_only_matching(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M04-05 — this is how a knowledge base purge works (FR-C-08)."""
    await store.put("ws1/kb1/originals/a", b"a")
    await store.put("ws1/kb1/parsed/b", b"b")
    await store.put("ws1/kb2/originals/c", b"c")
    await store.put("ws2/kb1/originals/d", b"d")

    removed = await store.delete_prefix("ws1/kb1/")
    assert removed == 2

    assert await store.head("ws1/kb1/originals/a") is None
    assert await store.head("ws1/kb2/originals/c") is not None
    assert await store.head("ws2/kb1/originals/d") is not None


async def test_delete_prefix_of_nothing_is_zero(store) -> None:  # type: ignore[no-untyped-def]
    assert await store.delete_prefix("absent/") == 0


# --- listing -----------------------------------------------------------------


async def test_list_filters_by_prefix_and_paginates(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M04-06."""
    for index in range(5):
        await store.put(f"ws/kb/{index:02d}", b"x")
    await store.put("other/zz", b"x")

    first = await store.list("ws/kb/", limit=3)
    assert len(first.items) == 3
    assert first.next_cursor is not None

    second = await store.list("ws/kb/", limit=3, cursor=first.next_cursor)
    assert len(second.items) == 2
    assert second.next_cursor is None

    keys = [item.key for item in (*first.items, *second.items)]
    assert keys == sorted(keys)
    assert all(key.startswith("ws/kb/") for key in keys)


# --- presigning --------------------------------------------------------------


async def test_presigned_get_returns_a_url(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M04-07 / NFR-SEC-07 — uploads are served by signed URL, never proxied
    through the API or served from the app origin."""
    await store.put("ws/kb/icon/x.png", b"png")
    url = await store.presigned_get("ws/kb/icon/x.png", timedelta(minutes=15))
    assert isinstance(url, str) and url


async def test_health(store) -> None:  # type: ignore[no-untyped-def]
    assert await store.health() is True


# --- local-only: path confinement --------------------------------------------


@pytest.mark.parametrize("key", ["../escape", "a/../../escape", "/absolute", ""])
async def test_local_store_confines_keys_to_its_root(tmp_path: Path, key: str) -> None:
    """TC-M04-08 — a key is user-influenced (it embeds a filename), so path
    traversal here would let an upload write anywhere the process can reach."""
    store = LocalObjectStore(tmp_path)
    with pytest.raises(ValidationFailed):
        await store.put(key, b"x")


async def test_local_store_put_is_atomic(tmp_path: Path) -> None:
    """A crash mid-write must not leave a truncated object that later reads as
    valid. The temp-then-rename means readers see all of it or none of it."""
    store = LocalObjectStore(tmp_path)

    async def exploding() -> AsyncIterator[bytes]:
        yield b"partial"
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        await store.put("k", exploding())

    assert await store.head("k") is None
    assert not list(tmp_path.rglob("*.partial"))  # noqa: ASYNC240 — test assertion


async def test_local_presigned_signature_is_verified(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path)
    await store.put("k", b"v")
    url = await store.presigned_get("k", timedelta(minutes=5))

    expires = int(url.split("expires=")[1].split("&")[0])
    signature = url.split("signature=")[1]

    assert store.verify_presigned("k", expires, signature) is True
    # A signature for one key must not authorise another.
    assert store.verify_presigned("other", expires, signature) is False
    assert store.verify_presigned("k", expires, "0" * 64) is False


# --- key layout --------------------------------------------------------------


def test_key_layout_is_prefix_purgeable() -> None:
    """Every key a knowledge base owns must live under its prefix, or
    `delete_prefix` during a purge would leave orphaned storage nobody can find.
    """
    from uuid import uuid4

    ws, kb, doc = uuid4(), uuid4(), uuid4()
    prefix = ObjectKeys.kb_prefix(ws, kb)

    for key in (
        ObjectKeys.original(ws, kb, "deadbeef"),
        ObjectKeys.parsed_markdown(ws, kb, doc, 1),
        ObjectKeys.parsed_layout(ws, kb, doc, 1),
        ObjectKeys.asset(ws, kb, doc, 0, "png"),
        ObjectKeys.icon(ws, kb, "logo.png"),
    ):
        assert key.startswith(prefix), key
