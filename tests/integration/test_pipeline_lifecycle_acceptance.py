"""Coordinator acceptance for active model coherence and rebuild identity."""

from __future__ import annotations

from pathlib import Path

from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.catalog.dto import ReindexSpec
from cairn.modelgw.catalog import ModelCatalog
from cairn.modelgw.dto import RegisterModelSpec

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
