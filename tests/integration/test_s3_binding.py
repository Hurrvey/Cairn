"""S3 credentials remain sealed throughout catalog and worker projections."""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr
from tests.integration.test_ingestion_pipeline import pipeline_admin

from cairn.catalog.models import StorageBinding
from cairn.catalog.service import CatalogService
from cairn.catalog.storage import object_binding
from cairn.core.config import get_settings
from cairn.core.db import session_scope
from cairn.core.secrets import SealedCredential

__all__ = ["pipeline_admin"]


async def test_storage_credentials_are_sealed_and_rotate_across_worker_refs(
    pipeline_admin, monkeypatch
):
    async def healthy(self, binding_id):
        return True

    monkeypatch.setattr(CatalogService, "test_binding", healthy)
    catalog = CatalogService()
    ref = await catalog.create_binding(
        pipeline_admin,
        kind="object",
        driver="s3",
        name="Bucket",
        config={"bucket": "testing", "endpoint": "http://minio:9000", "allow_private": True},
        credentials={"access_key_id": "test-id", "secret_access_key": SecretStr("private-key")},
    )
    assert ref.has_credentials
    assert "private-key" not in repr(ref)
    async with session_scope() as session:
        row = await session.get(StorageBinding, ref.id)
        first = await object_binding(session, row)
        assert isinstance(first.credential, SealedCredential)
        assert "private-key" not in first.credential.model_dump_json()
        assert (
            json.loads(
                first.credential.open(
                    get_settings().master_key, pipeline_admin.workspace_id
                ).get_secret_value()
            )["secret_access_key"]
            == "private-key"
        )
    await catalog.rotate_binding_credentials(
        pipeline_admin,
        ref.id,
        {"access_key_id": "test-id", "secret_access_key": SecretStr("second-key")},
    )
    async with session_scope() as session:
        row = await session.get(StorageBinding, ref.id)
        second = await object_binding(session, row)
    assert second.credential.secret_id != first.credential.secret_id


async def test_storage_probe_rejects_foreign_workspace_before_network(pipeline_admin, monkeypatch):
    from uuid import uuid4

    from cairn.core.errors import NotFound
    from cairn.objectstore.registry import ObjectStoreRegistry

    catalog = CatalogService()
    ref = await catalog.create_binding(
        pipeline_admin, kind="object", driver="local", name="Private"
    )

    async def forbidden(*args):
        raise AssertionError("foreign binding touched network")

    monkeypatch.setattr(ObjectStoreRegistry, "for_binding", forbidden)
    with pytest.raises(NotFound):
        await catalog.test_binding(ref.id, workspace_id=uuid4())


async def test_real_s3_upload_index_content_and_cleanup(pipeline_admin, tmp_path, client):
    import os

    from sqlalchemy import update
    from tests.integration.test_ingestion_pipeline import (
        _create_pipeline_document,
        _execute_pipeline,
        _test_pipeline,
    )

    from cairn.authz.service import ApiKeySpec, AuthzService
    from cairn.catalog.models import KnowledgeBase, StorageBinding
    from cairn.catalog.storage import object_binding
    from cairn.core.db import transaction
    from cairn.core.ids import encode_id
    from cairn.objectstore.registry import get_object_registry

    endpoint = os.environ.get("CAIRN_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("requires a real S3 endpoint")
    catalog, kb, registration, source, source_key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    ref = await catalog.create_binding(
        pipeline_admin,
        kind="object",
        driver="s3",
        name="Real bucket",
        config={
            "bucket": "cairn-test",
            "endpoint": endpoint,
            "allow_private": True,
            "prefix": f"integration/{kb.id}",
        },
        credentials={"access_key_id": "cairn-test", "secret_access_key": "cairn-disposable-only"},
    )
    async with transaction() as session:
        row = await session.get(StorageBinding, ref.id)
        store = await get_object_registry().for_binding(await object_binding(session, row))
        await store.put(source_key, source, content_type="text/markdown")
        await session.execute(
            update(KnowledgeBase).where(KnowledgeBase.id == kb.id).values(object_binding_id=ref.id)
        )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)

    async def object_store(binding):
        from cairn.objectstore.registry import ObjectBindingRef

        return await get_object_registry().for_binding(
            ObjectBindingRef(
                binding.id,
                binding.driver,
                binding.config,
                credential=binding.credential,
                workspace_id=binding.workspace_id,
            )
        )

    from dataclasses import replace

    pipeline._resources = replace(pipeline._resources, object_store_for=object_store)
    try:
        await _execute_pipeline(pipeline)
        view = await catalog.get_document(registration.document.id)
        assert view.state == "indexed", (view.error_code, view.error_detail)
        authz = AuthzService()
        actor = await authz.principal_for_user(pipeline_admin.id)
        _, key = await authz.create_api_key(
            actor, ApiKeySpec(name="s3-reader", scopes=["kb:read"], kb_ids=[kb.id])
        )
        content = await client.get(
            f"/v1/documents/{encode_id('doc', view.id)}/content",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert content.status_code == 200, content.text
        assert "paragraph" in content.text
        assert (await store.list("", limit=100)).items
        from tests.integration.test_ingestion_pipeline import _execute_one

        from apps.worker.main import build_worker

        await catalog.delete_document(pipeline_admin, view.id)
        await _execute_one(build_worker("maintain"))
        assert not (await store.list("", limit=100)).items
    finally:
        await store.delete_prefix("")
