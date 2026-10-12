"""PDF multipart upload through the real task, object and vector pipeline."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)
from tests.integration.test_management_upload import _admin_session
from tests.unit.ingestion.test_pdf import _text_pdf

from cairn.authz.model import Principal
from cairn.catalog.parse_view import ParseUnavailable, read_parse
from cairn.core.errors import NotFound
from cairn.core.ids import decode_id, encode_id
from cairn.vectorstore.base import Namespace

__all__ = ["pipeline_admin"]


async def test_uploaded_pdf_is_indexed_with_page_citations(
    pipeline_admin: Principal,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    catalog, kb, _, _, _ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, _, _, vectors, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    headers = await _admin_session(client)
    response = await client.post(
        f"/v1/knowledge-bases/{encode_id('kb', kb.id)}/documents/upload",
        headers=headers,
        files={
            "file": (
                "report.pdf",
                _text_pdf("The PDF acceptance planet is Saturn."),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 202, response.text
    document_id = decode_id("doc", response.json()["document"]["id"])
    await _execute_pipeline(pipeline)
    document = await catalog.get_document(document_id)
    assert document.state == "indexed"
    chunks = await catalog.list_chunks(kb.id, document_id)
    assert any("Saturn" in chunk.content for chunk in chunks)
    assert all(chunk.metadata["page"] == 1 for chunk in chunks)
    assert all(citation["bbox"] for chunk in chunks for citation in chunk.metadata["citations"])
    indexed = await vectors.fetch(Namespace(kb.id, 1), [chunk.id for chunk in chunks])
    assert indexed and "Saturn" in indexed[0].content
    public_id = encode_id("doc", document_id)
    summary = await client.get(f"/v1/documents/{public_id}/parse", headers=headers)
    assert summary.status_code == 200, summary.text
    version = summary.json()["parse_version"]
    assert len(version) == 64
    elements = await client.get(
        f"/v1/documents/{public_id}/parse/elements",
        params={"view": "active", "parse_version": version},
        headers=headers,
    )
    assert elements.status_code == 200, elements.text
    assert any("Saturn" in item["text"] for item in elements.json()["items"])
    assert all(
        citation["parse_version"] == version
        for chunk in chunks
        for citation in chunk.metadata["citations"]
    )
    preview = await client.get(
        f"/v1/documents/{public_id}/parse/pages/1/preview",
        params={"view": "active", "parse_version": version},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.content.startswith(b"\x89PNG")
    stale = await client.get(
        f"/v1/documents/{public_id}/parse/elements",
        params={"view": "active", "parse_version": "0" * 64},
        headers=headers,
    )
    assert stale.status_code == 404 and stale.json()["code"] == "PARSE_VERSION_STALE"
    query_only = replace(pipeline_admin, role="user", permissions=frozenset(),
                         resource_permissions={kb.id: frozenset({"kb:query"})})
    with pytest.raises(NotFound) as rejected:
        await read_parse(query_only, document_id, view="active", parse_version="0" * 64)
    assert rejected.value.code == "RESOURCE_NOT_FOUND"
    with pytest.raises(NotFound):
        await read_parse(replace(pipeline_admin, workspace_id=uuid4()), document_id, view="active")
    with pytest.raises(ParseUnavailable):
        reader = replace(pipeline_admin, resource_permissions={kb.id: frozenset({"kb:read"})})
        await read_parse(reader, document_id, view="candidate")
