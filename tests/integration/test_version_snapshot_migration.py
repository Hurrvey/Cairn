"""Upgrade regression for legacy versions whose provenance is unknowable."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from tests.integration.conftest import REPO_ROOT
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.catalog.config import ChunkConfig
from cairn.catalog.service import CatalogService
from cairn.core.cache import get_cache
from cairn.core.db import dispose_engine, session_scope
from cairn.core.errors import ValidationFailed

__all__ = ["pipeline_admin"]


def _migrate(revision: str) -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        os.environ["CAIRN_DATABASE_URL"].replace("+asyncpg", "").replace("%", "%%"),
    )
    if revision == "0007_ingestion_pipeline":
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


async def test_upgrade_leaves_drifted_legacy_snapshot_unavailable_and_reserves_failed_slot(
    pipeline_admin: Principal, tmp_path: Path
) -> None:
    catalog, kb, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    await catalog.activate_index_version(kb.id, 1)
    assert await catalog.publish_runtime(kb.id) is not None
    runtime_key = f"kb:runtime:{kb.id}"
    assert await get_cache().get(runtime_key) is not None

    await dispose_engine()
    _migrate("0007_ingestion_pipeline")
    try:
        legacy_url = os.environ["CAIRN_DATABASE_URL"].replace("postgresql+asyncpg", "postgresql")
        changed_chunk_config = ChunkConfig(strategy="fixed").model_dump(mode="json")
        with psycopg2.connect(legacy_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE model SET tokenizer_id='drifted-tokenizer', "
                "model_key='drifted-model-key' WHERE id=%s",
                (str(kb.embedding_model_id),),
            )
            cursor.execute(
                "UPDATE knowledge_base SET chunk_config=%s::jsonb WHERE id=%s",
                (json.dumps(changed_chunk_config), str(kb.id)),
            )
            cursor.execute(
                "INSERT INTO kb_index_version "
                "(kb_id, version, workspace_id, state, layout) "
                "VALUES (%s, 2, %s, 'failed', 'shared')",
                (str(kb.id), str(kb.workspace_id)),
            )
            cursor.execute(
                "DELETE FROM kb_index_version WHERE kb_id=%s AND version=2",
                (str(kb.id),),
            )
            cursor.execute(
                "INSERT INTO task (workspace_id, kb_id, queue, kind, payload) "
                "VALUES (%s, %s, 'maintain', 'legacy.untrusted', "
                "'{\"index_version\":999999999999999999999999}'::jsonb)",
                (str(kb.workspace_id), str(kb.id)),
            )
            cursor.execute(
                "INSERT INTO task (workspace_id, kb_id, queue, kind, payload) "
                "VALUES (%s, %s, 'maintain', 'legacy.overflow', "
                "'{\"index_version\":9999999999}'::jsonb)",
                (str(kb.workspace_id), str(kb.id)),
            )
            cursor.execute(
                "INSERT INTO task (workspace_id, kb_id, queue, kind, payload) "
                "VALUES (%s, %s, 'maintain', 'legacy.malformed', "
                '\'{"index_version":"not-a-version"}\'::jsonb)',
                (str(kb.workspace_id), str(kb.id)),
            )

        _migrate("head")
        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT config_snapshot, snapshot_unavailable, "
                        "index_version_high_water FROM kb_index_version v "
                        "JOIN knowledge_base kb ON kb.id=v.kb_id "
                        "WHERE v.kb_id=:kb AND v.version=1"
                    ),
                    {"kb": kb.id},
                )
            ).one()
        assert row.config_snapshot is None
        assert row.snapshot_unavailable is True
        assert row.index_version_high_water == 2

        with pytest.raises(ValidationFailed, match="rebuild"):
            await CatalogService().publish_runtime(kb.id)
        assert await get_cache().get(runtime_key) is None
    finally:
        await dispose_engine()
        _migrate("head")
