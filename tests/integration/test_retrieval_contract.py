"""The full retrieval contract over HTTP on PostgreSQL, Redis and Qdrant.

Document metadata travels into the read model at index time; filters,
highlights, explain and batch run against it, and cited chunks and document
text resolve only for keys that may read the knowledge base.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.authz.service import ApiKeySpec, AuthzService
from cairn.catalog.dto import UploadSpec
from cairn.catalog.service import _runtime_key
from cairn.core.cache import get_cache
from cairn.core.document_metadata import DocumentMetadataTooLarge
from cairn.core.ids import encode_id
from cairn.core.time import utcnow
from cairn.objectstore.local import LocalObjectStore

__all__ = ["pipeline_admin"]

OPS = b"# Operations\n\nRotate the signing key with the rotate-key command every quarter."
ZH = "# 运维\n\n签名密钥每季度通过 rotate-key 命令轮换一次。".encode()


async def _add_document(catalog, admin, kb, tmp_path, source, *, filename, metadata):  # type: ignore[no-untyped-def]
    digest = sha256(source).hexdigest()
    key = f"{admin.workspace_id}/{kb.id}/originals/{digest}"
    await LocalObjectStore(tmp_path).put(key, source, content_type="text/markdown")
    registration = await catalog.register_upload(
        admin,
        kb.id,
        UploadSpec(
            filename=filename,
            content_hash=digest,
            size_bytes=len(source),
            mime_type="text/markdown",
            object_key=key,
            metadata=metadata,
        ),
    )
    assert registration.document is not None
    return registration.document


async def _indexed_kb(admin: Principal, tmp_path: Path):  # type: ignore[no-untyped-def]
    catalog, kb, first, *_ = await _create_pipeline_document(admin, tmp_path, source=OPS)
    second = await _add_document(
        catalog, admin, kb, tmp_path, ZH, filename="运维.md", metadata={"lang": "zh"}
    )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    # One task per queue per pass: one pass per document.
    await _execute_pipeline(pipeline)
    await _execute_pipeline(pipeline)
    assert first.document is not None
    for document in (first.document, second):
        view = await catalog.get_document(document.id)
        assert view.state == "indexed", (view.title, view.error_code, view.error_detail)
    runtime = await catalog.publish_runtime(kb.id)
    assert runtime.directory.doc_count == 2
    assert runtime.directory.chunk_count == 4  # two indexed children and their parent chunks
    assert runtime.directory.last_indexed_at is not None
    return catalog, kb, first.document, second


async def _key(admin: Principal, kb_id, scopes: list[str]) -> str:  # type: ignore[no-untyped-def]
    authz = AuthzService()
    principal = await authz.principal_for_user(admin.id)
    assert principal is not None
    _record, raw = await authz.create_api_key(
        principal, ApiKeySpec(name="-".join(scopes), scopes=scopes, kb_ids=[kb_id])
    )
    return raw


async def test_filters_highlights_explain_and_batch_over_http(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    _catalog, kb, first, second = await _indexed_kb(pipeline_admin, tmp_path)
    headers = {"Authorization": f"Bearer {await _key(pipeline_admin, kb.id, ['kb:query'])}"}
    target = [{"knowledge_base_id": encode_id("kb", kb.id)}]
    base = {
        "targets": target,
        "query": "rotate-key",
        "search_mode": "fulltext",
        "rerank": {"enabled": False},
        "options": {"expand_parent": False},
    }

    both = await client.post("/v1/retrieval/query", headers=headers, json=base)
    assert both.status_code == 200, both.text
    assert {hit["document_id"] for hit in both.json()["results"]} == {
        encode_id("doc", first.id),
        encode_id("doc", second.id),
    }

    zh_only = await client.post(
        "/v1/retrieval/query",
        headers=headers,
        json={
            **base,
            "filters": {"metadata": {"lang": "zh"}},
            "options": {"expand_parent": False, "include_highlights": True, "explain": True},
        },
    )
    assert zh_only.status_code == 200, zh_only.text
    body = zh_only.json()
    [hit] = body["results"]
    assert hit["document_id"] == encode_id("doc", second.id)
    assert hit["metadata"]["lang"] == "zh"
    assert hit["source"]["title"] == "运维.md" and hit["source"]["type"] == "md"
    assert [hit["content"][s["start"] : s["end"]] for s in hit["highlights"]] == ["rotate-key"]
    assert {stage["stage"] for stage in body["explain"]["stages"]} >= {"embed", "sparse"}

    unknown = await client.post(
        "/v1/retrieval/query",
        headers=headers,
        json={**base, "filters": {"metadata": {"lang": {"$gt": "a"}}}},
    )
    assert unknown.status_code == 400 and unknown.json()["code"] == "VALIDATION_FAILED"

    batch = await client.post(
        "/v1/retrieval/batch",
        headers=headers,
        json={
            "queries": [base, {**base, "filters": {"document_ids": [encode_id("doc", first.id)]}}]
        },
    )
    assert batch.status_code == 200, batch.text
    responses = batch.json()["responses"]
    assert len(responses[0]["results"]) == 2
    assert [hit["document_id"] for hit in responses[1]["results"]] == [encode_id("doc", first.id)]


async def test_dify_http_retrieval_and_auth_probe(pipeline_admin, tmp_path, client):
    from apps.api.main import create_app

    _catalog, kb, first, _second = await _indexed_kb(pipeline_admin, tmp_path)
    key = await _key(pipeline_admin, kb.id, ["kb:query"])
    app = create_app()

    async def embed_query(model, query, **kwargs):
        return [1.0, 1.0, 1.0], 1, None

    app.state.retrieval_runtime.embed_query = embed_query
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://testserver"
    ) as http:
        headers = {"Authorization": f"Bearer {key}"}
        probe = await http.post(
            "/retrieval", headers=headers, json={"knowledge_id": "", "query": ""}
        )
        assert probe.status_code == 400
        denied = await http.post("/retrieval", json={"knowledge_id": "", "query": ""})
        assert denied.status_code == 403 and denied.json()["error_code"] == 1002
        response = await http.post(
            "/retrieval",
            headers=headers,
            json={
                "knowledge_id": encode_id("kb", kb.id),
                "query": "rotate-key",
                "retrieval_setting": {"top_k": 5, "score_threshold": 0.5},
            },
        )
        assert response.status_code == 200, response.text
        records = response.json()["records"]
        assert records and all(0.5 <= item["score"] <= 1 for item in records)
        assert encode_id("doc", first.id) in {item["metadata"]["document_id"] for item in records}


async def test_citations_resolve_only_for_keys_that_may_read(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    _catalog, kb, first, _second = await _indexed_kb(pipeline_admin, tmp_path)
    query_only = {"Authorization": f"Bearer {await _key(pipeline_admin, kb.id, ['kb:query'])}"}
    reader = {"Authorization": f"Bearer {await _key(pipeline_admin, kb.id, ['kb:read'])}"}
    found = await client.post(
        "/v1/retrieval/query",
        headers=query_only,
        json={
            "targets": [{"knowledge_base_id": encode_id("kb", kb.id)}],
            "query": "signing key",
            "search_mode": "fulltext",
            "rerank": {"enabled": False},
            "options": {"expand_parent": False},
            "filters": {"document_ids": [encode_id("doc", first.id)]},
        },
    )
    chunk_id = found.json()["results"][0]["chunk_id"]

    hidden = await client.get(f"/v1/chunks/{chunk_id}", headers=query_only)
    shown = await client.get(f"/v1/chunks/{chunk_id}", headers=reader)
    hidden_text = await client.get(
        f"/v1/documents/{encode_id('doc', first.id)}/content", headers=query_only
    )
    shown_text = await client.get(
        f"/v1/documents/{encode_id('doc', first.id)}/content", headers=reader
    )

    assert hidden.status_code == 404 and hidden_text.status_code == 404
    assert shown.status_code == 200, shown.text
    assert shown.json()["knowledge_base_id"] == encode_id("kb", kb.id)
    assert "signing key" in shown.json()["content"]
    assert shown_text.status_code == 200, shown_text.text
    assert shown_text.headers["content-type"].startswith("text/markdown")
    assert "Rotate the signing key" in shown_text.text

    # A cited ID must not expose unpublished or retired versions, even to readers.
    from sqlalchemy import update

    from cairn.catalog.models import KnowledgeBase
    from cairn.core.db import transaction

    async with transaction() as session:
        await session.execute(
            update(KnowledgeBase).where(KnowledgeBase.id == kb.id).values(active_index_version=2)
        )
    retired = await client.get(f"/v1/chunks/{chunk_id}", headers=reader)
    assert retired.status_code == 404

    async with transaction() as session:
        await session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.id == kb.id)
            .values(active_index_version=1, deleted_at=utcnow())
        )
    deleted = await client.get(f"/v1/chunks/{chunk_id}", headers=reader)
    deleted_text = await client.get(
        f"/v1/documents/{encode_id('doc', first.id)}/content", headers=reader
    )
    assert deleted.status_code == 404 and deleted_text.status_code == 404


async def test_an_index_built_before_document_metadata_refuses_metadata_filters(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    catalog, kb, first, _second = await _indexed_kb(pipeline_admin, tmp_path)
    # Version snapshots are immutable in the database, so stand in for a version
    # captured before read model v2 with the projection such a version publishes.
    runtime = await catalog.publish_runtime(kb.id)
    assert runtime is not None and runtime.payload_schema == 2
    legacy = runtime.model_copy(update={"payload_schema": 1})
    await get_cache().set(_runtime_key(kb.id), legacy.model_dump_json().encode(), 300)
    headers = {"Authorization": f"Bearer {await _key(pipeline_admin, kb.id, ['kb:query'])}"}
    base = {
        "targets": [{"knowledge_base_id": encode_id("kb", kb.id)}],
        "query": "rotate-key",
        "search_mode": "fulltext",
        "rerank": {"enabled": False},
        "options": {"expand_parent": False},
    }

    refused = await client.post(
        "/v1/retrieval/query",
        headers=headers,
        json={**base, "filters": {"created_after": "2020-01-01T00:00:00Z"}},
    )
    by_document = await client.post(
        "/v1/retrieval/query",
        headers=headers,
        json={**base, "filters": {"document_ids": [encode_id("doc", first.id)]}},
    )

    assert refused.status_code == 400
    assert refused.json()["code"] == "FILTER_REQUIRES_REBUILD"
    assert encode_id("kb", kb.id) in refused.json()["detail"]
    assert by_document.status_code == 200
    assert [hit["document_id"] for hit in by_document.json()["results"]] == [
        encode_id("doc", first.id)
    ]


async def test_oversized_document_metadata_is_refused_at_registration(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)

    with pytest.raises(DocumentMetadataTooLarge):
        await _add_document(
            catalog,
            pipeline_admin,
            kb,
            tmp_path,
            b"# Big\n\nmetadata",
            filename="big.md",
            metadata={f"k{index}": index for index in range(65)},
        )


async def test_multipart_upload_metadata_is_what_filters_match(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path, source=OPS)
    writer = {"Authorization": f"Bearer {await _key(pipeline_admin, kb.id, ['kb:write'])}"}
    upload = f"/v1/knowledge-bases/{encode_id('kb', kb.id)}/documents/upload"

    rejected = await client.post(
        upload,
        headers=writer,
        files={"file": ("bad.md", b"# Bad\n\ntext", "text/markdown")},
        data={"metadata": "[1, 2]"},
    )
    accepted = await client.post(
        upload,
        headers=writer,
        files={"file": ("runbook.md", b"# Runbook\n\nRotate the signing key.", "text/markdown")},
        data={"metadata": '{"team": "sre", "tier": 1}'},
    )
    assert rejected.status_code == 400, rejected.text
    assert accepted.status_code == 202, accepted.text
    uploaded = accepted.json()["document"]["id"]

    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await _execute_pipeline(pipeline)
    headers = {"Authorization": f"Bearer {await _key(pipeline_admin, kb.id, ['kb:query'])}"}
    response = await client.post(
        "/v1/retrieval/query",
        headers=headers,
        json={
            "targets": [{"knowledge_base_id": encode_id("kb", kb.id)}],
            "query": "signing key",
            "search_mode": "fulltext",
            "rerank": {"enabled": False},
            "options": {"expand_parent": False},
            "filters": {"metadata": {"team": "sre", "tier": {"$lte": 1}}},
        },
    )

    assert response.status_code == 200, response.text
    assert [hit["document_id"] for hit in response.json()["results"]] == [uploaded]
    assert response.json()["results"][0]["metadata"]["team"] == "sre"
    assert catalog is not None


async def test_a_search_only_key_discovers_what_it_can_query(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    query_only = {"Authorization": f"Bearer {await _key(pipeline_admin, kb.id, ['kb:query'])}"}
    reader = {"Authorization": f"Bearer {await _key(pipeline_admin, kb.id, ['kb:read'])}"}

    searchable = await client.get("/v1/knowledge-bases", headers=query_only)
    readable = await client.get("/v1/knowledge-bases", headers=reader)

    assert searchable.status_code == 200, searchable.text
    [item] = [row for row in searchable.json()["items"] if row["id"] == encode_id("kb", kb.id)]
    assert item["permissions"] == ["kb:query"]
    [item] = [row for row in readable.json()["items"] if row["id"] == encode_id("kb", kb.id)]
    assert item["permissions"] == ["kb:query", "kb:read"]
