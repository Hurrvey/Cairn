"""Custom execution through a trusted scoped resolver, never task-supplied Python."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_one,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import ChunkSpec
from cairn.catalog.ingestion import IngestionRun
from cairn.core.db import transaction
from cairn.ingestion.base import ParsedDocument
from cairn.ingestion.chunkers import DocumentChunker
from cairn.ingestion.custom import (
    CustomChunkExecutor,
    CustomChunkLimits,
    CustomChunkScope,
    FunctionVersionRef,
    chunk_id,
)
from cairn.ingestion.pipeline import register_pipeline_handlers
from cairn.tasks.worker import TaskWorker
from cairn.vectorstore.base import Namespace

__all__ = ["pipeline_admin"]


@pytest.mark.parametrize("injected", [True, False])
async def test_custom_worker_requires_explicit_scoped_executor(
    pipeline_admin: Principal, tmp_path: Path, injected: bool
) -> None:
    catalog, kb, registration, _source, _key = await _create_pipeline_document(
        pipeline_admin, tmp_path, source=b"A trusted custom chunking example."
    )
    assert registration.document is not None
    config = ChunkConfig(
        strategy="custom",
        function_id="approved-chunker",
        function_version=7,
        child_tokens=64,
        child_overlap=0,
        min_chunk_tokens=8,
    )
    async with transaction() as session:
        await session.execute(
            text("UPDATE knowledge_base SET chunk_config=CAST(:config AS jsonb) WHERE id=:id"),
            {"id": kb.id, "config": json.dumps(config.model_dump(mode="json"))},
        )
    (
        pipeline,
        provider,
        _objects,
        vectors,
        _resolved_objects,
        _resolved_vectors,
    ) = await _test_pipeline(kb, tmp_path)
    calls: list[tuple[UUID, UUID, FunctionVersionRef]] = []
    prepared = await pipeline._resources.embedding_for(kb.embedding_model_id)

    class TrustedExecutor:
        identity = "audited-test-implementation:7"

        async def execute(
            self,
            ref: FunctionVersionRef,
            doc: ParsedDocument,
            cfg: ChunkConfig,
            scope: CustomChunkScope,
            limits: CustomChunkLimits,
        ) -> list[ChunkSpec]:
            assert ref == FunctionVersionRef("approved-chunker", 7)
            assert scope.document_id == registration.document.id
            assert limits.max_chunks > 0
            fixed = cfg.model_copy(update={"strategy": "fixed"})
            chunks = await DocumentChunker(
                document_id=scope.document_id,
                tokenizer=prepared.tokenizer,
                index_version=scope.index_version,
            ).chunk(doc, fixed)
            return [
                replace(
                    chunk,
                    id=chunk_id(
                        scope,
                        ordinal=chunk.ordinal,
                        role="standalone",
                        content_hash=chunk.content_hash,
                        citations=chunk.metadata["citations"],
                    ),
                )
                for chunk in chunks
            ]

    async def resolve(run: IngestionRun) -> CustomChunkExecutor:
        assert run.workspace_id == pipeline_admin.workspace_id
        assert run.kb_id == kb.id
        assert run.chunk_config.function_version == 7
        calls.append((run.workspace_id, run.kb_id, FunctionVersionRef("approved-chunker", 7)))
        return TrustedExecutor()

    if injected:
        pipeline._resources = replace(pipeline._resources, custom_executor_for=resolve)
    queues = ("parse", "chunk", "embed", "index") if injected else ("parse", "chunk")
    for queue in queues:
        worker = TaskWorker(queue, concurrency=1)
        register_pipeline_handlers(worker, pipeline)
        await _execute_one(worker)
    document = await catalog.get_document(registration.document.id)
    if injected:
        assert document.state == "indexed"
        assert len(calls) == 1
        assert await vectors.count(Namespace(kb.id, 1)) == 1
        assert len(provider.calls) == 1
    else:
        assert document.state == "failed"
        assert document.error_code == "CHUNK_UNSUPPORTED_STRATEGY"
        assert calls == [] and provider.calls == []
