import asyncio
from types import SimpleNamespace

import pytest

from cairn.catalog import parse_view


async def test_full_preview_slot_times_out_before_any_large_reads(monkeypatch):
    calls = []

    async def read(*args, **kwargs):
        calls.append("read")
        return SimpleNamespace(pages=1, revision=1)

    monkeypatch.setattr(parse_view, "read_parse", read)
    monkeypatch.setattr(
        parse_view,
        "PreviewSettings",
        lambda: SimpleNamespace(preview_timeout_s=0.03, preview_max_concurrency=1),
    )
    monkeypatch.setattr(parse_view, "_preview_slots", asyncio.Semaphore(0))

    async def locate(*args, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(parse_view, "locate_parse", locate, raising=False)
    started = asyncio.get_running_loop().time()
    with pytest.raises(parse_view.ParseUnavailable):
        await asyncio.wait_for(
            parse_view.preview_page(None, None, view="active", parse_version="a" * 64, page=1), 0.2
        )
    assert not calls
    assert asyncio.get_running_loop().time() - started < 0.12


async def test_cancelled_preview_releases_admitted_slot(monkeypatch):
    slots = asyncio.Semaphore(1)
    entered = asyncio.Event()
    monkeypatch.setattr(parse_view, "_preview_slots", slots)
    monkeypatch.setattr(
        parse_view,
        "PreviewSettings",
        lambda: SimpleNamespace(
            preview_timeout_s=10, preview_max_concurrency=1, preview_max_pages=5
        ),
    )

    async def locate(*args, **kwargs):
        return SimpleNamespace(pages=1, source_key="source", binding=None)

    async def resolve(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(parse_view, "locate_parse", locate)
    monkeypatch.setattr(
        parse_view, "get_object_registry", lambda: SimpleNamespace(for_binding=resolve)
    )
    task = asyncio.create_task(
        parse_view.preview_page(None, None, view="active", parse_version="a" * 64, page=1)
    )
    await asyncio.wait_for(entered.wait(), 1)
    assert slots.locked()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(slots.acquire(), 0.1)
    slots.release()


async def test_concurrent_queued_previews_share_total_deadline_and_never_read(monkeypatch):
    reads = []
    slots = asyncio.Semaphore(0)
    monkeypatch.setattr(parse_view, "_preview_slots", slots)
    monkeypatch.setattr(
        parse_view,
        "PreviewSettings",
        lambda: SimpleNamespace(preview_timeout_s=0.04, preview_max_concurrency=1),
    )

    async def locate(*args, **kwargs):
        return SimpleNamespace()

    async def resolve(*args, **kwargs):
        reads.append(True)
        raise AssertionError("read before admission")

    monkeypatch.setattr(parse_view, "locate_parse", locate)
    monkeypatch.setattr(
        parse_view, "get_object_registry", lambda: SimpleNamespace(for_binding=resolve)
    )
    start = asyncio.get_running_loop().time()
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                parse_view.preview_page(None, None, view="active", parse_version="a" * 64, page=1)
                for _ in range(4)
            ),
            return_exceptions=True,
        ),
        0.2,
    )
    assert all(isinstance(result, parse_view.ParseUnavailable) for result in results)
    assert asyncio.get_running_loop().time() - start < 0.15 and not reads


async def test_preview_supervision_failure_has_stable_unavailable_error(monkeypatch):
    import hashlib

    from cairn.core.native_process import ProcessSupervisionUnavailable

    monkeypatch.setattr(parse_view, "_preview_slots", asyncio.Semaphore(1))
    monkeypatch.setattr(
        parse_view,
        "PreviewSettings",
        lambda: SimpleNamespace(
            preview_timeout_s=1,
            preview_max_concurrency=1,
            preview_max_pages=5,
            preview_max_page_pixels=100,
            preview_max_image_bytes=100,
            preview_response_max_bytes=100,
            preview_max_document_bytes=100,
        ),
    )

    async def locate(*args, **kwargs):
        return SimpleNamespace(
            pages=1,
            source_key="source",
            binding=None,
            identity=SimpleNamespace(source_sha256=hashlib.sha256(b"source").hexdigest()),
        )

    async def read(*args, **kwargs):
        return b"source"

    async def resolve(*args, **kwargs):
        return SimpleNamespace(get_bytes=read)

    async def unavailable(*args, **kwargs):
        raise ProcessSupervisionUnavailable()

    monkeypatch.setattr(parse_view, "locate_parse", locate)
    monkeypatch.setattr(
        parse_view, "get_object_registry", lambda: SimpleNamespace(for_binding=resolve)
    )
    monkeypatch.setattr(parse_view, "run_bounded", unavailable)
    with pytest.raises(parse_view.ParseUnavailable):
        await parse_view.preview_page(None, None, view="active", parse_version="a" * 64, page=1)
