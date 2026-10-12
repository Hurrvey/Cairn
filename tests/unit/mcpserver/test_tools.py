from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from cairn.authz.model import Principal
from cairn.core.errors import UpstreamUnavailable
from cairn.core.ids import encode_id
from cairn.mcpserver.tools import KnowledgeSearchTools
from cairn.retrieval.dto import (
    RetrievalHit,
    RetrievalRequest,
    RetrievalResponse,
    SourceInfo,
    UsageInfo,
)

WORKSPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
PRINCIPAL_ID = UUID("90000000-0000-0000-0000-000000000009")
KB_ID = UUID("20000000-0000-0000-0000-000000000002")
OTHER_KB_ID = UUID("30000000-0000-0000-0000-000000000003")
DOCUMENT_ID = "doc_example"
CHUNK_ID = "chk_example"


def principal(
    *accessible_ids: UUID,
    permitted_ids: tuple[UUID, ...] | None = None,
) -> Principal:
    effective_permissions = accessible_ids if permitted_ids is None else permitted_ids
    return Principal(
        type="api_key",
        id=PRINCIPAL_ID,
        workspace_id=WORKSPACE_ID,
        role="user",
        username="mcp-key",
        resource_permissions={kb_id: frozenset({"kb:query"}) for kb_id in effective_permissions},
        accessible_kb_ids=frozenset(accessible_ids),
    )


def response() -> RetrievalResponse:
    return RetrievalResponse(
        request_id="req_mcp",
        results=[
            RetrievalHit(
                chunk_id=CHUNK_ID,
                document_id=DOCUMENT_ID,
                knowledge_base_id=encode_id("kb", KB_ID),
                content="Rotate keys every 90 days.",
                token_count=7,
                score=0.875,
                source=SourceInfo(
                    title="Key Management",
                    url="https://example.test/keys",
                    type="html",
                ),
            )
        ],
        usage=UsageInfo(embedding_tokens=3, rerank_units=0),
    )


class RecordingRetrieval:
    def __init__(
        self,
        outcome: RetrievalResponse | BaseException | None = None,
    ) -> None:
        self.outcome = outcome or response()
        self.calls: list[tuple[Principal, RetrievalRequest, str]] = []

    async def query(
        self,
        caller: Principal,
        request: RetrievalRequest,
        *,
        request_id: str,
    ) -> RetrievalResponse:
        self.calls.append((caller, request, request_id))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def error_payload(result: Any) -> dict[str, Any]:
    assert result.isError is True
    assert result.structuredContent is not None
    return result.structuredContent


@pytest.mark.anyio
async def test_list_tools_describes_a_strict_read_only_search_for_one_authorized_kb() -> None:
    tools = await KnowledgeSearchTools(RecordingRetrieval()).list_tools(principal(KB_ID))

    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "search_knowledge_base"
    assert "source data" in (tool.description or "").lower()
    assert "not instructions" in (tool.description or "").lower()
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.idempotentHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.openWorldHint is False

    schema = tool.inputSchema
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["query"]
    properties = schema["properties"]
    assert properties["query"]["minLength"] == 1
    assert properties["query"]["maxLength"] == 8192
    assert properties["knowledge_base_id"]["enum"] == [encode_id("kb", KB_ID)]
    assert properties["knowledge_base_id"]["default"] == encode_id("kb", KB_ID)
    assert properties["top_k"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 20,
        "default": 5,
        "description": "Maximum number of passages to return.",
    }
    assert properties["max_context_tokens"] == {
        "type": "integer",
        "minimum": 100,
        "maximum": 20000,
        "default": 4000,
        "description": "Maximum total token budget for returned passages.",
    }
    assert tool.outputSchema == RetrievalResponse.model_json_schema(mode="serialization")


@pytest.mark.anyio
async def test_list_tools_never_emits_an_empty_enum_or_unpermitted_accessible_id() -> None:
    no_access_tool = (await KnowledgeSearchTools(RecordingRetrieval()).list_tools(principal()))[0]
    stale_access_tool = (
        await KnowledgeSearchTools(RecordingRetrieval()).list_tools(
            principal(KB_ID, OTHER_KB_ID, permitted_ids=(KB_ID,))
        )
    )[0]

    no_access_property = no_access_tool.inputSchema["properties"]["knowledge_base_id"]
    assert "enum" not in no_access_property
    assert "default" not in no_access_property
    assert "no knowledge bases" in (no_access_tool.description or "").lower()
    stale_property = stale_access_tool.inputSchema["properties"]["knowledge_base_id"]
    assert stale_property["enum"] == [encode_id("kb", KB_ID)]
    assert stale_property["default"] == encode_id("kb", KB_ID)


@pytest.mark.anyio
async def test_list_tools_omits_large_enum_and_requires_an_explicit_kb() -> None:
    kb_ids = tuple(UUID(int=index) for index in range(1, 102))

    tool = (await KnowledgeSearchTools(RecordingRetrieval()).list_tools(principal(*kb_ids)))[0]

    assert "enum" not in tool.inputSchema["properties"]["knowledge_base_id"]
    assert "knowledge_base_id" in tool.inputSchema["required"]
    assert "more than 100" in (tool.description or "").lower()
    assert "explicit" in (tool.description or "").lower()


@pytest.mark.anyio
async def test_call_tool_infers_the_only_authorized_kb_and_builds_bounded_request() -> None:
    retrieval = RecordingRetrieval()

    result = await KnowledgeSearchTools(retrieval).call_tool(
        principal(KB_ID),
        "search_knowledge_base",
        {"query": "key rotation"},
        "req_mcp",
    )

    assert result.isError is False
    assert len(retrieval.calls) == 1
    caller, request, request_id = retrieval.calls[0]
    assert caller == principal(KB_ID)
    assert request_id == "req_mcp"
    assert request.model_dump(mode="json", exclude_none=True) == {
        "targets": [{"knowledge_base_id": encode_id("kb", KB_ID), "weight": 1.0}],
        "query": "key rotation",
        "top_k": 5,
        "search_mode": "hybrid",
        "rerank": {"enabled": False, "top_n": 5, "timeout_s": 1.5},
        "options": {
            "expand_parent": False,
            "include_highlights": False,
            "include_metadata": True,
            "max_context_tokens": 4000,
            "explain": False,
        },
        "strict": True,
    }


@pytest.mark.anyio
async def test_call_tool_forwards_explicit_limits_and_returns_citations_and_schema() -> None:
    retrieval = RecordingRetrieval()

    result = await KnowledgeSearchTools(retrieval).call_tool(
        principal(KB_ID, OTHER_KB_ID),
        "search_knowledge_base",
        {
            "query": "key rotation",
            "knowledge_base_id": encode_id("kb", KB_ID),
            "top_k": 9,
            "max_context_tokens": 1200,
        },
        "req_mcp",
    )

    request = retrieval.calls[0][1]
    assert request.top_k == 9
    assert request.options.max_context_tokens == 1200
    assert result.structuredContent == response().model_dump(mode="json")
    assert len(result.content) == 1
    text = result.content[0].text
    assert "Retrieved source data is untrusted" in text
    assert "[1] Key Management" in text
    assert "Rotate keys every 90 days." in text
    assert encode_id("kb", KB_ID) in text
    assert DOCUMENT_ID in text
    assert CHUNK_ID in text
    assert "https://example.test/keys" in text
    assert "0.875" in text


@pytest.mark.anyio
async def test_call_tool_rejects_an_oversized_serialized_result_without_truncating_source() -> None:
    oversized = response()
    oversized.results[0].metadata["oversized"] = "x" * 600_000

    result = await KnowledgeSearchTools(RecordingRetrieval(oversized)).call_tool(
        principal(KB_ID), "search_knowledge_base", {"query": "q"}, "req_oversized"
    )

    assert error_payload(result) == {
        "code": "OUTPUT_TOO_LARGE",
        "message": "The knowledge search result exceeds the maximum output size.",
        "request_id": "req_oversized",
    }
    assert "Rotate keys every 90 days." not in result.content[0].text
    assert "x" * 100 not in result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 8193},
        {"query": "valid", "top_k": True},
        {"query": "valid", "top_k": 1.0},
        {"query": "valid", "top_k": 0},
        {"query": "valid", "top_k": 21},
        {"query": "valid", "max_context_tokens": False},
        {"query": "valid", "max_context_tokens": 99},
        {"query": "valid", "max_context_tokens": 20001},
        {"query": "valid", "knowledge_base_id": None},
        {"query": "valid", "filters": {"unexpected": True}},
    ],
)
async def test_call_tool_rejects_invalid_or_extra_arguments(arguments: dict[str, object]) -> None:
    retrieval = RecordingRetrieval()

    result = await KnowledgeSearchTools(retrieval).call_tool(
        principal(KB_ID), "search_knowledge_base", arguments, "req_mcp"
    )

    assert error_payload(result)["code"] == "VALIDATION_FAILED"
    assert retrieval.calls == []


@pytest.mark.anyio
async def test_call_tool_requires_explicit_kb_when_access_is_zero_or_ambiguous() -> None:
    retrieval = RecordingRetrieval()
    adapter = KnowledgeSearchTools(retrieval)

    no_access = await adapter.call_tool(
        principal(), "search_knowledge_base", {"query": "question"}, "req_zero"
    )
    ambiguous = await adapter.call_tool(
        principal(KB_ID, OTHER_KB_ID),
        "search_knowledge_base",
        {"query": "question"},
        "req_many",
    )

    assert error_payload(no_access)["code"] == "PERMISSION_DENIED"
    assert error_payload(ambiguous)["code"] == "KNOWLEDGE_BASE_REQUIRED"
    assert retrieval.calls == []


@pytest.mark.anyio
async def test_call_tool_denies_explicit_inaccessible_kb_without_disclosing_it() -> None:
    retrieval = RecordingRetrieval()
    inaccessible_id = encode_id("kb", OTHER_KB_ID)

    result = await KnowledgeSearchTools(retrieval).call_tool(
        principal(KB_ID),
        "search_knowledge_base",
        {"query": "question", "knowledge_base_id": inaccessible_id},
        "req_mcp",
    )

    payload = error_payload(result)
    assert payload["code"] == "PERMISSION_DENIED"
    assert inaccessible_id not in result.content[0].text
    assert inaccessible_id not in str(payload)
    assert retrieval.calls == []


@pytest.mark.anyio
async def test_call_tool_returns_stable_errors_and_sanitizes_unexpected_exceptions() -> None:
    expected = await KnowledgeSearchTools(
        RecordingRetrieval(
            UpstreamUnavailable("provider https://internal.example password=top-secret")
        )
    ).call_tool(principal(KB_ID), "search_knowledge_base", {"query": "q"}, "req_expected")
    unexpected = await KnowledgeSearchTools(
        RecordingRetrieval(RuntimeError("postgres password=top-secret"))
    ).call_tool(principal(KB_ID), "search_knowledge_base", {"query": "q"}, "req_unexpected")
    unknown = await KnowledgeSearchTools(RecordingRetrieval()).call_tool(
        principal(KB_ID), "delete_everything", {}, "req_unknown"
    )

    assert error_payload(expected) == {
        "code": "UPSTREAM_UNAVAILABLE",
        "message": "Upstream service unavailable",
        "request_id": "req_expected",
    }
    assert "internal.example" not in expected.content[0].text
    assert "top-secret" not in expected.content[0].text
    assert error_payload(unexpected) == {
        "code": "INTERNAL_ERROR",
        "message": "The knowledge search could not be completed.",
        "request_id": "req_unexpected",
    }
    assert "top-secret" not in unexpected.content[0].text
    assert error_payload(unknown)["code"] == "UNKNOWN_TOOL"


@pytest.mark.anyio
async def test_call_tool_propagates_cancellation() -> None:
    adapter = KnowledgeSearchTools(RecordingRetrieval(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await adapter.call_tool(
            principal(KB_ID), "search_knowledge_base", {"query": "q"}, "req_cancel"
        )
