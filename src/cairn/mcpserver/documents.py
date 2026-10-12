"""Permission-scoped knowledge directory and ordered indexed passages.

Only runtime projections and vector payloads are read. Indexed chunks can overlap;
this service never guesses how to reconstruct source files from them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol
from uuid import UUID

from cairn.authz.model import Principal
from cairn.core.errors import NotFound, PayloadTooLarge, ValidationFailed
from cairn.core.ids import InvalidIdError, decode_id, encode_id
from cairn.core.retrieval_runtime import KnowledgeBaseRuntime
from cairn.retrieval.errors import (
    FilterRequiresRebuild,
    KbRuntimeNotPublished,
    RetrievalReadModelInvalid,
)
from cairn.retrieval.runtime import KnowledgeBaseRuntimeLoader
from cairn.vectorstore.base import Namespace, VectorStore
from cairn.vectorstore.filters import Compare
from cairn.vectorstore.registry import VectorBindingRef, get_vector_registry

MAX_POINTS = 10_000
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_BYTES = 440 * 1024
NOTE = "Indexed passages in document order; passages may overlap. This is not the original file."
FIELDS = (
    "chunk_id",
    "kb_id",
    "document_id",
    "index_version",
    "revision",
    "ordinal",
    "content",
    "token_count",
    "doc_title",
    "doc_type",
    "doc_updated_at",
)


class RuntimeReader(Protocol):
    async def load_one(self, kb_id: UUID, workspace_id: UUID) -> KnowledgeBaseRuntime: ...


class Stores(Protocol):
    async def for_binding(self, binding: VectorBindingRef) -> VectorStore: ...


class KnowledgeDocuments:
    def __init__(
        self,
        *,
        runtime_loader: RuntimeReader | None = None,
        vectors: Stores | None = None,
    ) -> None:
        self._runtime = runtime_loader or KnowledgeBaseRuntimeLoader()
        self._vectors = vectors or get_vector_registry()

    @staticmethod
    def authorized(principal: Principal, permission: str) -> list[UUID]:
        return sorted(
            (kb for kb in principal.accessible_kb_ids if principal.can(permission, kb)),
            key=lambda kb: kb.int,
        )

    async def directory(
        self, principal: Principal, *, permission: str = "kb:query"
    ) -> dict[str, Any]:
        ids = self.authorized(principal, permission)
        entries: list[dict[str, Any]] = []

        async def entry(kb: UUID) -> dict[str, Any]:
            public_id = encode_id("kb", kb)
            try:
                runtime = await self._runtime.load_one(kb, principal.workspace_id)
            except KbRuntimeNotPublished:
                return {"id": public_id, "name": public_id, "status": "not_ready"}
            return {
                **runtime.directory.model_dump(mode="json"),
                "id": public_id,
                "name": runtime.directory.name or public_id,
                "status": "ready",
                "permissions": [p for p in ("kb:query", "kb:read") if principal.can(p, kb)],
            }

        for offset in range(0, min(len(ids), 500), 16):
            entries.extend(
                await asyncio.gather(*(entry(kb) for kb in ids[offset : min(offset + 16, 500)]))
            )
        return {"knowledge_bases": entries, "truncated": len(ids) > 500}

    async def document(
        self,
        principal: Principal,
        document_id: str,
        knowledge_base_id: str | None = None,
        *,
        max_characters: int = 50_000,
    ) -> dict[str, Any]:
        if not 1 <= max_characters <= 200_000:
            raise ValidationFailed("max_characters must be between 1 and 200000.")
        allowed = self.authorized(principal, "kb:read")
        try:
            kb = (
                decode_id("kb", knowledge_base_id)
                if knowledge_base_id
                else (allowed[0] if len(allowed) == 1 else None)
            )
            doc = decode_id("doc", document_id)
        except (InvalidIdError, ValueError) as exc:
            raise NotFound("Document not available.") from exc
        if kb is None:
            if not allowed:
                raise NotFound("Document not available.")
            raise ValidationFailed(
                "knowledge_base_id is required when more than one KB is readable."
            )
        if kb not in allowed:
            raise NotFound("Document not available.")
        async with asyncio.timeout(10):
            runtime = await self._runtime.load_one(kb, principal.workspace_id)
            if runtime.payload_schema < 2:
                raise FilterRequiresRebuild("Rebuild the index to enable ordered document reading.")
            binding = runtime.vector_binding
            store = await self._vectors.for_binding(
                VectorBindingRef(binding.id, binding.driver, binding.config)
            )
            rows: list[dict[str, Any]] = []
            seen: set[UUID] = set()
            cursor: UUID | None = None
            revision: int | None = None
            size = 0
            while True:
                hits, following = await store.scroll(
                    Namespace(kb, runtime.index_version),
                    Compare("document_id", "$eq", str(doc)),
                    limit=64,
                    cursor=cursor,
                    payload_fields=FIELDS,
                )
                for hit in hits:
                    row = dict(hit.payload)
                    current_revision, ordinal = row.get("revision"), row.get("ordinal")
                    if (
                        hit.id in seen
                        or row.get("chunk_id") != str(hit.id)
                        or row.get("kb_id") != str(kb)
                        or row.get("document_id") != str(doc)
                        or row.get("index_version") != runtime.index_version
                        or type(ordinal) is not int
                        or ordinal < 0
                        or type(current_revision) is not int
                        or current_revision < 1
                        or (revision is not None and revision != current_revision)
                        or not isinstance(row.get("content"), str)
                    ):
                        raise RetrievalReadModelInvalid(
                            "The indexed document has inconsistent passage facts."
                        )
                    revision = current_revision
                    seen.add(hit.id)
                    size += len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
                    if len(seen) > MAX_POINTS or size > MAX_SCAN_BYTES:
                        raise PayloadTooLarge(
                            "Document exceeds MCP scan limits; use the HTTP content endpoint."
                        )
                    rows.append(row)
                if following is None:
                    break
                if not hits or (cursor is not None and following.int <= cursor.int):
                    raise RetrievalReadModelInvalid("The document page cursor did not advance.")
                cursor = following
        if not rows:
            raise NotFound("Document not available.")
        rows.sort(key=lambda row: (row["ordinal"], row["chunk_id"]))
        passages = []
        remaining, byte_budget = max_characters, MAX_DOCUMENT_BYTES
        truncated = False
        for row in rows:
            if remaining <= 0 or byte_budget < 256:
                truncated = True
                break
            content = row["content"][:remaining]
            passage = {
                "chunk_id": encode_id("chk", UUID(row["chunk_id"])),
                "ordinal": row["ordinal"],
                "content": content,
            }
            # Bound actual JSON bytes, including escapes, without splitting Unicode code points.
            low, high = 0, len(content)
            while low < high:
                middle = (low + high + 1) // 2
                passage["content"] = content[:middle]
                if len(json.dumps(passage, ensure_ascii=False).encode("utf-8")) <= byte_budget:
                    low = middle
                else:
                    high = middle - 1
            passage["content"] = content[:low]
            used = len(json.dumps(passage, ensure_ascii=False).encode("utf-8"))
            if used > byte_budget:
                truncated = True
                break
            passages.append(passage)
            byte_budget -= used + 2
            remaining -= low
            if low < len(row["content"]):
                truncated = True
                break
        return {
            "knowledge_base_id": encode_id("kb", kb),
            "document_id": document_id,
            "index_version": runtime.index_version,
            "revision": revision,
            "title": str(rows[0].get("doc_title") or "")[:512],
            "note": NOTE,
            "passages": passages,
            "truncated": truncated,
        }
