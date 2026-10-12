from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.catalog.parse_view import ParseUnavailable, read_parse
from cairn.core.db import session_scope
from cairn.core.errors import CairnError
from cairn.core.parse_artifact import decode_payload, encode_payload
from cairn.ingestion.pipeline import register_pipeline_handlers
from cairn.tasks.worker import TaskWorker

__all__ = ["pipeline_admin"]


@pytest.mark.parametrize("field", ["document_id", "revision", "source_content_hash"])
async def test_content_rejects_valid_checksum_wrong_sql_identity(
    pipeline_admin, tmp_path: Path, field
):
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, _provider, store, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    actor = replace(pipeline_admin, resource_permissions={kb.id: frozenset({"kb:read"})})
    async with session_scope() as session:
        key = await session.scalar(
            text("SELECT parsed_object_key FROM document WHERE id=:id"),
            {"id": registration.document.id},
        )
    payload = decode_payload(await store.get_bytes(key, max_bytes=10**6))
    payload[field] = (
        str(uuid4()) if field == "document_id" else 9 if field == "revision" else "e" * 64
    )
    await store.put(key, encode_payload(payload, version=2), content_type="application/json")
    with pytest.raises(CairnError):
        await catalog.document_content(actor, registration.document.id)


async def test_current_content_can_precede_active_publication_without_parse_fallback(
    pipeline_admin, tmp_path: Path
):
    catalog, kb, registration, _source, key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    pipeline, _provider, store, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    replacement = b"Replacement revision content."
    from hashlib import sha256

    newkey = key + "-replacement"
    await store.put(newkey, replacement)
    await CatalogIngestionFacade().start_revision(
        registration.document.id,
        object_key=newkey,
        content_hash=sha256(replacement).hexdigest(),
        size_bytes=len(replacement),
        mime_type="text/markdown",
    )
    worker = TaskWorker("parse", concurrency=1)
    register_pipeline_handlers(worker, pipeline)
    await _execute_one(worker)
    actor = replace(pipeline_admin, resource_permissions={kb.id: frozenset({"kb:read"})})
    _view, body = await catalog.document_content(actor, registration.document.id)
    assert body == replacement.decode()
    with pytest.raises(ParseUnavailable):
        await read_parse(actor, registration.document.id, view="active")
