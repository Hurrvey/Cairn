"""Knowledge base, document, chunk, and storage binding endpoints.

Route shape follows the ownership graph rather than the tables: documents and
chunks are always addressed *under* their knowledge base. That is not cosmetic
— it is what lets `require_permission(..., resource_param="kb_id")` scope every
check to a specific knowledge base, instead of each handler having to re-derive
which knowledge base a document belongs to before it can authorize anything.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from cairn.authz.deps import current_principal, require_permission
from cairn.authz.model import Principal
from cairn.catalog.dto import (
    CreateKbSpec,
    ReindexEstimate,
    ReindexSpec,
    UpdateKbSpec,
    UploadSpec,
)
from cairn.catalog.schemas import (
    BindingResponse,
    ChunkResponse,
    CreateBindingRequest,
    CreateKbRequest,
    DocumentRegistrationResponse,
    DocumentResponse,
    EditChunkRequest,
    IndexProgressResponse,
    IndexVersionResponse,
    KnowledgeBaseResponse,
    RegisterUploadRequest,
    ReindexEstimateResponse,
    ReindexRequest,
    UpdateKbRequest,
)
from cairn.catalog.service import CatalogService, get_catalog_service
from cairn.core.ids import decode_id
from cairn.core.pagination import MAX_LIMIT, CursorPage, decode_cursor

__all__ = ["router"]

router = APIRouter(prefix="/v1", tags=["catalog"])

PROBLEM: dict[int | str, dict[str, Any]] = {
    403: {"content": {"application/problem+json": {}}},
    404: {"content": {"application/problem+json": {}}},
    409: {"content": {"application/problem+json": {}}},
}

#: One request registers a whole upload batch. Capped because each entry becomes
#: a row plus a queued task, and an unbounded list is an unbounded transaction.
MAX_BATCH = 100


def _service() -> CatalogService:
    return get_catalog_service()


def _cursor_id(prefix: str, cursor: str | None) -> UUID | None:
    """Decode the `id` key from an opaque cursor, if present.

    The cursor carries a prefixed public id, so `decode_id` still rejects a
    cursor minted for a different resource type.
    """
    if cursor is None:
        return None
    raw = decode_cursor(cursor).get("id")
    return decode_id(prefix, str(raw)) if raw else None


class RegisterUploadsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    documents: list[RegisterUploadRequest] = Field(min_length=1, max_length=MAX_BATCH)


class DocumentStateCounts(BaseModel):
    """Counts by pipeline state — what the ingestion progress bar is built from."""

    counts: dict[str, int]
    total: int


# ------------------------------------------------------------ knowledge bases


@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEM,
    summary="Create a knowledge base",
    description=(
        "The embedding model is validated here, before anything is written. It "
        "becomes immutable once the knowledge base has been indexed (ADR-0006)."
    ),
)
async def create_knowledge_base(
    actor: Annotated[Principal, Depends(require_permission("kb:create"))],
    body: CreateKbRequest,
    service: Annotated[CatalogService, Depends(_service)],
) -> KnowledgeBaseResponse:
    view = await service.create_kb(
        actor,
        CreateKbSpec(
            name=body.name,
            embedding_model_id=decode_id("mdl", body.embedding_model_id),
            vector_binding_id=decode_id("bind", body.vector_binding_id),
            object_binding_id=decode_id("bind", body.object_binding_id),
            description=body.description,
            slug=body.slug,
            metric=body.metric,
            chunk_config=body.chunk_config,
            retrieval_config=body.retrieval_config,
            metadata=body.metadata,
        ),
    )
    return KnowledgeBaseResponse.from_dto(view)


@router.get(
    "/knowledge-bases",
    response_model=CursorPage[KnowledgeBaseResponse],
    responses=PROBLEM,
    summary="List knowledge bases",
    description=(
        "Administrators see the whole workspace; everyone else sees only the "
        "knowledge bases they hold a grant on. The filter is applied in the "
        "query, so inaccessible rows are never fetched."
    ),
)
async def list_knowledge_bases(
    # No per-resource permission: the accessible-set filter IS the
    # authorization, and a workspace-wide `kb:read` would be wrong here.
    actor: Annotated[Principal, Depends(current_principal)],
    service: Annotated[CatalogService, Depends(_service)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> CursorPage[KnowledgeBaseResponse]:
    # One extra row tells us whether another page exists without a COUNT.
    views = await service.list_kbs(actor, limit=limit + 1, cursor_id=_cursor_id("kb", cursor))
    items = [KnowledgeBaseResponse.from_dto(view) for view in views]
    return CursorPage.build(
        items,
        limit=limit,
        cursor_fields={"id": items[limit - 1].id} if len(items) > limit else None,
    )


@router.get(
    "/knowledge-bases/{kb_id}",
    response_model=KnowledgeBaseResponse,
    responses=PROBLEM,
    summary="Get a knowledge base",
)
async def get_knowledge_base(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:read", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse.from_dto(await service.get_kb(decode_id("kb", kb_id)))


@router.patch(
    "/knowledge-bases/{kb_id}",
    response_model=KnowledgeBaseResponse,
    responses=PROBLEM,
    summary="Update a knowledge base",
    description=(
        "Changing the embedding model or metric on an indexed knowledge base is "
        "refused with a pointer to the reindex endpoint, because mixing "
        "embedding spaces in one index degrades retrieval silently."
    ),
)
async def update_knowledge_base(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:manage", "kb_id"))],
    body: UpdateKbRequest,
    service: Annotated[CatalogService, Depends(_service)],
) -> KnowledgeBaseResponse:
    view = await service.update_kb(
        actor,
        decode_id("kb", kb_id),
        UpdateKbSpec(
            name=body.name,
            description=body.description,
            chunk_config=body.chunk_config,
            retrieval_config=body.retrieval_config,
            metadata=body.metadata,
            embedding_model_id=(
                decode_id("mdl", body.embedding_model_id) if body.embedding_model_id else None
            ),
            metric=body.metric,
        ),
    )
    return KnowledgeBaseResponse.from_dto(view)


@router.delete(
    "/knowledge-bases/{kb_id}",
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEM,
    summary="Delete a knowledge base",
    description=(
        "Accepted, not completed: the row is marked and a purge task removes the "
        "documents, chunks, vectors, and objects in the background."
    ),
)
async def delete_knowledge_base(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:manage", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> Response:
    await service.delete_kb(actor, decode_id("kb", kb_id))
    return Response(status_code=status.HTTP_202_ACCEPTED)


# ------------------------------------------------------------- index versions


@router.post(
    "/knowledge-bases/{kb_id}/reindex",
    responses=PROBLEM,
    summary="Rebuild the index",
    description=(
        "Without `confirm` this returns a cost estimate and starts nothing. "
        "With it, a new index version begins building while the active one "
        "keeps serving queries (ADR-0007)."
    ),
)
async def start_reindex(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:manage", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
    body: Annotated[ReindexRequest, Body()] = ReindexRequest(),
) -> ReindexEstimateResponse | IndexVersionResponse:
    result = await service.start_reindex(
        actor,
        decode_id("kb", kb_id),
        ReindexSpec(
            embedding_model_id=(
                decode_id("mdl", body.embedding_model_id) if body.embedding_model_id else None
            ),
            chunk_config=body.chunk_config,
            confirm=body.confirm,
            reason=body.reason,
        ),
    )
    if isinstance(result, ReindexEstimate):
        return ReindexEstimateResponse.from_dto(result)
    return IndexVersionResponse.from_dto(result)


@router.get(
    "/knowledge-bases/{kb_id}/index-versions",
    response_model=list[IndexVersionResponse],
    responses=PROBLEM,
    summary="List index versions",
    description="Newest first. A failed build keeps its error so it stays diagnosable.",
)
async def list_index_versions(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:read", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> list[IndexVersionResponse]:
    rows = await service.list_index_versions(decode_id("kb", kb_id))
    return [IndexVersionResponse.from_dto(row) for row in rows]


@router.get(
    "/knowledge-bases/{kb_id}/index-progress",
    response_model=IndexProgressResponse,
    responses=PROBLEM,
    summary="Index build progress",
)
async def get_index_progress(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:read", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> IndexProgressResponse:
    return IndexProgressResponse.from_dto(await service.get_index_progress(decode_id("kb", kb_id)))


# ------------------------------------------------------------------ documents


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=list[DocumentRegistrationResponse],
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEM,
    summary="Register uploaded documents",
    description=(
        "Registers content already written to object storage and queues it for "
        "parsing. The bytes never pass through this endpoint, so a large upload "
        "does not occupy an API worker. Content already present in this "
        "knowledge base comes back as `skipped`, which is a success."
    ),
)
async def register_documents(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:write", "kb_id"))],
    body: RegisterUploadsRequest,
    service: Annotated[CatalogService, Depends(_service)],
) -> list[DocumentRegistrationResponse]:
    target = decode_id("kb", kb_id)
    results = []
    for entry in body.documents:
        registration = await service.register_upload(
            actor,
            target,
            UploadSpec(
                filename=entry.filename,
                content_hash=entry.content_hash,
                size_bytes=entry.size_bytes,
                mime_type=entry.mime_type,
                object_key=entry.object_key,
                metadata=entry.metadata,
                title=entry.title,
                source_type=entry.source_type,
                source_ref=entry.source_ref,
            ),
        )
        results.append(DocumentRegistrationResponse.from_dto(registration))
    return results


@router.get(
    "/knowledge-bases/{kb_id}/documents",
    response_model=CursorPage[DocumentResponse],
    responses=PROBLEM,
    summary="List documents",
)
async def list_documents(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:read", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
    state: Annotated[str | None, Query()] = None,
    source_type: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> CursorPage[DocumentResponse]:
    raw_cursor = _cursor_id("doc", cursor)
    views = await service.list_documents(
        decode_id("kb", kb_id),
        state=state,
        source_type=source_type,
        search=search,
        limit=limit + 1,
        cursor_id=raw_cursor,
    )
    items = [DocumentResponse.from_dto(view) for view in views]
    return CursorPage.build(
        items,
        limit=limit,
        cursor_fields={"id": items[limit - 1].id} if len(items) > limit else None,
    )


@router.get(
    "/knowledge-bases/{kb_id}/documents/stats",
    response_model=DocumentStateCounts,
    responses=PROBLEM,
    summary="Document counts by state",
)
async def document_stats(
    kb_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:read", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> DocumentStateCounts:
    counts = await service.document_state_counts(decode_id("kb", kb_id))
    return DocumentStateCounts(counts=counts, total=sum(counts.values()))


@router.get(
    "/knowledge-bases/{kb_id}/documents/{document_id}",
    response_model=DocumentResponse,
    responses=PROBLEM,
    summary="Get a document",
)
async def get_document(
    kb_id: str,
    document_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:read", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> DocumentResponse:
    return DocumentResponse.from_dto(await service.get_document(decode_id("doc", document_id)))


@router.delete(
    "/knowledge-bases/{kb_id}/documents/{document_id}",
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEM,
    summary="Delete a document",
)
async def delete_document(
    kb_id: str,
    document_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:write", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> Response:
    await service.delete_document(actor, decode_id("doc", document_id))
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/knowledge-bases/{kb_id}/documents/{document_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEM,
    summary="Retry a failed document",
    description="Re-runs the pipeline from the start for a document in `failed`.",
)
async def retry_document(
    kb_id: str,
    document_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:write", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> Response:
    await service.retry_document(actor, decode_id("doc", document_id))
    return Response(status_code=status.HTTP_202_ACCEPTED)


# --------------------------------------------------------------------- chunks


@router.get(
    "/knowledge-bases/{kb_id}/documents/{document_id}/chunks",
    response_model=list[ChunkResponse],
    responses=PROBLEM,
    summary="List a document's chunks",
    description=(
        "Defaults to the active index version. Paginated by `after_ordinal` "
        "rather than a cursor, because chunk order within a document is stable "
        "and meaningful."
    ),
)
async def list_chunks(
    kb_id: str,
    document_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:read", "kb_id"))],
    service: Annotated[CatalogService, Depends(_service)],
    index_version: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    after_ordinal: Annotated[int | None, Query(ge=0)] = None,
) -> list[ChunkResponse]:
    views = await service.list_chunks(
        decode_id("kb", kb_id),
        decode_id("doc", document_id),
        index_version=index_version,
        limit=limit,
        after_ordinal=after_ordinal,
    )
    return [ChunkResponse.from_dto(view) for view in views]


@router.patch(
    "/knowledge-bases/{kb_id}/chunks/{chunk_id}",
    response_model=ChunkResponse,
    responses=PROBLEM,
    summary="Edit a chunk",
    description=(
        "Marks the chunk as edited so the next rebuild carries the correction "
        "forward (FR-F-09). Only chunks in the active index version are "
        "editable — an edit to a building version would be discarded by the "
        "next switch, which looks like data loss."
    ),
)
async def edit_chunk(
    kb_id: str,
    chunk_id: str,
    actor: Annotated[Principal, Depends(require_permission("kb:write", "kb_id"))],
    body: EditChunkRequest,
    service: Annotated[CatalogService, Depends(_service)],
) -> ChunkResponse:
    view = await service.edit_chunk(
        actor, decode_id("kb", kb_id), decode_id("chk", chunk_id), body.content
    )
    return ChunkResponse.from_dto(view)


# ------------------------------------------------------------ storage bindings


@router.post(
    "/storage-bindings",
    response_model=BindingResponse,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEM,
    summary="Create a storage binding",
    description=(
        "Reachability is verified before this returns, so a misconfigured "
        "backend fails here rather than when the first document is uploaded."
    ),
)
async def create_binding(
    actor: Annotated[Principal, Depends(require_permission("platform:storage"))],
    body: CreateBindingRequest,
    service: Annotated[CatalogService, Depends(_service)],
) -> BindingResponse:
    ref = await service.create_binding(
        actor,
        kind=body.kind,
        driver=body.driver,
        name=body.name,
        config=body.config,
        is_default=body.is_default,
    )
    return BindingResponse.from_dto(ref)


@router.get(
    "/storage-bindings",
    response_model=list[BindingResponse],
    responses=PROBLEM,
    summary="List storage bindings",
    description=(
        "Guarded by `kb:create` rather than `platform:storage`: choosing a "
        "backend is part of creating a knowledge base, and the response carries "
        "no credentials."
    ),
)
async def list_bindings(
    actor: Annotated[Principal, Depends(require_permission("kb:create"))],
    service: Annotated[CatalogService, Depends(_service)],
    kind: Annotated[str | None, Query(pattern="^(vector|object)$")] = None,
) -> list[BindingResponse]:
    refs = await service.list_bindings(actor.workspace_id, kind)
    return [BindingResponse.from_dto(ref) for ref in refs]


@router.post(
    "/storage-bindings/{binding_id}/test",
    responses=PROBLEM,
    summary="Re-test a storage binding",
)
async def test_binding(
    binding_id: str,
    actor: Annotated[Principal, Depends(require_permission("platform:storage"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> dict[str, bool]:
    return {"healthy": await service.test_binding(decode_id("bind", binding_id))}


@router.delete(
    "/storage-bindings/{binding_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEM,
    summary="Delete a storage binding",
    description="Refused while any knowledge base still points at it.",
)
async def delete_binding(
    binding_id: str,
    actor: Annotated[Principal, Depends(require_permission("platform:storage"))],
    service: Annotated[CatalogService, Depends(_service)],
) -> Response:
    await service.delete_binding(actor, decode_id("bind", binding_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
