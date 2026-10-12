"""Coordinator acceptance for active model coherence and rebuild identity."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.catalog.dto import ReindexSpec
from cairn.catalog.ingestion import CatalogIngestionFacade
from cairn.core.db import transaction
from cairn.modelgw.catalog import ModelCatalog
from cairn.modelgw.dto import RegisterModelSpec
from cairn.tasks.dto import TaskContext

__all__ = ["pipeline_admin"]


async def test_rebuild_keeps_active_model_bound_to_active_vectors(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, _registration, _source, _key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    pipeline, *_rest = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    models = ModelCatalog()
    old = await models.get_embedding_runtime(kb.embedding_model_id)
    replacement = await models.register_model(
        pipeline_admin.workspace_id,
        RegisterModelSpec(
            provider_id=old.provider_id,
            model_key="replacement-four-dimensional",
            display_name="Replacement model",
            capability="embedding",
            dimension=4,
            max_input_tokens=128,
            tokenizer_id="pipeline-test-tokenizer",
        ),
    )
    await catalog.start_reindex(
        pipeline_admin, kb.id, ReindexSpec(embedding_model_id=replacement.id, confirm=True)
    )
    runtime = await catalog.publish_runtime(kb.id)
    assert runtime is not None
    assert runtime.index_version == 1
    assert runtime.embedding_model.id == kb.embedding_model_id
    assert runtime.embedding_model.dimension == 3


async def test_failed_rebuild_does_not_reuse_its_index_version(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, _registration, _source, _key = await _create_pipeline_document(
        pipeline_admin, tmp_path
    )
    pipeline, *_rest = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    await catalog.fail_index_version(kb.id, 2, "simulated failed rebuild")
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version == 3
    assert [version.version for version in await catalog.list_index_versions(kb.id)] == [3, 2, 1]


async def test_purged_failed_version_does_not_erase_allocation_history(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    await catalog.fail_index_version(kb.id, 2, "failed before fanout")
    # Simulate removal of a failed version by the retention path, without
    # introducing a dependency on the still-unimplemented maintenance handler.
    async with transaction() as session:
        await session.execute(
            text("DELETE FROM kb_index_version WHERE kb_id=:kb AND version=2"),
            {"kb": kb.id},
        )
    await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
    current = await catalog.get_kb(kb.id)
    assert current.active_index_version == 1
    assert current.building_index_version == 3


async def test_published_model_metadata_is_not_reconstructed_from_mutable_registry(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    original = await ModelCatalog().get_ref(kb.embedding_model_id)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE model SET tokenizer_id='changed-tokenizer', normalize=NOT normalize, "
                "model_key='changed-weights', max_input_tokens=64, query_prefix='changed: ' "
                "WHERE id=:model"
            ),
            {"model": kb.embedding_model_id},
        )
    runtime = await catalog.publish_runtime(kb.id)
    assert runtime is not None
    assert runtime.index_version == 1
    assert runtime.embedding_model == original


async def test_active_metric_does_not_follow_mutated_desired_config(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    async with transaction() as session:
        # Emulate the atomic desired-metric/build-pointer update permitted to
        # rebuild writers by ADR-0006; do not disable the database guard.
        await session.execute(
            text(
                "UPDATE knowledge_base SET metric='dot', building_index_version=2, "
                "status='indexing' WHERE id=:kb"
            ),
            {"kb": kb.id},
        )
    runtime = await catalog.publish_runtime(kb.id)
    assert runtime is not None
    assert runtime.metric == kb.metric


async def test_existing_run_keeps_version_chunk_settings_after_desired_change(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    # This exercises the read-only catalog DTO seam, not task lease ownership.
    context = TaskContext(
        task_id=0,
        queue="parse",
        kind="document.parse",
        workspace_id=pipeline_admin.workspace_id,
        kb_id=kb.id,
        document_id=registration.document.id,
        payload={"revision": 1, "index_version": 1},
        attempt=1,
        max_attempts=3,
        correlation_id=None,
        worker_id="snapshot-read-acceptance",
    )
    facade = CatalogIngestionFacade()
    before = await facade.load_run(context)
    assert before is not None
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE knowledge_base SET chunk_config=jsonb_set("
                "chunk_config, '{strategy}', '\"fixed\"'::jsonb) WHERE id=:kb"
            ),
            {"kb": kb.id},
        )
    after = await facade.load_run(context)
    assert after is not None
    assert after.chunk_config == before.chunk_config
