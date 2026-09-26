"""Catalog-owned facade for ingestion workers."""

from __future__ import annotations

import asyncio
from base64 import urlsafe_b64encode
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal, TypeVar, cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import BindingRef, ChunkReembedTarget, ChunkSpec, IndexVersionConfig
from cairn.catalog.models import (
    Chunk,
    Document,
    DocumentIngestion,
    KbIndexVersion,
    KnowledgeBase,
    StorageBinding,
)
from cairn.catalog.reindex import activate_build_if_ready
from cairn.catalog.repository import CatalogRepository
from cairn.core.db import session_scope, transaction
from cairn.core.errors import CairnError, NotFound, ValidationFailed
from cairn.core.modelref import ModelRef
from cairn.core.sparse import SparseSpec
from cairn.core.time import utcnow
from cairn.tasks.dto import TaskContext, TaskSpec
from cairn.tasks.service import TaskLeaseLostError, TaskService, get_task_service


class IngestionOwnershipError(CairnError):
    code = "INGESTION_OWNERSHIP_MISMATCH"
    http_status = 409
    title = "The ingestion task no longer owns this document revision."
    retryable = False


@dataclass(frozen=True, slots=True)
class IngestionRun:
    document_id: UUID
    workspace_id: UUID
    kb_id: UUID
    revision: int
    index_version: int
    state: Literal["registered", "parsed", "chunked", "embedded", "indexed", "failed"]
    source_content_hash: str
    object_key: str
    mime_type: str
    source_url: str | None
    version_config: IndexVersionConfig
    object_binding: BindingRef
    vector_binding: BindingRef
    prior_parsed_object_key: str | None
    parsed_object_key: str | None
    chunks_object_key: str | None
    embeddings_object_key: str | None
    prior_embeddings_object_key: str | None
    point_count: int
    stale_point_ids: tuple[UUID, ...]
    active_index_version: int | None
    building_index_version: int | None
    recovery_generation: int = 0

    @property
    def chunk_config(self) -> ChunkConfig:
        return self.version_config.chunk_config

    @property
    def embedding_model(self) -> ModelRef:
        return self.version_config.embedding_model

    @property
    def embedding_model_id(self) -> UUID:
        return self.embedding_model.id

    @property
    def embedding_dim(self) -> int:
        dimension = self.embedding_model.dimension
        if dimension is None:  # pragma: no cover - snapshot validation rejects this
            raise ValidationFailed("The version embedding model has no dimension.")
        return dimension

    @property
    def metric(self) -> str:
        return self.version_config.metric

    @property
    def sparse(self) -> SparseSpec:
        return self.version_config.sparse

    @property
    def artifact_prefix(self) -> str:
        return (
            f"{self.workspace_id}/{self.kb_id}/i/{_compact_uuid(self.document_id)}/"
            f"{self.revision:x}-{self.index_version:x}"
        )


@dataclass(frozen=True, slots=True)
class IndexCommit:
    advanced: bool
    activated: bool


@dataclass(frozen=True, slots=True)
class DeletedBuildTarget:
    document_id: UUID
    workspace_id: UUID
    kb_id: UUID
    index_version: int
    vector_binding: BindingRef
    embedding_dim: int
    metric: str
    sparse_modifier: Literal["idf", "none"] = "idf"


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class IndexMutationGuard:
    _before_external: Callable[[], Awaitable[float]]
    _commit_indexed: Callable[[int], Awaitable[IndexCommit]]

    async def apply(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        remaining = await self._before_external()
        async with asyncio.timeout(remaining):
            return await operation()

    async def commit(self, count: int) -> IndexCommit:
        return await self._commit_indexed(count)


@dataclass(frozen=True, slots=True)
class DeletedIndexMutationGuard:
    target: DeletedBuildTarget
    _before_external: Callable[[], Awaitable[float]]
    _commit_deleted: Callable[[], Awaitable[IndexCommit]]

    async def apply(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        remaining = await self._before_external()
        async with asyncio.timeout(remaining):
            return await operation()

    async def commit(self) -> IndexCommit:
        return await self._commit_deleted()


@dataclass(frozen=True, slots=True)
class ChunkReembedMutationGuard:
    _before_external: Callable[[], Awaitable[float]]
    _commit_applied: Callable[[int], Awaitable[None]]

    async def apply(self, operation: Callable[[], Awaitable[_T]]) -> _T:
        remaining = await self._before_external()
        async with asyncio.timeout(remaining):
            return await operation()

    async def commit(self, actual_token_count: int) -> None:
        await self._commit_applied(actual_token_count)


class CatalogIngestionFacade:
    def __init__(
        self,
        repository: CatalogRepository | None = None,
        *,
        tasks: TaskService | None = None,
    ) -> None:
        self._repo = repository or CatalogRepository()
        self._tasks = tasks or get_task_service()

    async def start_revision(
        self,
        document_id: UUID,
        *,
        object_key: str,
        content_hash: str,
        size_bytes: int,
        mime_type: str,
    ) -> int:
        if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
            raise ValidationFailed("content_hash must be a lowercase SHA-256 digest")
        if size_bytes < 0 or not mime_type:
            raise ValidationFailed("The replacement document metadata is invalid.")
        async with transaction() as session:
            document = await self._repo.get_document(session, document_id, for_update=True)
            if document is None or document.deleted_at is not None:
                raise NotFound("Document not found.")
            kb = await self._repo.get_kb(session, document.kb_id, for_update=True)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            if kb.status in {"deleting", "archived"}:
                raise IngestionOwnershipError("The knowledge base does not accept new revisions.")
            if not source_key_in_scope(
                object_key,
                workspace_id=document.workspace_id,
                kb_id=document.kb_id,
            ):
                raise ValidationFailed("The source object key is outside this document's scope.")
            index_version = kb.building_index_version or kb.active_index_version
            if index_version is None:
                raise IngestionOwnershipError("The knowledge base has no index target.")
            document.revision += 1
            document.object_key = object_key
            document.content_hash = content_hash
            document.size_bytes = size_bytes
            document.mime_type = mime_type
            document.state = "registered"
            document.stage_detail = None
            document.error_code = None
            document.error_detail = None
            document.progress_pct = 0
            document.parsed_object_key = None
            await self._repo.add_document_ingestion(
                session,
                DocumentIngestion(
                    document_id=document.id,
                    revision=document.revision,
                    kb_id=document.kb_id,
                    index_version=index_version,
                    source_content_hash=content_hash,
                ),
            )
            await self._tasks.enqueue(
                session,
                TaskSpec(
                    queue="parse",
                    kind="document.parse",
                    workspace_id=document.workspace_id,
                    kb_id=document.kb_id,
                    document_id=document.id,
                    payload={
                        "revision": document.revision,
                        "index_version": index_version,
                    },
                    dedupe_key=(
                        f"document.parse:{document.id}:{document.revision}:{index_version}"
                    ),
                ),
            )
            revision = document.revision
        await self._tasks.notify("parse")
        return revision

    async def load_run(self, context: TaskContext) -> IngestionRun | None:
        identity = _identity(context)
        if identity is None:
            return None
        document_id, revision, index_version, recovery_generation = identity
        async with session_scope() as session:
            document = await self._repo.get_document(session, document_id)
            run = await self._repo.get_document_ingestion(
                session, document_id, revision, index_version
            )
            if document is None or run is None or document.deleted_at is not None:
                return None
            kb = await self._repo.get_kb(session, run.kb_id)
            if kb is None or kb.status in {"deleting", "archived"}:
                return None
            version = await self._repo.get_index_version(session, run.kb_id, run.index_version)
            version_config = _load_version_config(version)
            object_binding = await self._repo.get_binding(session, kb.object_binding_id)
            vector_binding = await self._repo.get_binding(session, kb.vector_binding_id)
            if object_binding is None or vector_binding is None:
                return None
            if (
                context.workspace_id != document.workspace_id
                or context.kb_id != document.kb_id
                or run.kb_id != document.kb_id
                or kb.workspace_id != document.workspace_id
                or document.revision != revision
                or document.content_hash != run.source_content_hash
                or run.recovery_generation != recovery_generation
                or index_version not in {kb.active_index_version, kb.building_index_version}
                or object_binding.workspace_id != document.workspace_id
                or object_binding.kind != "object"
                or vector_binding.workspace_id != document.workspace_id
                or vector_binding.kind != "vector"
            ):
                return None
            previous_key = await session.scalar(
                select(DocumentIngestion.embeddings_object_key)
                .where(
                    DocumentIngestion.document_id == document_id,
                    DocumentIngestion.embeddings_object_key.is_not(None),
                    ~(
                        (DocumentIngestion.revision == revision)
                        & (DocumentIngestion.index_version == index_version)
                    ),
                )
                .order_by(DocumentIngestion.updated_at.desc())
                .limit(1)
            )
            return _run_view(
                document,
                run,
                kb,
                version_config,
                _binding_ref(object_binding),
                _binding_ref(vector_binding),
                previous_key,
            )

    async def commit_parsed(
        self,
        context: TaskContext,
        *,
        parsed_object_key: str,
        page_count: int,
    ) -> bool:
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            locked = await self._lock_current(session, context)
            if locked is None:
                return False
            document, run, _kb = locked
            if _stage_at_least(run.state, "parsed"):
                return False
            run.state = "parsed"
            run.parsed_object_key = parsed_object_key
            document.state = "parsed"
            document.parsed_object_key = parsed_object_key
            document.page_count = page_count
            document.progress_pct = 25
            document.error_code = None
            document.error_detail = None
            await self._tasks.enqueue(
                session,
                _next_task(context, queue="chunk", kind="document.chunk"),
            )
        return True

    async def existing_point_ids(self, context: TaskContext) -> tuple[UUID, ...]:
        identity = _identity(context)
        if identity is None or context.kb_id is None:
            return ()
        document_id, _revision, index_version, _recovery_generation = identity
        async with session_scope() as session:
            rows = await self._repo.ingestion_chunks(
                session,
                context.kb_id,
                document_id,
                index_version,
            )
        return tuple(row.id for row in rows if row.chunk_metadata.get("embed", True) is not False)

    async def preserved_edits(self, context: TaskContext) -> dict[int, tuple[str, str]]:
        run = await self.load_run(context)
        if run is None or run.active_index_version is None:
            return {}
        async with session_scope() as session:
            rows = await self._repo.edited_chunks(
                session,
                run.kb_id,
                run.document_id,
                run.active_index_version,
            )
        return {row.ordinal: (row.content, row.content_hash) for row in rows}

    async def load_chunk_reembed(self, context: TaskContext) -> ChunkReembedTarget | None:
        identity = _chunk_reembed_identity(context)
        if identity is None or context.kb_id is None or context.document_id is None:
            return None
        chunk_id, revision, index_version, content_hash, edit_generation = identity
        async with session_scope() as session:
            document = await self._repo.get_document(session, context.document_id)
            run = await self._repo.get_document_ingestion(
                session, context.document_id, revision, index_version
            )
            kb = await self._repo.get_kb(session, context.kb_id)
            version = await self._repo.get_index_version(session, context.kb_id, index_version)
            chunk = await self._repo.get_chunk(session, context.kb_id, chunk_id)
            if (
                document is None
                or document.deleted_at is not None
                or document.state == "deleting"
                or run is None
                or run.state != "indexed"
                or kb is None
                or kb.workspace_id != context.workspace_id
                or kb.status in {"deleting", "archived"}
                or kb.active_index_version != index_version
                or version is None
                or version.state != "active"
                or chunk is None
                or chunk.workspace_id != context.workspace_id
                or chunk.document_id != context.document_id
                or chunk.index_version != index_version
                or chunk.content_hash != content_hash
                or chunk.edit_generation != edit_generation
                or not chunk.is_edited
                or chunk.chunk_metadata.get("embed", True) is False
                or document.workspace_id != context.workspace_id
                or document.kb_id != context.kb_id
                or document.revision != revision
                or document.content_hash != run.source_content_hash
                or run.kb_id != context.kb_id
            ):
                return None
            binding = await self._repo.get_binding(session, kb.vector_binding_id)
            if (
                binding is None
                or binding.workspace_id != context.workspace_id
                or binding.kind != "vector"
            ):
                return None
            return _chunk_reembed_target(
                document,
                chunk,
                _load_version_config(version),
                _binding_ref(binding),
            )

    @asynccontextmanager
    async def chunk_reembed_mutation(
        self,
        context: TaskContext,
        expected: ChunkReembedTarget,
        *,
        timeout_s: float = 30.0,
    ) -> AsyncIterator[ChunkReembedMutationGuard]:
        identity = _chunk_reembed_identity(context)
        expected_identity = (
            expected.chunk_id,
            expected.revision,
            expected.index_version,
            expected.content_hash,
            expected.edit_generation,
        )
        if (
            identity != expected_identity
            or context.kb_id != expected.kb_id
            or context.document_id != expected.document_id
            or context.workspace_id != expected.workspace_id
        ):
            raise IngestionOwnershipError()
        await context.heartbeat()
        async with asyncio.timeout(timeout_s), transaction() as session:
            document = await self._repo.get_document(session, expected.document_id, for_update=True)
            run = await self._repo.get_document_ingestion(
                session,
                expected.document_id,
                expected.revision,
                expected.index_version,
                for_update=True,
            )
            kb = await self._repo.get_kb(session, expected.kb_id, for_update=True)
            version = await self._repo.get_index_version(
                session, expected.kb_id, expected.index_version, for_update=True
            )
            chunk = await self._repo.get_chunk(
                session, expected.kb_id, expected.chunk_id, for_update=True
            )
            if (
                document is None
                or document.deleted_at is not None
                or document.state == "deleting"
                or run is None
                or run.state != "indexed"
                or kb is None
                or kb.workspace_id != expected.workspace_id
                or kb.status in {"deleting", "archived"}
                or kb.active_index_version != expected.index_version
                or version is None
                or version.state != "active"
                or chunk is None
                or chunk.workspace_id != expected.workspace_id
                or chunk.document_id != expected.document_id
                or chunk.index_version != expected.index_version
                or chunk.content != expected.content
                or chunk.content_hash != expected.content_hash
                or chunk.edit_generation != expected.edit_generation
                or chunk.parent_id != expected.parent_id
                or chunk.chunk_metadata != expected.metadata
                or not chunk.is_edited
                or chunk.chunk_metadata.get("embed", True) is False
                or document.workspace_id != expected.workspace_id
                or document.kb_id != expected.kb_id
                or document.revision != expected.revision
                or document.content_hash != run.source_content_hash
                or run.kb_id != expected.kb_id
            ):
                raise IngestionOwnershipError()
            version_config = _load_version_config(version)
            binding = await self._repo.get_binding(session, kb.vector_binding_id)
            if (
                version_config.embedding_model != expected.embedding_model
                or version_config.metric != expected.metric
                or binding is None
                or binding.workspace_id != expected.workspace_id
                or binding.kind != "vector"
                or binding.id != expected.vector_binding.id
                or binding.driver != expected.vector_binding.driver
                or dict(binding.config or {}) != expected.vector_binding.config
            ):
                raise IngestionOwnershipError()

            async def before_external() -> float:
                await context.heartbeat()
                return await self._owned_lease_budget(session, context, timeout_s)

            async def commit(actual_token_count: int) -> None:
                await context.heartbeat()
                await self._owned_lease_budget(session, context, timeout_s)
                if actual_token_count < 0:
                    raise ValidationFailed("The measured chunk token count is invalid.")
                chunk.token_count = actual_token_count
                chunk.reembed_applied_generation = expected.edit_generation
                await session.flush()
                document.token_count = await self._repo.sum_document_tokens(
                    session,
                    expected.kb_id,
                    expected.document_id,
                    expected.index_version,
                )

            await self._owned_lease_budget(session, context, timeout_s)
            yield ChunkReembedMutationGuard(before_external, commit)

    async def commit_chunked(
        self,
        context: TaskContext,
        *,
        chunks_object_key: str,
        chunks: list[ChunkSpec],
        stale_point_ids: tuple[UUID, ...],
    ) -> bool:
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            locked = await self._lock_current(session, context)
            if locked is None:
                return False
            document, run, kb = locked
            if _stage_at_least(run.state, "chunked"):
                return False
            existing = await self._repo.ingestion_chunks(
                session, run.kb_id, document.id, run.index_version
            )
            if (
                run.recovery_generation > 0
                and run.chunks_object_key is None
                and existing
                and await self._repo.edited_chunks(session, kb.id, document.id)
            ):
                raise ValidationFailed(
                    "Rechunking cannot replace manual edits created during recovery."
                )
            actual_stale = {
                row.id for row in existing if row.chunk_metadata.get("embed", True) is not False
            } - {chunk.id for chunk in chunks if chunk.metadata.get("embed", True) is not False}
            if actual_stale != set(stale_point_ids):
                raise IngestionOwnershipError("Chunk state changed while this task was running.")

            preserved = {}
            if kb.active_index_version is not None:
                preserved = {
                    row.ordinal: row
                    for row in await self._repo.edited_chunks(
                        session,
                        run.kb_id,
                        document.id,
                        kb.active_index_version,
                    )
                }
            rows: list[dict[str, object]] = []
            for chunk in chunks:
                edit = preserved.get(chunk.ordinal)
                if edit is not None and (
                    chunk.content != edit.content or chunk.content_hash != edit.content_hash
                ):
                    raise IngestionOwnershipError(
                        "A preserved chunk edit changed while this task was running."
                    )
                rows.append(
                    {
                        "kb_id": run.kb_id,
                        "id": chunk.id,
                        "workspace_id": document.workspace_id,
                        "document_id": document.id,
                        "index_version": run.index_version,
                        "parent_id": chunk.parent_id,
                        "ordinal": chunk.ordinal,
                        "content": chunk.content,
                        "content_hash": chunk.content_hash,
                        "token_count": chunk.token_count,
                        "chunk_metadata": chunk.metadata,
                        "is_edited": edit is not None,
                    }
                )
            await self._repo.replace_chunks(
                session, run.kb_id, document.id, run.index_version, rows
            )
            run.state = "chunked"
            run.chunks_object_key = chunks_object_key
            run.stale_point_ids = [str(point_id) for point_id in stale_point_ids]
            run.point_count = sum(
                chunk.metadata.get("embed", True) is not False for chunk in chunks
            )
            document.state = "chunked"
            document.chunk_count = len(chunks)
            document.token_count = sum(chunk.token_count for chunk in chunks)
            document.progress_pct = 50
            if kb.building_index_version == run.index_version:
                total = await self._repo.ingestion_point_total(
                    session, run.kb_id, run.index_version, indexed_only=False
                )
                await self._repo.set_index_progress(
                    session, run.kb_id, run.index_version, done=0, total=total
                )
            await self._tasks.enqueue(
                session,
                _next_task(context, queue="embed", kind="document.embed"),
            )
        return True

    async def commit_embedded(self, context: TaskContext, *, embeddings_object_key: str) -> bool:
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            locked = await self._lock_current(session, context)
            if locked is None:
                return False
            document, run, _kb = locked
            if _stage_at_least(run.state, "embedded"):
                return False
            if run.state != "chunked":
                raise IngestionOwnershipError("Chunking has not committed for this revision.")
            run.state = "embedded"
            run.embeddings_object_key = embeddings_object_key
            document.state = "embedded"
            document.progress_pct = 75
            await self._tasks.enqueue(
                session,
                _next_task(context, queue="index", kind="document.index"),
            )
        return True

    @asynccontextmanager
    async def index_mutation(
        self, context: TaskContext, *, timeout_s: float = 30.0
    ) -> AsyncIterator[IndexMutationGuard]:
        await context.heartbeat()
        async with asyncio.timeout(timeout_s), transaction() as session:
            locked = await self._lock_current(session, context)
            if locked is None or locked[1].state != "embedded":
                raise IngestionOwnershipError()
            version = await self._repo.get_index_version(
                session, locked[1].kb_id, locked[1].index_version, for_update=True
            )
            if version is None or version.state not in {"active", "building"}:
                raise IngestionOwnershipError()

            async def before_external() -> float:
                await context.heartbeat()
                return await self._owned_lease_budget(session, context, timeout_s)

            async def commit(count: int) -> IndexCommit:
                await context.heartbeat()
                await self._owned_lease_budget(session, context, timeout_s)
                return await self._commit_indexed(session, context, count)

            await self._owned_lease_budget(session, context, timeout_s)
            yield IndexMutationGuard(before_external, commit)

    async def commit_indexed(
        self, context: TaskContext, *, verified_point_count: int
    ) -> IndexCommit:
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            return await self._commit_indexed(session, context, verified_point_count)

    async def is_deleted_activation_publication_retry(self, context: TaskContext) -> bool:
        identity = _deleted_identity(context)
        if identity is None:
            return False
        document_id, index_version = identity
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            document = await self._repo.get_document(session, document_id)
            kb = await self._repo.get_kb(session, context.kb_id) if context.kb_id else None
            version = (
                await self._repo.get_index_version(session, context.kb_id, index_version)
                if context.kb_id
                else None
            )
            return bool(
                document is not None
                and document.deleted_at is not None
                and document.workspace_id == context.workspace_id
                and document.kb_id == context.kb_id
                and kb is not None
                and kb.workspace_id == context.workspace_id
                and kb.active_index_version == index_version
                and version is not None
                and version.state == "active"
                and version.enrollment_state == "complete"
            )

    @asynccontextmanager
    async def deleted_index_mutation(
        self, context: TaskContext, *, timeout_s: float = 30.0
    ) -> AsyncIterator[DeletedIndexMutationGuard]:
        identity = _deleted_identity(context)
        if identity is None or context.kb_id is None:
            raise IngestionOwnershipError()
        document_id, index_version = identity
        await context.heartbeat()
        async with asyncio.timeout(timeout_s), transaction() as session:
            document = await self._repo.get_document(session, document_id, for_update=True)
            if (
                document is None
                or document.deleted_at is None
                or document.state != "deleting"
                or document.workspace_id != context.workspace_id
                or document.kb_id != context.kb_id
            ):
                raise IngestionOwnershipError()
            kb = await self._repo.get_kb(session, context.kb_id, for_update=True)
            if kb is None or kb.building_index_version != index_version:
                raise IngestionOwnershipError()
            version = await self._repo.get_index_version(
                session, context.kb_id, index_version, for_update=True
            )
            binding = await self._repo.get_binding(session, kb.vector_binding_id)
            if (
                version is None
                or version.state != "building"
                or binding is None
                or binding.workspace_id != context.workspace_id
                or binding.kind != "vector"
            ):
                raise IngestionOwnershipError()
            version_config = _load_version_config(version)
            embedding_dim = version_config.embedding_model.dimension
            if embedding_dim is None:
                raise IngestionOwnershipError()
            target = DeletedBuildTarget(
                document_id=document_id,
                workspace_id=context.workspace_id,
                kb_id=context.kb_id,
                index_version=index_version,
                vector_binding=_binding_ref(binding),
                embedding_dim=embedding_dim,
                metric=version_config.metric,
                sparse_modifier=version_config.sparse.modifier,
            )

            async def before_external() -> float:
                return await self._owned_lease_budget(session, context, timeout_s)

            async def commit() -> IndexCommit:
                await self._owned_lease_budget(session, context, timeout_s)
                await self._repo.clear_document_version_chunks(
                    session, target.kb_id, document_id, index_version
                )
                activated = await activate_build_if_ready(
                    session,
                    kb=kb,
                    version=version,
                    repository=self._repo,
                    tasks=self._tasks,
                )
                return IndexCommit(advanced=True, activated=activated)

            await self._owned_lease_budget(session, context, timeout_s)
            yield DeletedIndexMutationGuard(target, before_external, commit)

    async def _commit_indexed(
        self, session: AsyncSession, context: TaskContext, verified_point_count: int
    ) -> IndexCommit:
        activated = False
        locked = await self._lock_current(session, context)
        if locked is None:
            return IndexCommit(advanced=False, activated=False)
        document, run, kb = locked
        if _stage_at_least(run.state, "indexed"):
            return IndexCommit(
                advanced=False,
                activated=kb.active_index_version == run.index_version,
            )
        if run.state != "embedded" or verified_point_count != run.point_count:
            raise IngestionOwnershipError("The verified index does not match this revision.")
        run.state = "indexed"
        document.state = "indexed"
        document.progress_pct = 100
        document.indexed_at = utcnow()
        if kb.building_index_version == run.index_version:
            await session.flush()
            done = await self._repo.ingestion_point_total(
                session, run.kb_id, run.index_version, indexed_only=True
            )
            total = await self._repo.ingestion_point_total(
                session, run.kb_id, run.index_version, indexed_only=False
            )
            await self._repo.set_index_progress(
                session, run.kb_id, run.index_version, done=done, total=total
            )
            version = await self._repo.get_index_version(
                session, run.kb_id, run.index_version, for_update=True
            )
            if version is not None:
                activated = await activate_build_if_ready(
                    session,
                    kb=kb,
                    version=version,
                    repository=self._repo,
                    tasks=self._tasks,
                )
        return IndexCommit(advanced=True, activated=activated)

    async def record_failure(
        self,
        context: TaskContext,
        *,
        stage: str,
        error_code: str,
        detail: str,
        terminal: bool,
    ) -> None:
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            locked = await self._lock_current(session, context)
            if locked is None:
                return
            document, run, _kb = locked
            if run.state == "failed":
                return
            document.stage_detail = stage
            document.error_code = error_code
            document.error_detail = detail
            if terminal:
                run.failed_stage = stage
                run.previous_committed_state = run.state
                document.state = "failed"
                run.state = "failed"

    async def publish_runtime(self, kb_id: UUID) -> None:
        from cairn.catalog.service import get_catalog_service

        await get_catalog_service().publish_runtime(kb_id)

    async def confirm_published(self, context: TaskContext) -> None:
        async with transaction() as session:
            await self._tasks.lock_owned_task(session, context)
            locked = await self._lock_current(session, context)
            if locked is None:
                return
            document, run, kb = locked
            if run.state == "indexed" and kb.active_index_version == run.index_version:
                document.stage_detail = None
                document.error_code = None
                document.error_detail = None

    async def _lock_current(
        self,
        session: AsyncSession,
        context: TaskContext,
    ) -> tuple[Document, DocumentIngestion, KnowledgeBase] | None:
        identity = _identity(context)
        if identity is None:
            return None
        document_id, revision, index_version, recovery_generation = identity
        document = await self._repo.get_document(session, document_id, for_update=True)
        if document is None or document.deleted_at is not None:
            return None
        run = await self._repo.get_document_ingestion(
            session, document_id, revision, index_version, for_update=True
        )
        if run is None:
            return None
        kb = await self._repo.get_kb(session, run.kb_id, for_update=True)
        if kb is None:
            return None
        if (
            context.workspace_id != document.workspace_id
            or context.kb_id != document.kb_id
            or run.kb_id != document.kb_id
            or document.revision != revision
            or document.content_hash != run.source_content_hash
            or run.recovery_generation != recovery_generation
            or kb.status in {"deleting", "archived"}
            or index_version not in {kb.active_index_version, kb.building_index_version}
        ):
            return None
        return document, run, kb

    async def _owned_lease_budget(
        self, session: AsyncSession, context: TaskContext, timeout_s: float
    ) -> float:
        remaining = await session.scalar(
            text(
                "SELECT extract(epoch FROM lease_until - clock_timestamp()) FROM task "
                "WHERE id=:task_id AND state='running' AND worker_id=:worker_id "
                "AND attempt=:attempt AND lease_until > clock_timestamp()"
            ),
            {
                "task_id": context.task_id,
                "worker_id": context.worker_id,
                "attempt": context.attempt,
            },
        )
        if remaining is None or float(remaining) <= 0.1:
            raise TaskLeaseLostError(context.task_id)
        return min(timeout_s, float(remaining) - 0.05)


_STAGE_ORDER = {
    "registered": 0,
    "parsed": 1,
    "chunked": 2,
    "embedded": 3,
    "indexed": 4,
    "failed": -1,
}


def _stage_at_least(current: str, expected: str) -> bool:
    return _STAGE_ORDER.get(current, -1) >= _STAGE_ORDER[expected]


def source_key_in_scope(object_key: str, *, workspace_id: UUID, kb_id: UUID) -> bool:
    parts = object_key.split("/")
    return (
        len(parts) >= 3
        and not any(char in object_key for char in ("\\", ":", "\x00"))
        and parts[0] == str(workspace_id)
        and parts[1] == str(kb_id)
        and all(part not in {"", ".", ".."} and part == part.rstrip(" .") for part in parts)
    )


def _compact_uuid(value: UUID) -> str:
    return urlsafe_b64encode(value.bytes).rstrip(b"=").decode("ascii")


def _identity(context: TaskContext) -> tuple[UUID, int, int, int] | None:
    if context.document_id is None or context.kb_id is None:
        return None
    revision = context.payload.get("revision")
    index_version = context.payload.get("index_version")
    recovery_generation = context.payload.get("recovery_generation", 0)
    if (
        type(revision) is not int
        or type(index_version) is not int
        or type(recovery_generation) is not int
    ):
        return None
    if revision < 1 or index_version < 1 or recovery_generation < 0:
        return None
    return context.document_id, revision, index_version, recovery_generation


def _recovery_generation(context: TaskContext) -> int:
    generation = context.payload.get("recovery_generation", 0)
    return generation if type(generation) is int and generation >= 0 else -1


def _deleted_identity(context: TaskContext) -> tuple[UUID, int] | None:
    if context.document_id is None or context.kb_id is None:
        return None
    index_version = context.payload.get("index_version")
    if type(index_version) is not int or index_version < 1:
        return None
    return context.document_id, index_version


def _next_task(context: TaskContext, *, queue: str, kind: str) -> TaskSpec:
    assert context.document_id is not None and context.kb_id is not None
    revision = int(context.payload["revision"])
    index_version = int(context.payload["index_version"])
    payload = {"revision": revision, "index_version": index_version}
    recovery_generation = _recovery_generation(context)
    if recovery_generation > 0:
        payload["recovery_generation"] = recovery_generation
    generation_suffix = f":{recovery_generation}" if recovery_generation > 0 else ""
    return TaskSpec(
        queue=queue,  # type: ignore[arg-type]
        kind=kind,
        workspace_id=context.workspace_id,
        kb_id=context.kb_id,
        document_id=context.document_id,
        correlation_id=context.correlation_id,
        payload=payload,
        dedupe_key=(f"{kind}:{context.document_id}:{revision}:{index_version}{generation_suffix}"),
    )


def _binding_ref(binding: StorageBinding) -> BindingRef:
    return BindingRef(
        id=binding.id,
        kind=cast("Literal['vector', 'object']", binding.kind),
        driver=binding.driver,
        name=binding.name,
        config=dict(binding.config or {}),
        health_state=binding.health_state,
    )


def _chunk_reembed_target(
    document: Document,
    chunk: Chunk,
    version_config: IndexVersionConfig,
    vector_binding: BindingRef,
) -> ChunkReembedTarget:
    return ChunkReembedTarget(
        workspace_id=document.workspace_id,
        kb_id=document.kb_id,
        document_id=document.id,
        chunk_id=chunk.id,
        revision=document.revision,
        index_version=chunk.index_version,
        edit_generation=chunk.edit_generation,
        reembed_applied_generation=chunk.reembed_applied_generation,
        content=chunk.content,
        content_hash=chunk.content_hash,
        parent_id=chunk.parent_id,
        metadata=chunk.chunk_metadata,
        embedding_model=version_config.embedding_model,
        metric=version_config.metric,
        vector_binding=vector_binding,
        sparse=version_config.sparse,
    )


def _run_view(
    document: Document,
    run: DocumentIngestion,
    kb: KnowledgeBase,
    version_config: IndexVersionConfig,
    object_binding: BindingRef,
    vector_binding: BindingRef,
    previous_key: str | None,
) -> IngestionRun:
    if document.object_key is None or document.mime_type is None:
        raise IngestionOwnershipError()
    return IngestionRun(
        document_id=document.id,
        workspace_id=document.workspace_id,
        kb_id=document.kb_id,
        revision=run.revision,
        index_version=run.index_version,
        recovery_generation=run.recovery_generation,
        state=run.state,  # type: ignore[arg-type]
        source_content_hash=run.source_content_hash,
        object_key=document.object_key,
        mime_type=document.mime_type,
        source_url=document.source_ref if document.source_type == "crawl" else None,
        version_config=version_config,
        object_binding=object_binding,
        vector_binding=vector_binding,
        prior_parsed_object_key=run.prior_parsed_object_key,
        parsed_object_key=run.parsed_object_key,
        chunks_object_key=run.chunks_object_key,
        embeddings_object_key=run.embeddings_object_key,
        prior_embeddings_object_key=previous_key,
        point_count=run.point_count,
        stale_point_ids=tuple(UUID(value) for value in run.stale_point_ids),
        active_index_version=kb.active_index_version,
        building_index_version=kb.building_index_version,
    )


def _load_version_config(row: KbIndexVersion | None) -> IndexVersionConfig:
    if row is None or row.snapshot_unavailable or row.config_snapshot is None:
        raise ValidationFailed(
            "The ingestion target has no proven version snapshot; a fresh rebuild is required."
        )
    try:
        return IndexVersionConfig.model_validate(row.config_snapshot)
    except ValueError as exc:
        raise ValidationFailed(
            "The ingestion target version snapshot is invalid; a fresh rebuild is required."
        ) from exc


def _chunk_reembed_identity(
    context: TaskContext,
) -> tuple[UUID, int, int, str, int] | None:
    if context.kind != "chunk.reembed":
        return None
    payload = context.payload
    raw_chunk_id = payload.get("chunk_id")
    raw_revision = payload.get("revision")
    raw_index_version = payload.get("index_version")
    raw_content_hash = payload.get("content_hash")
    raw_edit_generation = payload.get("edit_generation")
    if (
        not isinstance(raw_chunk_id, str)
        or type(raw_revision) is not int
        or type(raw_index_version) is not int
        or not isinstance(raw_content_hash, str)
        or type(raw_edit_generation) is not int
    ):
        return None
    try:
        chunk_id = UUID(raw_chunk_id)
    except (ValueError, AttributeError):
        return None
    revision = raw_revision
    index_version = raw_index_version
    content_hash = raw_content_hash
    edit_generation = raw_edit_generation
    if (
        revision <= 0
        or index_version <= 0
        or edit_generation <= 0
        or not isinstance(content_hash, str)
        or len(content_hash) != 64
        or any(char not in "0123456789abcdef" for char in content_hash)
    ):
        return None
    return chunk_id, revision, index_version, content_hash, edit_generation
