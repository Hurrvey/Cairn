"""Parser deployment changes must take effect through ordinary rebuild enrollment."""

from dataclasses import replace
from pathlib import Path

from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from cairn.catalog.dto import ReindexSpec
from cairn.catalog.reindex import ReindexFanoutService
from cairn.core.db import session_scope
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.registry import ParserDescriptor, ParserRegistry
from cairn.tasks.worker import TaskWorker

__all__ = ["pipeline_admin"]


async def test_parser_change_reindex_reparses_and_same_parse_rebuild_reuses(
    pipeline_admin, tmp_path: Path
):
    catalog, kb, registration, *_ = await _create_pipeline_document(pipeline_admin, tmp_path)
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)

    class Changed:
        supported_mimes = frozenset({"text/markdown"})
        calls = 0

        async def parse(self, data, ctx):
            self.calls += 1
            return ParsedDocument("New parser result", [Block("paragraph", "New parser result", 0)])

    changed = Changed()
    registry = ParserRegistry()
    registry.register(changed, descriptor=ParserDescriptor(implementation="changed/1"))
    pipeline._resources = replace(pipeline._resources, parsers=registry)
    versions = []
    for expected_index in (2, 3):
        await catalog.start_reindex(pipeline_admin, kb.id, ReindexSpec(confirm=True))
        worker = TaskWorker("maintain", concurrency=1)
        worker.register("kb.reindex_fanout", ReindexFanoutService().handle)
        await _execute_one(worker)
        await _execute_pipeline(pipeline)
        await _execute_one(worker)
        await _execute_one(worker)
        assert (await catalog.get_kb(kb.id)).active_index_version == expected_index
        async with session_scope() as session:
            version = await session.scalar(
                text(
                    "SELECT parse_version_id FROM document_ingestion "
                    "WHERE document_id=:doc AND index_version=:idx"
                ),
                {"doc": registration.document.id, "idx": expected_index},
            )
        versions.append(version)
    assert changed.calls == 1
    assert versions[0] == versions[1]
    chunks = await catalog.list_chunks(kb.id, registration.document.id)
    assert any("New parser result" in chunk.content for chunk in chunks)
