"""Package-B management, setup-option, and nested-scope integration tests."""

from __future__ import annotations

import asyncio
import json
import os
from hashlib import sha256
from pathlib import Path

import httpx
from tests.integration.test_ingestion_pipeline import (
    STRONG_PASSWORD,
    _create_pipeline_document,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.authz.service import ApiKeySpec, AuthzService
from cairn.catalog.dto import CreateKbSpec, UploadSpec
from cairn.catalog.service import CatalogService
from cairn.core.cache import get_cache
from cairn.core.config import RetrievalSettings, get_settings
from cairn.core.ids import decode_id, encode_id
from cairn.core.provider_runtime import ProviderRuntimeProjection, provider_runtime_key
from cairn.modelgw.catalog import ModelCatalog
from cairn.objectstore.keys import ObjectKeys
from cairn.objectstore.local import LocalObjectStore
from cairn.retrieval.runtime import QueryEmbeddingRuntime

__all__ = ["pipeline_admin"]


async def _admin_session(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": STRONG_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": client.cookies["cairn_csrf"]}


async def test_admin_model_management_and_safe_kb_options(
    pipeline_admin: Principal,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    settings.retrieval.tokenizer_files = {"minilm": str(tmp_path / "tokenizer.json")}
    headers = await _admin_session(client)

    provider_response = await client.post(
        "/v1/model-providers",
        headers=headers,
        json={
            "name": "local-tei",
            "family": "tei",
            "base_url": "http://127.0.0.1:9",
            "config": {
                "binding_revision": "minilm-v1",
                "allow_private": True,
                "max_batch_size": 16,
            },
        },
    )
    assert provider_response.status_code == 201, provider_response.text
    provider = provider_response.json()
    assert set(provider) == {
        "id",
        "name",
        "family",
        "base_url",
        "is_enabled",
        "model_count",
        "has_credentials",
    }
    assert provider["has_credentials"] is False
    provider_uuid = decode_id("prov", provider["id"])
    projection_raw = await get_cache().get(provider_runtime_key(provider_uuid))
    assert projection_raw is not None
    projection = ProviderRuntimeProjection.model_validate_json(projection_raw)
    assert projection.workspace_id == pipeline_admin.workspace_id
    assert projection.credential is None
    await get_cache().delete(provider_runtime_key(provider_uuid))

    from cairn.catalog.maintenance import RuntimeRefresher

    await RuntimeRefresher().refresh(1)
    assert await get_cache().get(provider_runtime_key(provider_uuid)) is not None

    # Keys are accepted now that they are stored encrypted (TEI supports --api-key),
    # but they are never echoed and Redis only ever sees ciphertext.
    keyed = await client.post(
        "/v1/model-providers",
        headers=headers,
        json={
            "name": "secret-provider",
            "family": "tei",
            "base_url": "http://127.0.0.1:9",
            "api_key": "tei-bearer-value",
            "config": {"binding_revision": "v1", "allow_private": True},
        },
    )
    assert keyed.status_code == 201, keyed.text
    assert keyed.json()["has_credentials"] is True
    assert "tei-bearer-value" not in keyed.text
    keyed_raw = await get_cache().get(provider_runtime_key(decode_id("prov", keyed.json()["id"])))
    assert keyed_raw is not None and b"tei-bearer-value" not in keyed_raw
    assert ProviderRuntimeProjection.model_validate_json(keyed_raw).credential is not None
    hosted_without_key = await client.post(
        "/v1/model-providers",
        headers=headers,
        json={"name": "aliyun", "family": "dashscope", "config": {"binding_revision": "v1"}},
    )
    assert hosted_without_key.status_code == 400

    model_response = await client.post(
        "/v1/models",
        headers=headers,
        json={
            "provider_id": provider["id"],
            "model_key": "sentence-transformers/all-MiniLM-L6-v2",
            "display_name": "MiniLM",
            "capability": "embedding",
            "dimension": 384,
            "max_input_tokens": 256,
            "optimal_batch_size": 16,
            "tokenizer_id": "minilm",
            "normalize": True,
        },
    )
    assert model_response.status_code == 201, model_response.text
    model = model_response.json()
    assert model["provider_id"] == provider["id"]
    assert model["tokenizer_id"] == "minilm"

    options_response = await client.get("/v1/knowledge-base-options")
    assert options_response.status_code == 200, options_response.text
    options = options_response.json()
    assert set(options) == {
        "models",
        "bindings",
        "tokenizers",
        "supported_extensions",
        "max_upload_bytes",
    }
    assert options["models"] == [
        {
            "id": model["id"],
            "provider_id": provider["id"],
            "display_name": "MiniLM",
            "model_key": "sentence-transformers/all-MiniLM-L6-v2",
            "dimension": 384,
            "max_input_tokens": 256,
            "tokenizer_id": "minilm",
            "provider_family": "tei",
            "sparse": False,
        }
    ]
    assert options["bindings"] == []
    assert options["tokenizers"] == ["minilm"]
    assert {".pdf", ".png", ".jpg", ".tiff"}.issubset(options["supported_extensions"])
    assert options["max_upload_bytes"] == 50 * 1024 * 1024


async def test_nested_document_routes_bind_the_path_knowledge_base(
    pipeline_admin: Principal,
    tmp_path: Path,
    client: httpx.AsyncClient,
) -> None:
    catalog, first_kb, first_registration, *_ = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    first_document = first_registration.document
    assert first_document is not None
    second_kb = await catalog.create_kb(
        pipeline_admin,
        CreateKbSpec(
            name="Second pipeline",
            embedding_model_id=first_kb.embedding_model_id,
            vector_binding_id=first_kb.vector_binding_id,
            object_binding_id=first_kb.object_binding_id,
        ),
    )
    source = b"belongs only to the second knowledge base"
    digest = sha256(source).hexdigest()
    key = ObjectKeys.original(pipeline_admin.workspace_id, second_kb.id, digest)
    await LocalObjectStore(tmp_path).put(key, source, content_type="text/plain")
    second_registration = await catalog.register_upload(
        pipeline_admin,
        second_kb.id,
        UploadSpec(
            filename="second.txt",
            content_hash=digest,
            size_bytes=len(source),
            mime_type="text/plain",
            object_key=key,
        ),
    )
    second_document = second_registration.document
    assert second_document is not None

    refreshed = await AuthzService().principal_for_user(pipeline_admin.id)
    assert refreshed is not None
    _record, token = await AuthzService().create_api_key(
        refreshed,
        ApiKeySpec(
            name="nested-scope",
            scopes=["kb:read", "kb:write"],
            kb_ids=[first_kb.id, second_kb.id],
        ),
    )
    headers = {"Authorization": f"Bearer {token}"}
    first_path = f"/v1/knowledge-bases/{encode_id('kb', first_kb.id)}"
    second_document_id = encode_id("doc", second_document.id)

    get_response = await client.get(f"{first_path}/documents/{second_document_id}", headers=headers)
    download_response = await client.get(
        f"{first_path}/documents/{second_document_id}/download", headers=headers
    )
    chunks_response = await client.get(
        f"{first_path}/documents/{second_document_id}/chunks", headers=headers
    )
    delete_response = await client.delete(
        f"{first_path}/documents/{second_document_id}", headers=headers
    )

    assert get_response.status_code == 404
    assert download_response.status_code == 404
    assert chunks_response.status_code == 404
    assert delete_response.status_code == 404
    assert (await catalog.get_document(second_document.id)).kb_id == second_kb.id


async def test_local_binding_api_confines_explicit_path_to_deployment_root(
    pipeline_admin: Principal,
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    settings = get_settings()
    deployment_root = tmp_path / "objects"
    settings.objectstore.local_path = str(deployment_root)
    headers = await _admin_session(client)

    default_response = await client.post(
        "/v1/storage-bindings",
        headers=headers,
        json={"name": "default", "kind": "object", "driver": "local", "config": {}},
    )
    assert default_response.status_code == 201, default_response.text
    assert default_response.json()["config"] == {}

    escaped_response = await client.post(
        "/v1/storage-bindings",
        headers=headers,
        json={
            "name": "escaped",
            "kind": "object",
            "driver": "local",
            "config": {"path": str(tmp_path.parent)},
        },
    )
    assert escaped_response.status_code == 400, escaped_response.text

    listed = await client.get("/v1/storage-bindings")
    assert listed.status_code == 200
    assert json.dumps(listed.json()).find(str(deployment_root)) == -1


async def test_live_model_probe_uses_registered_tei_and_tokenizer(
    pipeline_admin: Principal,
    client: httpx.AsyncClient,
) -> None:
    endpoint = os.environ["CAIRN_TEST_TEI_URL"]
    tokenizer_path = os.environ["CAIRN_TEST_TEI_TOKENIZER"]
    get_settings().retrieval.tokenizer_files = {"minilm": tokenizer_path}
    headers = await _admin_session(client)
    provider = (
        await client.post(
            "/v1/model-providers",
            headers=headers,
            json={
                "name": "live-tei",
                "family": "tei",
                "base_url": endpoint,
                "config": {
                    "binding_revision": "fresh-minilm",
                    "allow_private": True,
                    "max_batch_size": 16,
                },
            },
        )
    ).json()
    model_response = await client.post(
        "/v1/models",
        headers=headers,
        json={
            "provider_id": provider["id"],
            "model_key": "sentence-transformers/all-MiniLM-L6-v2",
            "display_name": "Fresh MiniLM",
            "capability": "embedding",
            "dimension": 384,
            "max_input_tokens": 256,
            "optimal_batch_size": 16,
            "tokenizer_id": "minilm",
            "normalize": True,
        },
    )
    assert model_response.status_code == 201, model_response.text

    tested = await client.post(f"/v1/models/{model_response.json()['id']}/test", headers=headers)

    assert tested.status_code == 200, tested.text
    assert tested.json()["healthy"] is True
    assert tested.json()["dimensions"] == 384
    assert tested.json()["tokens"] > 0

    model = await ModelCatalog().get_ref(decode_id("mdl", model_response.json()["id"]))
    runtime = QueryEmbeddingRuntime(
        RetrievalSettings(tokenizer_files={"minilm": tokenizer_path}), cache=get_cache()
    )
    await runtime.start()
    try:
        vector, _tokens, _cached = await runtime.embed_query_for_workspace(
            pipeline_admin.workspace_id, model, "Saturn has rings"
        )
    finally:
        await runtime.close()
    assert len(vector) == 384


async def test_referenced_model_and_provider_deletion_is_refused(
    pipeline_admin: Principal,
    tmp_path: Path,
    client: httpx.AsyncClient,
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    model = await ModelCatalog().get_ref(kb.embedding_model_id)
    assert model.provider_id is not None
    headers = await _admin_session(client)

    model_delete = await client.delete(f"/v1/models/{encode_id('mdl', model.id)}", headers=headers)
    provider_delete = await client.delete(
        f"/v1/model-providers/{encode_id('prov', model.provider_id)}", headers=headers
    )

    assert model_delete.status_code == 409
    assert provider_delete.status_code == 409


async def test_kb_delete_waits_for_inflight_canonical_upload(
    pipeline_admin: Principal,
    tmp_path: Path,
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    service = CatalogService()
    source = b"upload serialized with knowledge base deletion"
    digest = sha256(source).hexdigest()
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_source():  # type: ignore[no-untyped-def]
        started.set()
        await release.wait()
        yield source

    uploading = asyncio.create_task(
        service.upload_document(
            pipeline_admin,
            kb.id,
            UploadSpec(
                filename="race.txt",
                content_hash=digest,
                size_bytes=len(source),
                mime_type="text/plain",
                object_key=ObjectKeys.original(pipeline_admin.workspace_id, kb.id, digest),
            ),
            delayed_source(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    deleting = asyncio.create_task(service.delete_kb(pipeline_admin, kb.id))
    await asyncio.sleep(0.05)
    assert not deleting.done()

    release.set()
    registration, _deleted = await asyncio.gather(uploading, deleting)

    assert registration.status == "accepted"
