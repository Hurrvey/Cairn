"""Coordinator acceptance for operational runtime and deletion lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from apps.worker.main import build_worker
from cairn.authz.model import Principal
from cairn.core.cache import get_cache
from cairn.core.config import get_settings
from cairn.core.errors import NotFound
from cairn.vectorstore.base import Namespace

__all__ = ["pipeline_admin"]


def test_maintenance_factory_consumes_purge_and_retirement() -> None:
    assert {"document.purge", "kb.purge", "index.drop"} <= build_worker("maintain")._handlers.keys()


async def test_deleting_document_removes_real_vectors_and_original(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, _source, key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    assert registration.document is not None
    pipeline, _provider, objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert await vectors.count(Namespace(kb.id, 1)) > 0
    await catalog.delete_document(pipeline_admin, registration.document.id)
    await _execute_one(build_worker("maintain"))
    assert await vectors.count(Namespace(kb.id, 1)) == 0
    assert await objects.head(key) is None
    try:
        document = await catalog.get_document(registration.document.id)
    except NotFound:
        return
    assert document.state != "deleting", "Document purge must finish, not remain queued forever"


async def test_deleted_kb_cannot_be_republished_by_later_refresh(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.delete_kb(pipeline_admin, kb.id)
    assert await get_cache().get(f"kb:runtime:{kb.id}") is None
    await catalog.publish_runtime(kb.id)
    cached = await get_cache().get(f"kb:runtime:{kb.id}")
    assert cached is None


async def test_scheduler_repairs_evicted_runtime_without_manual_task_enqueue(
    pipeline_admin: Principal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.catalog.maintenance import build_runtime_refresh_scheduler

    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    key = f"kb:runtime:{kb.id}"
    await get_cache().delete(key)
    published = asyncio.Event()
    original_set = get_cache().set

    async def observed_set(cache_key: str, value: bytes, ttl: int) -> None:
        await original_set(cache_key, value, ttl)
        if cache_key == key:
            published.set()

    monkeypatch.setattr(get_cache(), "set", observed_set)
    monkeypatch.setattr(get_settings().tasks, "runtime_refresh_interval_s", 0.1)
    stop = asyncio.Event()
    scheduler = asyncio.create_task(build_runtime_refresh_scheduler().run(stop))
    try:
        await asyncio.wait_for(published.wait(), timeout=5)
        assert await get_cache().get(key) is not None
        published.clear()
        await get_cache().delete(key)
        await asyncio.wait_for(published.wait(), timeout=5)
        assert await get_cache().get(key) is not None
    finally:
        stop.set()
        await asyncio.wait_for(scheduler, timeout=5)
