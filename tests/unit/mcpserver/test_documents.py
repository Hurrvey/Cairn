from dataclasses import replace
from uuid import UUID

import pytest
from tests.fakes.vectorstore import FakeVectorStore
from tests.unit.retrieval.test_contract_options import DOC_A, Registry, v2
from tests.unit.retrieval.test_service import ALLOWED_KB_ID, Loader, principal

from cairn.core.errors import NotFound
from cairn.core.ids import encode_id
from cairn.mcpserver.documents import KnowledgeDocuments
from cairn.retrieval.errors import KbRuntimeNotPublished, RetrievalReadModelInvalid
from cairn.vectorstore.base import Namespace, NamespaceSpec, Point


async def fixture():
    runtime = v2()
    store = FakeVectorStore()
    ns = Namespace(runtime.id, 1)
    await store.ensure_namespace(ns, NamespaceSpec(dim=3))
    for ordinal, chunk_id in enumerate([UUID(int=3), UUID(int=1), UUID(int=2)]):
        await store.upsert(
            ns,
            [
                Point(
                    id=chunk_id,
                    dense=[1, 0, 0],
                    payload={
                        "chunk_id": str(chunk_id),
                        "kb_id": str(runtime.id),
                        "document_id": str(DOC_A),
                        "index_version": 1,
                        "revision": 1,
                        "ordinal": ordinal,
                        "content": f"overlap passage {ordinal} overlap",
                        "doc_title": "Handbook",
                        "_parent_snapshot": {"content": "stale parent must never appear"},
                    },
                )
            ],
        )
    reader = replace(
        principal(ALLOWED_KB_ID),
        resource_permissions={ALLOWED_KB_ID: frozenset({"kb:read", "kb:query"})},
    )
    service = KnowledgeDocuments(
        runtime_loader=Loader({runtime.id: runtime}), vectors=Registry(store)
    )
    return service, reader, store, ns


async def test_document_keeps_passage_order_overlap_and_excludes_parent_snapshots():
    service, reader, _store, _ns = await fixture()
    result = await service.document(reader, encode_id("doc", DOC_A))
    assert [p["ordinal"] for p in result["passages"]] == [0, 1, 2]
    assert all(p["content"].startswith("overlap") for p in result["passages"])
    assert "stale parent" not in str(result)
    assert result["truncated"] is False


async def test_query_only_cannot_read_document():
    service, _reader, _store, _ns = await fixture()
    with pytest.raises(NotFound):
        await service.document(principal(ALLOWED_KB_ID), encode_id("doc", DOC_A))


async def test_document_truncation_and_corrupt_ordinals():
    service, reader, store, ns = await fixture()
    result = await service.document(reader, encode_id("doc", DOC_A), max_characters=10)
    assert result["truncated"] is True
    assert sum(len(p["content"]) for p in result["passages"]) == 10
    point = (await store.fetch(ns, [UUID(int=1)]))[0]
    await store.upsert(
        ns, [Point(id=point.id, dense=[1, 0, 0], payload={**point.payload, "ordinal": True})]
    )
    with pytest.raises(RetrievalReadModelInvalid):
        await service.document(reader, encode_id("doc", DOC_A))


async def test_missing_runtime_is_distinct_from_backend_failure():
    from cairn.core.cache import InMemoryCache
    from cairn.retrieval.runtime import KnowledgeBaseRuntimeLoader

    service, reader, _store, _ns = await fixture()
    loader = KnowledgeBaseRuntimeLoader(cache=InMemoryCache())
    with pytest.raises(KbRuntimeNotPublished):
        await loader.load_one(ALLOWED_KB_ID, reader.workspace_id)
    service = KnowledgeDocuments(runtime_loader=loader)
    result = await service.directory(reader)
    assert result["knowledge_bases"][0]["status"] == "not_ready"


async def test_scroll_paginates_filtered_payload_without_duplicates():
    from cairn.vectorstore.filters import Compare

    _service, _reader, store, ns = await fixture()
    first, cursor = await store.scroll(
        ns, Compare("document_id", "$eq", str(DOC_A)), limit=2, payload_fields=["ordinal"]
    )
    last, end = await store.scroll(ns, None, limit=2, cursor=cursor, payload_fields=["ordinal"])
    assert [hit.id for hit in first + last] == [UUID(int=1), UUID(int=2), UUID(int=3)]
    assert end is None
    assert all(set(hit.payload) == {"ordinal"} for hit in first + last)


async def test_tools_offer_directory_and_document_with_real_read_service():
    from tests.unit.mcpserver.test_tools import RecordingRetrieval

    from cairn.mcpserver.tools import KnowledgeSearchTools

    service, reader, _store, _ns = await fixture()
    tools = KnowledgeSearchTools(RecordingRetrieval(), documents=service)
    offered = await tools.list_tools(reader)
    assert {tool.name for tool in offered} == {
        "search_knowledge_base",
        "list_knowledge_bases",
        "get_document",
    }
    listing = await tools.call_tool(reader, "list_knowledge_bases", {}, "req_t")
    assert listing.isError is False
    result = await tools.call_tool(
        reader, "get_document", {"document_id": encode_id("doc", DOC_A)}, "req_t"
    )
    assert result.isError is False
    assert len(result.structuredContent["passages"]) == 3


def test_resource_wire_limit_counts_json_escaping():
    from mcp.shared.exceptions import McpError

    from cairn.mcpserver.server import bounded_resource

    with pytest.raises(McpError):
        bounded_resource("cairn://kb/test", {"passages": [{"content": "\\" * 180_000}]})
