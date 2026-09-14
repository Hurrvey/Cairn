"""Focused regressions for index-version-owned configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import ReindexEstimate, ReindexSpec
from cairn.core.db import session_scope, transaction
from cairn.ingestion.pipeline import register_pipeline_handlers
from cairn.modelgw.catalog import ModelCatalog
from cairn.tasks.worker import TaskWorker

__all__ = ["pipeline_admin"]


async def test_initial_version_freezes_complete_configuration(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    expected_model = await ModelCatalog().get_ref(kb.embedding_model_id)

    version = (await catalog.list_index_versions(kb.id))[0]

    assert version.config is not None
    assert version.config.embedding_model == expected_model
    assert version.config.metric == kb.metric
    assert version.config.chunk_config == kb.chunk_config
    async with session_scope() as session:
        high_water = await session.scalar(
            text("SELECT index_version_high_water FROM knowledge_base WHERE id=:kb"),
            {"kb": kb.id},
        )
    assert high_water == 1


async def test_captured_version_configuration_cannot_be_mutated(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    changed = ChunkConfig(strategy="fixed").model_dump(mode="json")

    with pytest.raises(IntegrityError, match=r"snapshot.*immutable"):
        async with transaction() as session:
            await session.execute(
                text(
                    "UPDATE kb_index_version "
                    "SET config_snapshot=jsonb_set(config_snapshot, '{chunk_config}', "
                    "CAST(:chunk_config AS jsonb)) WHERE kb_id=:kb AND version=1"
                ),
                {"kb": kb.id, "chunk_config": json.dumps(changed)},
            )


async def test_version_high_water_cannot_be_decreased(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    _catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)

    with pytest.raises(IntegrityError, match=r"high-water.*decrease"):
        async with transaction() as session:
            await session.execute(
                text("UPDATE knowledge_base SET index_version_high_water=0 WHERE id=:kb"),
                {"kb": kb.id},
            )


async def test_parse_fails_before_artifact_write_when_registered_model_drifted(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    assert registration.document is not None
    async with transaction() as session:
        await session.execute(
            text("UPDATE model SET tokenizer_id='drifted-tokenizer' WHERE id=:model"),
            {"model": kb.embedding_model_id},
        )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    worker = TaskWorker("parse", concurrency=1, poll_interval=0.01)
    register_pipeline_handlers(worker, pipeline)

    await _execute_one(worker)

    failed = await catalog.get_document(registration.document.id)
    assert failed.state == "failed"
    async with session_scope() as session:
        parsed_object_key = await session.scalar(
            text("SELECT parsed_object_key FROM document WHERE id=:document"),
            {"document": registration.document.id},
        )
    assert parsed_object_key is None


async def test_same_model_id_registry_drift_is_captured_by_fresh_rebuild(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    await catalog.activate_index_version(kb.id, 1)
    async with transaction() as session:
        await session.execute(
            text(
                "UPDATE model SET dimension=4, tokenizer_id='replacement-tokenizer' WHERE id=:model"
            ),
            {"model": kb.embedding_model_id},
        )

    result = await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))

    assert not isinstance(result, ReindexEstimate)
    desired = await catalog.get_kb(kb.id)
    assert desired.embedding_model_id == kb.embedding_model_id
    assert desired.embedding_dim == 4
    assert result.config is not None
    assert result.config.embedding_model.dimension == 4
    assert result.config.embedding_model.tokenizer_id == "replacement-tokenizer"
    active = await catalog.publish_runtime(kb.id)
    assert active is not None
    assert active.embedding_model.dimension == 3
