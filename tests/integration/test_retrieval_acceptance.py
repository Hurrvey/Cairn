"""Coordinator HTTP acceptance for the first public retrieval endpoint."""

from __future__ import annotations

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

from apps.api.main import create_app
from cairn.authz.dataplane import get_dataplane_authz, hash_api_key
from cairn.authz.model import Principal
from cairn.authz.service import ApiKeySpec, AuthzService
from cairn.catalog.dto import ReindexSpec
from cairn.core.cache import get_cache
from cairn.core.config import get_settings
from cairn.core.ids import encode_id

__all__ = ["pipeline_admin"]


async def test_public_retrieval_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/retrieval/query",
        json={"targets": [{"knowledge_base_id": encode_id("kb", uuid4())}], "query": "hello"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("search_mode", ["fulltext", "vector", "hybrid"])
@pytest.mark.parametrize("rebuilding", [False, True])
async def test_authorized_query_returns_indexed_content_via_http(
    pipeline_admin: Principal,
    tmp_path: Path,
    client: httpx.AsyncClient,
    search_mode: str,
    rebuilding: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(
        pipeline_admin,
        tmp_path,
        source=b"# Astronomy\n\nSaturn has prominent rings of ice and rock.",
    )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    assert registration.document is not None
    authz = AuthzService()
    principal = await authz.principal_for_user(pipeline_admin.id)
    assert principal is not None
    _key, raw = await authz.create_api_key(
        principal, ApiKeySpec(name="retrieval-test", scopes=["kb:query"], kb_ids=[kb.id])
    )
    if rebuilding:
        await catalog.start_reindex(principal, kb.id, ReindexSpec(confirm=True))
        await catalog.publish_runtime(kb.id)
    await get_dataplane_authz().authenticate_api_key(raw, ip="127.0.0.1")

    async def forbid_control_database(*args, **kwargs):
        raise AssertionError("A warm retrieval request must not hydrate control-plane tables")

    monkeypatch.setattr(
        "cairn.authz.repository.AuthzRepository.get_key_by_hash", forbid_control_database
    )
    monkeypatch.setattr(
        "cairn.catalog.repository.CatalogRepository.get_kb", forbid_control_database
    )
    monkeypatch.setattr(
        "cairn.modelgw.catalog.ModelCatalog.get_embedding_runtime", forbid_control_database
    )
    response = await client.post(
        "/v1/retrieval/query",
        headers={"Authorization": f"Bearer {raw}"},
        json={
            "targets": [{"knowledge_base_id": encode_id("kb", kb.id)}],
            "query": "Saturn",
            "search_mode": search_mode,
            **({"query_vector": [1.0, 1.0, 1.0]} if search_mode != "fulltext" else {}),
            "rerank": {"enabled": False},
            "options": {"expand_parent": False},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"] and "Saturn" in body["results"][0]["content"]
    assert body["results"][0]["document_id"] == encode_id("doc", registration.document.id)
    assert body["results"][0]["knowledge_base_id"] == encode_id("kb", kb.id)
    assert body["usage"]["index_versions"][encode_id("kb", kb.id)] == 1
    assert body["degraded"] == []
    assert body["partial_failures"] == []


async def test_query_denies_ungranted_target_even_when_other_target_is_allowed(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    authz = AuthzService()
    principal = await authz.principal_for_user(pipeline_admin.id)
    assert principal is not None
    _key, raw = await authz.create_api_key(
        principal, ApiKeySpec(name="one-kb-only", scopes=["kb:query"], kb_ids=[kb.id])
    )
    response = await client.post(
        "/v1/retrieval/query",
        headers={"Authorization": f"Bearer {raw}"},
        json={
            "targets": [
                {"knowledge_base_id": encode_id("kb", kb.id)},
                {"knowledge_base_id": encode_id("kb", uuid4())},
            ],
            "query": "restricted information",
        },
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    "invalid_runtime", ["foreign_workspace", "wrong_kb", "deleting", "missing"]
)
async def test_runtime_cannot_route_query_to_an_unusable_or_foreign_kb(
    pipeline_admin: Principal,
    tmp_path: Path,
    client: httpx.AsyncClient,
    invalid_runtime: str,
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    principal = await AuthzService().principal_for_user(pipeline_admin.id)
    assert principal is not None
    _key, raw = await AuthzService().create_api_key(
        principal, ApiKeySpec(name="runtime-test", scopes=["kb:query"], kb_ids=[kb.id])
    )
    runtime = await catalog.publish_runtime(kb.id)
    assert runtime is not None
    cache_key = f"kb:runtime:{kb.id}"
    if invalid_runtime == "missing":
        await get_cache().delete(cache_key)
    else:
        updates = {
            "foreign_workspace": {"workspace_id": uuid4()},
            "wrong_kb": {"id": uuid4()},
            "deleting": {"status": "deleting"},
        }[invalid_runtime]
        await get_cache().set(
            cache_key, runtime.model_copy(update=updates).model_dump_json().encode(), 300
        )
    response = await client.post(
        "/v1/retrieval/query",
        headers={"Authorization": f"Bearer {raw}"},
        json={
            "targets": [{"knowledge_base_id": encode_id("kb", kb.id)}],
            "query": "paragraph",
            "search_mode": "fulltext",
            "rerank": {"enabled": False},
            "options": {"expand_parent": False},
            "strict": True,
        },
    )
    assert response.status_code in {403, 404, 409, 502, 503}, response.text
    assert "paragraph" not in response.text.lower()


async def test_missing_ip_policy_cache_never_turns_restricted_key_into_unrestricted_key(
    pipeline_admin: Principal, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    principal = await AuthzService().principal_for_user(pipeline_admin.id)
    assert principal is not None
    _key, raw = await AuthzService().create_api_key(
        principal,
        ApiKeySpec(
            name="restricted-ip", scopes=["kb:query"], kb_ids=[kb.id], ip_allowlist=["192.0.2.0/24"]
        ),
    )
    await get_dataplane_authz().authenticate_api_key(raw, ip="192.0.2.10")
    await get_cache().delete(f"authz:ip:{hash_api_key(raw)}")
    response = await client.post(
        "/v1/retrieval/query",
        headers={"Authorization": f"Bearer {raw}"},
        json={"targets": [{"knowledge_base_id": encode_id("kb", kb.id)}], "query": "paragraph"},
    )
    assert response.status_code in {401, 403, 503}, response.text


@pytest.mark.parametrize("role", ["control", "data", "all"])
async def test_retrieval_endpoint_mount_matches_deployment_role(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    monkeypatch.setenv("CAIRN_ROLE", role)
    get_settings.cache_clear()
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/retrieval/query",
            json={"targets": [{"knowledge_base_id": encode_id("kb", uuid4())}], "query": "hello"},
        )
    assert response.status_code == (404 if role == "control" else 401)
    get_settings.cache_clear()
