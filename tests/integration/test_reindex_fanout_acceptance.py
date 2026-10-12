"""Coordinator acceptance for rebuild enrollment and stale lifecycle work."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from apps.worker.main import build_worker
from cairn.authz.model import Principal
from cairn.catalog.dto import ReindexSpec, UploadSpec
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.core.db import session_scope, transaction
from cairn.core.errors import Conflict
from cairn.ingestion.artifacts import decode_parse_manifest
from cairn.ingestion.base import ParseContext, ParsedDocument
from cairn.objectstore.local import LocalObjectStore
from cairn.vectorstore.base import Namespace
from cairn.vectorstore.filters import Compare

__all__ = ["pipeline_admin"]


def test_production_maintain_factory_consumes_reindex_fanout() -> None:
    worker = build_worker("maintain")
    assert "kb.reindex_fanout" in worker._handlers


async def test_explicit_switch_cannot_bypass_unfinished_rebuild_enrollment(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    with pytest.raises(Conflict):
        await catalog.activate_index_version(kb.id, 2)
    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version == 2


async def test_new_upload_cannot_activate_rebuild_before_existing_docs_are_enrolled(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    source = b"# New document\n\nUploaded while old documents await fanout."
    digest = sha256(source).hexdigest()
    key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{digest}"
    await LocalObjectStore(tmp_path).put(key, source, content_type="text/markdown")
    await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="during-rebuild.md",
            content_hash=digest,
            size_bytes=len(source),
            mime_type="text/markdown",
            object_key=key,
        ),
    )
    await _execute_pipeline(pipeline)
    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version == 2


async def test_delayed_failure_cannot_clear_a_newer_build(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    await catalog.fail_index_version(kb.id, 2, "first failure")
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    with suppress(Conflict):
        await catalog.fail_index_version(kb.id, 2, "delayed obsolete failure")
    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version == 3
    versions = await catalog.list_index_versions(kb.id)
    assert [(version.version, version.state) for version in versions] == [
        (3, "building"),
        (2, "failed"),
        (1, "active"),
    ]


@pytest.mark.parametrize("artifact_state", ["valid", "missing", "corrupt", "oversized"])
async def test_rebuild_reuses_parse_artifact_through_production_worker_factory(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_state: str,
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    pipeline, _provider, objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    original_parse = pipeline._resources.parsers.parse
    parse_calls: list[bytes] = []

    async def counted_parse(data: bytes, *, mime: str, ctx: ParseContext) -> ParsedDocument:
        parse_calls.append(data)
        return await original_parse(data, mime=mime, ctx=ctx)

    monkeypatch.setattr(pipeline._resources.parsers, "parse", counted_parse)
    await _execute_pipeline(pipeline)
    assert len(parse_calls) == 1
    async with session_scope() as session:
        prior_key = await session.scalar(
            text(
                "SELECT parsed_object_key FROM document_ingestion "
                "WHERE document_id=:document AND index_version=1"
            ),
            {"document": registration.document.id},
        )
    assert isinstance(prior_key, str)
    if artifact_state == "missing":
        await objects.delete(prior_key)
    elif artifact_state == "corrupt":
        await objects.put(prior_key, b"not an artifact", content_type="application/json")
    elif artifact_state == "oversized":
        pipeline._resources = replace(pipeline._resources, max_artifact_bytes=16 * 1024)
        await objects.put(prior_key, b"x" * (32 * 1024), content_type="application/json")
    monkeypatch.setattr(
        "cairn.ingestion.runtime.get_pipeline_runtime", lambda: SimpleNamespace(pipeline=pipeline)
    )
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    workers = [build_worker(queue) for queue in ("maintain", "parse", "chunk", "embed", "index")]
    for _attempt in range(20):
        for worker in workers:
            for row in await worker._claim_batch():
                await worker._execute(row)
        if (await catalog.get_kb(kb.id)).active_index_version == 2:
            break
    assert (await catalog.get_kb(kb.id)).active_index_version == 2
    assert len(parse_calls) == (1 if artifact_state == "valid" else 2)
    assert await vectors.count(Namespace(kb.id, 2)) == await vectors.count(Namespace(kb.id, 1))
    async with session_scope() as session:
        ledger = (
            await session.execute(
                text(
                    "SELECT revision, state, parsed_object_key FROM document_ingestion "
                    "WHERE document_id=:document AND index_version=2"
                ),
                {"document": registration.document.id},
            )
        ).one()
    assert ledger.revision == 1
    assert ledger.state == "indexed"
    assert ledger.parsed_object_key is not None
    manifest = decode_parse_manifest(
        await objects.get_bytes(ledger.parsed_object_key, max_bytes=1024 * 1024)
    )
    assert manifest.document_id == registration.document.id
    assert manifest.revision == 1
    assert manifest.index_version == 2


@pytest.mark.parametrize("replace_before_delete", [False, True])
async def test_deleting_an_indexed_first_batch_removes_its_points_before_switch(
    pipeline_admin: Principal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_before_delete: bool,
) -> None:
    from cairn.catalog.reindex import ReindexFanoutService

    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    source = b"# Another document\n\nThis document survives the rebuild."
    digest = sha256(source).hexdigest()
    key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{digest}"
    await LocalObjectStore(tmp_path).put(key, source, content_type="text/markdown")
    await catalog.register_upload(
        pipeline_admin,
        kb.id,
        UploadSpec(
            filename="survivor.md",
            content_hash=digest,
            size_bytes=len(source),
            mime_type="text/markdown",
            object_key=key,
        ),
    )
    pipeline, _provider, _objects, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await _execute_pipeline(pipeline)
    monkeypatch.setattr(
        "cairn.ingestion.runtime.get_pipeline_runtime", lambda: SimpleNamespace(pipeline=pipeline)
    )
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    maintain = build_worker("maintain")
    maintain._handlers["kb.reindex_fanout"] = ReindexFanoutService(batch_size=1).handle
    await _execute_one(maintain)
    await _execute_pipeline(pipeline)
    assert (await catalog.get_kb(kb.id)).active_index_version == 1
    async with session_scope() as session:
        deleted_id = await session.scalar(
            text("SELECT document_id FROM document_ingestion WHERE kb_id=:kb AND index_version=2"),
            {"kb": kb.id},
        )
    assert deleted_id is not None
    deleted_filter = Compare(field="document_id", op="$eq", value=str(deleted_id))
    assert await vectors.count(Namespace(kb.id, 2), deleted_filter) > 0
    if replace_before_delete:
        replacement = b"# Replacement\n\nDifferent text that has not reached vector indexing."
        replacement_hash = sha256(replacement).hexdigest()
        replacement_key = f"{pipeline_admin.workspace_id}/{kb.id}/originals/{replacement_hash}"
        await LocalObjectStore(tmp_path).put(
            replacement_key, replacement, content_type="text/markdown"
        )
        await CatalogIngestionFacade().start_revision(
            deleted_id,
            object_key=replacement_key,
            content_hash=replacement_hash,
            size_bytes=len(replacement),
            mime_type="text/markdown",
        )
        for queue in ("parse", "chunk"):
            await _execute_one(build_worker(queue))
    await catalog.delete_document(pipeline_admin, deleted_id)
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE task SET run_after=clock_timestamp()+interval '1 day' "
                "WHERE document_id=:document AND kind='document.purge' AND state='ready'"
            ),
            {"document": deleted_id},
        )
    workers = [maintain, *(build_worker(queue) for queue in ("parse", "chunk", "embed", "index"))]
    for _attempt in range(20):
        for worker in workers:
            for row in await worker._claim_batch():
                await worker._execute(row)
        if (await catalog.get_kb(kb.id)).active_index_version == 2:
            break
    assert (await catalog.get_kb(kb.id)).active_index_version == 2
    assert await vectors.count(Namespace(kb.id, 2), deleted_filter) == 0
    assert await vectors.count(Namespace(kb.id, 2)) > 0
