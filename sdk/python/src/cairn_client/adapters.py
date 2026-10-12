"""Optional LangChain and LlamaIndex retriever adapters.

Import this module only when the corresponding SDK extra is installed. Adapter
metadata is bounded to Cairn's response facts. It preserves caller-visible user
metadata but never fetches source URLs or serializes client credentials.
"""

from __future__ import annotations

import warnings
from typing import Any

from pydantic import ConfigDict, PrivateAttr

from .client import AsyncCairnClient, CairnClient
from .models import RetrievalRequest, RetrievalResponse

_MAX_WARNING_ITEMS = 8
_KNOWN_STAGES = frozenset(
    {"embedding", "vector", "fulltext", "fusion", "rerank", "search", "sparse", "expand_parent"}
)
_KNOWN_REASONS = frozenset(
    {
        "load_shed",
        "timeout",
        "unavailable",
        "disabled",
        "not_configured",
        "partial",
        "query_text_missing",
        "query_missing",
        "parent_snapshots_unsafe",
        "manual_child_edit",
        "parent_snapshot_missing",
        "parent_snapshot_metadata_missing",
    }
)
_KNOWN_FAILURE_CODES = frozenset(
    {
        "UPSTREAM_FAILED",
        "UPSTREAM_UNAVAILABLE",
        "TIMEOUT",
        "MODEL_UNAVAILABLE",
        "KB_RUNTIME_UNAVAILABLE",
        "EMBEDDING_PROVIDER_UNAVAILABLE",
        "STORAGE_BINDING_UNAVAILABLE",
    }
)


def _known(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "other"


class CairnRetrievalWarning(UserWarning):
    """A retrieval degraded or contained target-level failures."""


def _metadata(response: RetrievalResponse, hit: Any) -> dict[str, Any]:
    return {
        **dict(hit.metadata or {}),
        "cairn": {
            "score": hit.score,
            "scores": dict(hit.scores or {}),
            "chunk_id": hit.chunk_id,
            "document_id": hit.document_id,
            "knowledge_base_id": hit.knowledge_base_id,
            "index_version": (response.usage.index_versions or {}).get(hit.knowledge_base_id),
            "index_versions": dict(response.usage.index_versions or {}),
            "degraded_count": len(response.degraded or []),
            "degraded_truncated": len(response.degraded or []) > _MAX_WARNING_ITEMS,
            "degraded": [
                {
                    "stage": _known(notice.stage, _KNOWN_STAGES),
                    "reason": _known(notice.reason, _KNOWN_REASONS),
                }
                for notice in (response.degraded or [])[:_MAX_WARNING_ITEMS]
            ],
            "partial_failure_count": len(response.partial_failures or []),
            "partial_failures_truncated": len(response.partial_failures or []) > _MAX_WARNING_ITEMS,
            "partial_failures": [
                {
                    "knowledge_base_id": failure.knowledge_base_id,
                    "code": _known(failure.code, _KNOWN_FAILURE_CODES),
                }
                for failure in (response.partial_failures or [])[:_MAX_WARNING_ITEMS]
            ],
        },
    }


def _warn(response: RetrievalResponse) -> None:
    if response.degraded or response.partial_failures:
        reasons = [
            f"{_known(notice.stage, _KNOWN_STAGES)}:{_known(notice.reason, _KNOWN_REASONS)}"
            for notice in (response.degraded or [])[:_MAX_WARNING_ITEMS]
        ] + [
            _known(failure.code, _KNOWN_FAILURE_CODES)
            for failure in (response.partial_failures or [])[:_MAX_WARNING_ITEMS]
        ]
        warnings.warn(
            f"Cairn retrieval degraded: {', '.join(reasons[:_MAX_WARNING_ITEMS])}",
            CairnRetrievalWarning,
            stacklevel=3,
        )


def _raise_target_failures(
    response: RetrievalResponse, strict: bool, knowledge_base_ids: list[str]
) -> None:
    failed = {failure.knowledge_base_id for failure in response.partial_failures or []}
    all_targets_failed = bool(knowledge_base_ids) and failed.issuperset(knowledge_base_ids)
    if response.partial_failures and (strict or all_targets_failed):
        from .client import CairnError

        raise CairnError("Cairn retrieval had target failures.")


def _request(query: str, knowledge_base_ids: list[str], strict: bool) -> RetrievalRequest:
    return RetrievalRequest(
        targets=[{"knowledge_base_id": kb_id} for kb_id in knowledge_base_ids],
        query=query,
        strict=strict,
    )


try:
    from langchain_core.documents import Document
    from langchain_core.retrievers import BaseRetriever

    class CairnLangChainRetriever(BaseRetriever):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        knowledge_base_ids: list[str]
        strict: bool = True
        _client: CairnClient | AsyncCairnClient = PrivateAttr()

        def __init__(
            self,
            *,
            client: CairnClient | AsyncCairnClient,
            knowledge_base_ids: list[str],
            strict: bool = True,
            **kwargs: Any,
        ) -> None:
            super().__init__(knowledge_base_ids=knowledge_base_ids, strict=strict, **kwargs)
            self._client = client

        def _get_relevant_documents(self, query: str, *, run_manager: Any = None) -> list[Document]:
            if not isinstance(self._client, CairnClient):
                raise TypeError("Sync LangChain retrieval requires CairnClient")
            response = self._client.retrieve(_request(query, self.knowledge_base_ids, self.strict))
            _warn(response)
            _raise_target_failures(response, self.strict, self.knowledge_base_ids)
            return [
                Document(page_content=hit.content, metadata=_metadata(response, hit))
                for hit in response.results
            ]

        async def _aget_relevant_documents(
            self, query: str, *, run_manager: Any = None
        ) -> list[Document]:
            if not isinstance(self._client, AsyncCairnClient):
                raise TypeError("Async LangChain retrieval requires AsyncCairnClient")
            response = await self._client.retrieve(
                _request(query, self.knowledge_base_ids, self.strict)
            )
            _warn(response)
            _raise_target_failures(response, self.strict, self.knowledge_base_ids)
            return [
                Document(page_content=hit.content, metadata=_metadata(response, hit))
                for hit in response.results
            ]

except ImportError:  # pragma: no cover - optional dependency
    CairnLangChainRetriever = None  # type: ignore[assignment,misc]


try:
    from llama_index.core.base.base_retriever import BaseRetriever as LlamaBaseRetriever
    from llama_index.core.schema import NodeWithScore, QueryBundle, TextNode

    class CairnLlamaIndexRetriever(LlamaBaseRetriever):
        def __init__(
            self,
            client: CairnClient | AsyncCairnClient,
            knowledge_base_ids: list[str],
            strict: bool = True,
        ) -> None:
            super().__init__()
            self._cairn_client = client
            self._knowledge_base_ids = knowledge_base_ids
            self._strict = strict

        def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
            if not isinstance(self._cairn_client, CairnClient):
                raise TypeError("Sync LlamaIndex retrieval requires CairnClient")
            response = self._cairn_client.retrieve(
                _request(query_bundle.query_str, self._knowledge_base_ids, self._strict)
            )
            _warn(response)
            _raise_target_failures(response, self._strict, self._knowledge_base_ids)
            return [
                NodeWithScore(
                    node=TextNode(text=hit.content, metadata=_metadata(response, hit)),
                    score=hit.score,
                )
                for hit in response.results
            ]

        async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
            if not isinstance(self._cairn_client, AsyncCairnClient):
                raise TypeError("Async LlamaIndex retrieval requires AsyncCairnClient")
            response = await self._cairn_client.retrieve(
                _request(query_bundle.query_str, self._knowledge_base_ids, self._strict)
            )
            _warn(response)
            _raise_target_failures(response, self._strict, self._knowledge_base_ids)
            return [
                NodeWithScore(
                    node=TextNode(text=hit.content, metadata=_metadata(response, hit)),
                    score=hit.score,
                )
                for hit in response.results
            ]

except ImportError:  # pragma: no cover - optional dependency
    CairnLlamaIndexRetriever = None  # type: ignore[assignment,misc]


__all__ = [
    "CairnLangChainRetriever",
    "CairnLlamaIndexRetriever",
    "CairnRetrievalWarning",
]
