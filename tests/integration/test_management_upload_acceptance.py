"""Coordinator acceptance for UI-facing setup and actual file transfer."""

from __future__ import annotations

from pathlib import Path

import httpx
from tests.integration.test_ingestion_pipeline import _create_pipeline_document, pipeline_admin

from cairn.authz.model import Principal
from cairn.authz.service import ApiKeySpec, AuthzService
from cairn.core.ids import encode_id

__all__ = ["pipeline_admin"]


async def _key(admin: Principal, kb_id) -> str:
    authz = AuthzService()
    refreshed = await authz.principal_for_user(admin.id)
    assert refreshed is not None
    _record, raw = await authz.create_api_key(
        refreshed, ApiKeySpec(name="file-transfer", scopes=["kb:write", "kb:read"], kb_ids=[kb_id])
    )
    return raw


async def test_actual_upload_download_and_duplicate_are_scoped(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    token = await _key(pipeline_admin, kb.id)
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/v1/knowledge-bases/{encode_id('kb', kb.id)}/documents"
    source = b"# Uploaded over HTTP\n\nSaturn has rings."
    uploaded = await client.post(
        f"{path}/upload", headers=headers, files={"file": ("actual.md", source, "text/markdown")}
    )
    assert uploaded.status_code in {200, 201, 202}, uploaded.text
    body = uploaded.json()
    assert body["status"] == "accepted"
    document_id = body["document"]["id"]
    downloaded = await client.get(f"{path}/{document_id}/download", headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == source
    duplicate = await client.post(
        f"{path}/upload", headers=headers, files={"file": ("again.md", source, "text/markdown")}
    )
    assert duplicate.json()["status"] == "skipped"
    assert duplicate.json()["existing_document_id"] == document_id


async def test_upload_rejects_wrong_signature_without_registering_document(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    token = await _key(pipeline_admin, kb.id)
    before = (await catalog.get_kb(kb.id)).doc_count
    response = await client.post(
        f"/v1/knowledge-bases/{encode_id('kb', kb.id)}/documents/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("invalid.pdf", b"not a PDF", "application/pdf")},
    )
    assert response.status_code in {400, 415, 422}, response.text
    assert (await catalog.get_kb(kb.id)).doc_count == before
