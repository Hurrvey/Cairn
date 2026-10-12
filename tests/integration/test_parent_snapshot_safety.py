from __future__ import annotations

from pathlib import Path

from tests.integration.test_ingestion_pipeline import (
    _create_pipeline_document,
    _execute_pipeline,
    _test_pipeline,
    pipeline_admin,
)

from cairn.authz.model import Principal
from cairn.authz.service import AuthzService
from cairn.catalog.config import ChunkConfig
from cairn.core.cache import get_cache
from cairn.core.ids import encode_id
from cairn.core.retrieval_runtime import KnowledgeBaseRuntime
from cairn.retrieval.dto import RetrievalRequest
from cairn.retrieval.service import RetrievalService

__all__ = ["pipeline_admin"]


async def test_manual_edit_disables_sibling_parent_expansion_in_published_runtime(
    pipeline_admin: Principal,
    tmp_path: Path,
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(
        pipeline_admin,
        tmp_path,
        source=b"# Planets\n\n" + b"Saturn has icy rings and many moons. " * 30,
        chunk_config=ChunkConfig(strategy="parent_child", child_tokens=64, child_overlap=0),
    )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    published = await catalog.publish_runtime(kb.id)
    assert published is not None and published.parent_snapshots_safe
    assert registration.document is not None
    chunks = await catalog.list_chunks(kb.id, registration.document.id)
    children = [chunk for chunk in chunks if chunk.parent_id is not None]
    assert len(children) > 1
    await catalog.edit_chunk(pipeline_admin, kb.id, children[0].id, "Corrected planetary data.")
    raw = await get_cache().get(f"kb:runtime:{kb.id}")
    assert raw is not None
    runtime = KnowledgeBaseRuntime.model_validate_json(raw)
    assert runtime.parent_snapshots_safe is False
    republished = await catalog.publish_runtime(kb.id)
    assert republished is not None and republished.parent_snapshots_safe is False


async def test_weighted_query_expands_fresh_context_then_fails_closed_after_edit(
    pipeline_admin: Principal,
    tmp_path: Path,
) -> None:
    catalog, kb, registration, *_ = await _create_pipeline_document(
        pipeline_admin,
        tmp_path,
        source=b"# Planets\n\n" + b"Saturn has icy rings and many moons. " * 30,
        chunk_config=ChunkConfig(strategy="parent_child", child_tokens=64, child_overlap=0),
    )
    pipeline, *_ = await _test_pipeline(kb, tmp_path)
    await _execute_pipeline(pipeline)
    actor = await AuthzService().principal_for_user(pipeline_admin.id)
    assert actor is not None
    query = RetrievalRequest.model_validate(
        {
            "targets": [{"knowledge_base_id": encode_id("kb", kb.id)}],
            "query": "Saturn",
            "query_vector": [1, 1, 1],
            "search_mode": "hybrid",
            "fusion": {"method": "weighted"},
            "weights": {"dense": 0.2, "sparse": 0.8},
            "rerank": {"enabled": False},
            "options": {"expand_parent": True},
        }
    )
    response = await RetrievalService().query(actor, query, request_id="req_parent_before")
    assert response.results and response.results[0].matched_child_ids
    assert "fused" in response.results[0].scores
    assert "_parent_snapshot" not in response.results[0].metadata
    assert registration.document is not None
    chunks = await catalog.list_chunks(kb.id, registration.document.id)
    child = next(chunk for chunk in chunks if chunk.parent_id is not None)
    await catalog.edit_chunk(pipeline_admin, kb.id, child.id, "Updated planetary evidence.")
    response = await RetrievalService().query(actor, query, request_id="req_parent_after")
    assert response.results and all(not hit.matched_child_ids for hit in response.results)
    assert any(notice.stage == "expand_parent" for notice in response.degraded)
