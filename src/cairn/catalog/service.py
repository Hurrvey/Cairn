"""Catalog service — knowledge bases, documents, chunks, index versions.

Three behaviours in here carry most of the module's weight:

* **Embedding immutability** (ADR-0006). Enforced at the API, here, and by a
  database trigger. Three layers because the failure is silent.
* **Blue/green index versioning** (ADR-0007). A rebuild writes a new namespace
  while the active one keeps serving; the switch is one atomic UPDATE.
* **Runtime publication** (ADR-0002). The data plane sees a
  ``KnowledgeBaseRuntime`` DTO from Redis, never an ORM object.
"""

from __future__ import annotations

import re
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from cairn.authz.model import Principal
from cairn.catalog.config import ChunkConfig, RetrievalConfig
from cairn.catalog.dto import (
    BindingRef,
    BindingRefModel,
    ChunkSpec,
    ChunkView,
    CreateKbSpec,
    DocumentRegistration,
    DocumentView,
    IndexProgressView,
    IndexVersionView,
    KnowledgeBaseRuntime,
    KnowledgeBaseView,
    ReindexEstimate,
    ReindexSpec,
    UpdateKbSpec,
    UploadSpec,
)
from cairn.catalog.errors import (
    BindingInUse,
    ChunkNotEditable,
    EmbeddingModelImmutable,
    KbIndexInProgress,
    KbNotReady,
)
from cairn.catalog.ingestion import source_key_in_scope
from cairn.catalog.models import (
    Chunk,
    Document,
    DocumentIngestion,
    KbIndexVersion,
    KnowledgeBase,
    StorageBinding,
)
from cairn.catalog.repository import CatalogRepository
from cairn.core.cache import Cache, get_cache
from cairn.core.db import session_scope, transaction
from cairn.core.errors import Conflict, NotFound, ValidationFailed
from cairn.core.logging import get_logger
from cairn.core.time import utcnow
from cairn.modelgw.catalog import ModelCatalog, get_model_catalog
from cairn.platform.audit import AuditService, get_audit_service
from cairn.tasks.dto import TaskSpec
from cairn.tasks.service import TaskService, get_task_service
from cairn.vectorstore.base import Namespace, NamespaceSpec

__all__ = ["RUNTIME_CACHE_TTL", "CatalogService", "get_catalog_service"]

log = get_logger(__name__)

#: Bounds how long a config change takes to reach the data plane. Deletion and
#: index switches additionally publish an explicit invalidation, because those
#: two must be immediate.
RUNTIME_CACHE_TTL = 300

#: Fields that cannot change once a namespace exists (ADR-0006).
IMMUTABLE_ONCE_INDEXED = frozenset({"embedding_model_id", "metric"})

#: Changing these alters chunk boundaries, so they take effect only on rebuild.
REQUIRES_REINDEX = frozenset({"chunk_config"})

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_INDEX_RETENTION = timedelta(hours=24)


def slugify(name: str) -> str:
    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")
    return slug[:200] or "knowledge-base"


class CatalogService:
    def __init__(
        self,
        repository: CatalogRepository | None = None,
        *,
        models: ModelCatalog | None = None,
        tasks: TaskService | None = None,
        audit: AuditService | None = None,
        cache: Cache | None = None,
    ) -> None:
        self._repo = repository or CatalogRepository()
        self._models = models or get_model_catalog()
        self._tasks = tasks or get_task_service()
        self._audit = audit or get_audit_service()
        self._cache = cache

    @property
    def cache(self) -> Cache:
        return self._cache or get_cache()

    # ------------------------------------------------------------ knowledge base

    async def create_kb(self, actor: Principal, spec: CreateKbSpec) -> KnowledgeBaseView:
        # Validate the model *before* anything is written: a KB configured
        # against a chat model or a dimensionless embedding model would fail at
        # its first upsert, long after documents were uploaded.
        model = await self._models.require_embedding_model(
            actor.workspace_id, spec.embedding_model_id
        )
        assert model.dimension is not None

        chunk_config = spec.chunk_config or ChunkConfig()
        retrieval_config = spec.retrieval_config or RetrievalConfig()
        slug = spec.slug or slugify(spec.name)

        async with transaction() as session:
            vector = await self._require_binding(session, actor, spec.vector_binding_id, "vector")
            objects = await self._require_binding(session, actor, spec.object_binding_id, "object")

            if await self._repo.slug_taken(session, actor.workspace_id, slug):
                raise Conflict(f"A knowledge base with the slug {slug!r} already exists.")

            kb = await self._repo.add_kb(
                session,
                KnowledgeBase(
                    workspace_id=actor.workspace_id,
                    name=spec.name,
                    slug=slug,
                    description=spec.description,
                    kb_metadata=dict(spec.metadata),
                    embedding_model_id=model.id,
                    # Copied from the model, never taken from the request: a
                    # client-supplied dimension that disagreed with the model
                    # would corrupt every vector written.
                    embedding_dim=model.dimension,
                    metric=spec.metric,
                    vector_binding_id=vector.id,
                    object_binding_id=objects.id,
                    chunk_config=chunk_config.model_dump(mode="json"),
                    retrieval_config=retrieval_config.model_dump(mode="json", by_alias=True),
                    owner_user_id=actor.id,
                    active_index_version=None,
                ),
            )

            # Without this the creator cannot use the knowledge base they just
            # made, which is a confusing first five minutes.
            from cairn.authz.service import GrantSpec, get_authz_service

            await get_authz_service().grant(
                actor,
                GrantSpec(
                    subject_type="user",
                    subject_id=actor.id,
                    resource_type="knowledge_base",
                    resource_id=kb.id,
                    permissions=["kb:read", "kb:query", "kb:write", "kb:manage"],
                ),
            )

            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="kb.create",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="knowledge_base",
                resource_id=kb.id,
                after=kb.snapshot(),
            )
            view = _kb_view(kb)

        await self.publish_runtime(kb.id)
        log.info("catalog.kb_created", kb_id=str(kb.id), embedding_dim=model.dimension)
        return view

    async def get_kb(self, kb_id: UUID) -> KnowledgeBaseView:
        async with session_scope() as session:
            kb = await self._repo.get_kb(session, kb_id)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            return _kb_view(kb)

    async def list_kbs(
        self,
        actor: Principal,
        *,
        limit: int = 50,
        cursor_id: UUID | None = None,
    ) -> list[KnowledgeBaseView]:
        # Administrators see the workspace; everyone else sees what they hold a
        # grant on. Filtering here rather than after the fact means a large
        # workspace does not fetch rows the caller may not see.
        kb_ids = None if actor.is_admin else sorted(actor.accessible_kb_ids)
        async with session_scope() as session:
            rows = await self._repo.list_kbs(
                session, actor.workspace_id, kb_ids=kb_ids, limit=limit, cursor_id=cursor_id
            )
        return [_kb_view(kb) for kb in rows]

    async def update_kb(
        self, actor: Principal, kb_id: UUID, spec: UpdateKbSpec
    ) -> KnowledgeBaseView:
        changed = spec.changed_fields()

        async with transaction() as session:
            kb = await self._repo.get_kb(session, kb_id, for_update=True)
            if kb is None:
                raise NotFound("Knowledge base not found.")

            # ADR-0006, layer 2 of 3. The API rejects it first for a better
            # message; the database trigger is the backstop.
            if kb.active_index_version is not None and (changed & IMMUTABLE_ONCE_INDEXED):
                raise EmbeddingModelImmutable(
                    "The embedding model and metric cannot be changed once a knowledge "
                    "base has been indexed, because mixing embedding spaces in one index "
                    "silently degrades retrieval. Use POST "
                    f"/v1/knowledge-bases/{kb_id}/reindex to rebuild with a different model."
                )
            if spec.embedding_model_id is not None or spec.metric is not None:
                # Not yet indexed: allowed, but still validated.
                if spec.embedding_model_id is not None:
                    model = await self._models.require_embedding_model(
                        actor.workspace_id, spec.embedding_model_id
                    )
                    assert model.dimension is not None
                    kb.embedding_model_id = model.id
                    kb.embedding_dim = model.dimension
                if spec.metric is not None:
                    kb.metric = spec.metric

            before = kb.snapshot()

            if spec.name is not None:
                kb.name = spec.name
            if spec.description is not None:
                kb.description = spec.description
            if spec.metadata is not None:
                kb.kb_metadata = dict(spec.metadata)
            if spec.chunk_config is not None:
                kb.chunk_config = spec.chunk_config.model_dump(mode="json")
            if spec.retrieval_config is not None:
                kb.retrieval_config = spec.retrieval_config.model_dump(mode="json", by_alias=True)

            kb.config_version += 1

            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="kb.update",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="knowledge_base",
                resource_id=kb.id,
                before=before,
                after=kb.snapshot(),
                detail={"changed": sorted(changed)},
            )
            view = _kb_view(kb)

        await self.publish_runtime(kb_id)

        # Surfaced so the UI can prompt for a rebuild rather than leaving the
        # user to wonder why their new chunk size changed nothing.
        requires_reindex = (
            bool(changed & REQUIRES_REINDEX) and view.active_index_version is not None
        )
        return _with_reindex_flag(view, requires_reindex)

    async def delete_kb(self, actor: Principal, kb_id: UUID) -> None:
        """Mark for deletion and enqueue the purge (FR-C-08).

        Retrieval stops immediately — the status change plus cache invalidation
        happen before any storage is touched, so no query can hit a half-purged
        index.
        """
        async with transaction() as session:
            kb = await self._repo.get_kb(session, kb_id, for_update=True)
            if kb is None:
                raise NotFound("Knowledge base not found.")

            before = kb.snapshot()
            kb.status = "deleting"
            kb.deleted_at = utcnow()
            kb.config_version += 1

            await self._tasks.enqueue(
                session,
                TaskSpec(
                    queue="maintain",
                    kind="kb.purge",
                    workspace_id=actor.workspace_id,
                    kb_id=kb_id,
                    payload={"kb_id": str(kb_id)},
                    priority=50,
                    dedupe_key=f"kb.purge:{kb_id}",
                ),
            )
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="kb.delete",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="knowledge_base",
                resource_id=kb_id,
                before=before,
            )

        await self.invalidate_runtime(kb_id)
        log.info("catalog.kb_deleting", kb_id=str(kb_id))

    # --------------------------------------------------------- runtime publication

    async def publish_runtime(self, kb_id: UUID) -> KnowledgeBaseRuntime | None:
        """Publish the data plane's view of a knowledge base (ADR-0002).

        This is the seam that keeps the plane split honest. M09 reads this DTO
        from Redis; it never sees a ``KnowledgeBase``, so it never acquires the
        control plane's ORM, migrations, or startup cost.
        """
        async with session_scope() as session:
            kb = await self._repo.get_kb(session, kb_id)
            if kb is None or kb.active_index_version is None:
                # Nothing indexed yet: there is nothing the data plane can serve.
                await self.invalidate_runtime(kb_id)
                return None
            binding = await self._repo.get_binding(session, kb.vector_binding_id)
            if binding is None:  # pragma: no cover — FK guarantees this
                raise NotFound("Vector binding not found.")
            snapshot = (kb, binding)

        kb, binding = snapshot
        runtime = KnowledgeBaseRuntime(
            id=kb.id,
            workspace_id=kb.workspace_id,
            # ACTIVE, never "latest" — this is what stops a query seeing a
            # half-built index during a rebuild.
            index_version=kb.active_index_version,
            embedding_model=await self._models.get_ref(kb.embedding_model_id),
            metric=kb.metric,
            vector_binding=BindingRefModel(
                id=binding.id, driver=binding.driver, config=dict(binding.config)
            ),
            retrieval_config=RetrievalConfig.model_validate(kb.retrieval_config),
            config_version=kb.config_version,
            status=kb.status,
        )

        await self.cache.set(
            _runtime_key(kb_id), runtime.model_dump_json().encode(), RUNTIME_CACHE_TTL
        )
        log.info(
            "catalog.runtime_published",
            kb_id=str(kb_id),
            index_version=runtime.index_version,
            config_version=runtime.config_version,
        )
        return runtime

    async def invalidate_runtime(self, kb_id: UUID) -> None:
        """Immediate removal, for deletion and index switches.

        The TTL is fine for ordinary config drift, but a deleted knowledge base
        must stop answering now.
        """
        await self.cache.delete(_runtime_key(kb_id))

    # ------------------------------------------------------------- index versions

    async def start_reindex(
        self, actor: Principal, kb_id: UUID, spec: ReindexSpec
    ) -> IndexVersionView | ReindexEstimate:
        """Begin a blue/green rebuild (ADR-0007).

        Without ``confirm`` this returns an estimate and starts nothing. A UI
        click that silently spends hours of GPU time and provider budget is a
        footgun, so the cost is shown first.
        """
        async with session_scope() as session:
            kb = await self._repo.get_kb(session, kb_id)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            if kb.building_index_version is not None:
                raise KbIndexInProgress(
                    f"Index version {kb.building_index_version} is already building."
                )
            if kb.status == "deleting":
                raise KbNotReady("This knowledge base is being deleted.")
            estimate = await self._estimate_reindex(session, kb)

        if not spec.confirm:
            return estimate

        async with transaction() as session:
            kb = await self._repo.get_kb(session, kb_id, for_update=True)
            if kb is None:  # pragma: no cover — just read above
                raise NotFound("Knowledge base not found.")
            if kb.building_index_version is not None:
                raise KbIndexInProgress(
                    f"Index version {kb.building_index_version} is already building."
                )

            before = kb.snapshot()

            # A reindex is the ONLY way to change the embedding model.
            if spec.embedding_model_id is not None:
                model = await self._models.require_embedding_model(
                    actor.workspace_id, spec.embedding_model_id
                )
                assert model.dimension is not None
                kb.embedding_model_id = model.id
                kb.embedding_dim = model.dimension
            if spec.chunk_config is not None:
                kb.chunk_config = spec.chunk_config.model_dump(mode="json")

            new_version = (kb.active_index_version or 0) + 1
            kb.building_index_version = new_version
            kb.status = "indexing"
            kb.config_version += 1

            row = await self._repo.add_index_version(
                session,
                KbIndexVersion(
                    kb_id=kb_id,
                    version=new_version,
                    workspace_id=kb.workspace_id,
                    state="building",
                    physical_ref=Namespace(kb_id, new_version).key(),
                    chunk_total=estimate.chunks,
                ),
            )

            # Transactional enqueue: the fan-out task cannot be lost between the
            # version allocation and the work that fills it.
            await self._tasks.enqueue(
                session,
                TaskSpec(
                    queue="maintain",
                    kind="kb.reindex_fanout",
                    workspace_id=kb.workspace_id,
                    kb_id=kb_id,
                    payload={"index_version": new_version},
                    priority=50,
                    dedupe_key=f"kb.reindex:{kb_id}:{new_version}",
                ),
            )
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="kb.reindex",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="knowledge_base",
                resource_id=kb_id,
                before=before,
                after=kb.snapshot(),
                detail={
                    "index_version": new_version,
                    "reason": spec.reason,
                    "estimated_chunks": estimate.chunks,
                },
            )
            view = _index_version_view(row)

        log.info("catalog.reindex_started", kb_id=str(kb_id), index_version=view.version)
        return view

    async def activate_index_version(self, kb_id: UUID, version: int) -> None:
        """The atomic switch (ADR-0007).

        One transaction, so a request either reads the old index or the new one
        and never a mixture.
        """
        async with transaction() as session:
            kb = await self._repo.get_kb(session, kb_id, for_update=True)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            if kb.building_index_version != version:
                raise Conflict(
                    f"Index version {version} is not the version being built "
                    f"({kb.building_index_version})."
                )

            retired = kb.active_index_version
            kb.active_index_version = version
            kb.building_index_version = None
            kb.status = "active"
            kb.config_version += 1
            kb.last_indexed_at = utcnow()
            kb.chunk_count = await self._repo.count_chunks(session, kb_id, version)

            await self._repo.set_index_state(session, kb_id, version, "active")

            if retired is not None:
                # Kept briefly so a bad rebuild can be reverted with one UPDATE.
                await self._repo.set_index_state(
                    session,
                    kb_id,
                    retired,
                    "retired",
                    retire_after=utcnow() + _INDEX_RETENTION,
                )
                await self._tasks.enqueue(
                    session,
                    TaskSpec(
                        queue="maintain",
                        kind="index.drop",
                        workspace_id=kb.workspace_id,
                        kb_id=kb_id,
                        payload={"index_version": retired},
                        priority=10,
                        run_after=utcnow() + _INDEX_RETENTION,
                        dedupe_key=f"index.drop:{kb_id}:{retired}",
                    ),
                )

        await self.publish_runtime(kb_id)
        log.info("catalog.index_activated", kb_id=str(kb_id), version=version, retired=retired)

    async def fail_index_version(self, kb_id: UUID, version: int, error: str) -> None:
        """Abandon a build. The active version is never touched, so a failed
        rebuild is a non-event for anyone querying."""
        async with transaction() as session:
            kb = await self._repo.get_kb(session, kb_id, for_update=True)
            if kb is None:  # pragma: no cover
                return
            kb.building_index_version = None
            kb.status = "active" if kb.active_index_version is not None else "error"
            await self._repo.set_index_state(session, kb_id, version, "failed", error=error[:2000])
            await self._tasks.enqueue(
                session,
                TaskSpec(
                    queue="maintain",
                    kind="index.drop",
                    workspace_id=kb.workspace_id,
                    kb_id=kb_id,
                    payload={"index_version": version},
                    priority=10,
                    dedupe_key=f"index.drop:{kb_id}:{version}",
                ),
            )
        log.warning("catalog.index_failed", kb_id=str(kb_id), version=version, error=error[:200])

    async def list_index_versions(self, kb_id: UUID) -> list[IndexVersionView]:
        """Newest first. The history is what makes a failed rebuild diagnosable
        after the fact — the failed row keeps its error rather than vanishing."""
        async with session_scope() as session:
            rows = await self._repo.list_index_versions(session, kb_id)
        return [_index_version_view(row) for row in rows]

    async def get_index_progress(self, kb_id: UUID) -> IndexProgressView:
        async with session_scope() as session:
            kb = await self._repo.get_kb(session, kb_id)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            version = kb.building_index_version or kb.active_index_version
            row = (
                await self._repo.get_index_version(session, kb_id, version)
                if version is not None
                else None
            )

        if row is None:
            return IndexProgressView(
                active_index_version=kb.active_index_version,
                building_index_version=kb.building_index_version,
                state="none",
                chunk_total=0,
                chunk_done=0,
                percent=0.0,
                eta_seconds=None,
                started_at=None,
            )

        percent = (row.chunk_done / row.chunk_total * 100) if row.chunk_total else 0.0
        eta = None
        if row.state == "building" and row.chunk_done > 0 and row.chunk_total > row.chunk_done:
            elapsed = (utcnow() - row.started_at).total_seconds()
            rate = row.chunk_done / max(elapsed, 1e-6)
            eta = int((row.chunk_total - row.chunk_done) / rate) if rate > 0 else None

        return IndexProgressView(
            active_index_version=kb.active_index_version,
            building_index_version=kb.building_index_version,
            state=row.state,
            chunk_total=row.chunk_total,
            chunk_done=row.chunk_done,
            percent=round(percent, 1),
            eta_seconds=eta,
            started_at=row.started_at,
            error=row.error,
        )

    async def _estimate_reindex(self, session: AsyncSession, kb: KnowledgeBase) -> ReindexEstimate:
        version = kb.active_index_version
        chunks = (
            await self._repo.count_chunks(session, kb.id, version) if version is not None else 0
        )
        tokens = await self._repo.sum_tokens(session, kb.id, version) if version is not None else 0

        # Deliberately coarse. The point is to convert "click here" into "this
        # costs about eight minutes and four dollars", not to be precise.
        throughput_chunks_per_minute = 2000 * 60 / 300
        minutes = max(1, int(chunks / max(throughput_chunks_per_minute, 1)))
        return ReindexEstimate(
            chunks=chunks,
            embedding_tokens=tokens,
            estimated_cost_usd=None,  # populated once M10 knows provider pricing
            estimated_minutes=minutes,
            peak_storage_bytes=int(kb.bytes_used * 2),
        )

    # ----------------------------------------------------------------- documents

    async def register_upload(
        self, actor: Principal, kb_id: UUID, spec: UploadSpec
    ) -> DocumentRegistration:
        """Register an uploaded document and enqueue parsing.

        The document row and its parse task commit together (NFR-R-03), so the
        "document exists but nothing will ever process it" state is unreachable.
        """
        async with transaction() as session:
            kb = await self._repo.get_kb(session, kb_id, for_update=True)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            if kb.status in ("deleting", "archived"):
                raise KbNotReady(f"This knowledge base is {kb.status}.")

            if not source_key_in_scope(spec.object_key, workspace_id=kb.workspace_id, kb_id=kb.id):
                raise ValidationFailed("The source object key is outside this document's scope.")
            existing = await self._repo.get_document_by_hash(session, kb_id, spec.content_hash)
            if existing is not None and existing.deleted_at is None:
                # Not an error. Re-uploading identical content is the expected
                # outcome of a retried sync, and reporting it as a failure
                # trains users to ignore failures.
                return DocumentRegistration(
                    filename=spec.filename,
                    status="skipped",
                    reason="DUPLICATE_CONTENT_HASH",
                    document=_document_view(existing),
                    existing_document_id=existing.id,
                )

            document = await self._repo.add_document(
                session,
                Document(
                    workspace_id=kb.workspace_id,
                    kb_id=kb_id,
                    source_type=spec.source_type,
                    source_ref=spec.source_ref or spec.filename,
                    title=spec.title or spec.filename,
                    mime_type=spec.mime_type,
                    size_bytes=spec.size_bytes,
                    content_hash=spec.content_hash,
                    object_key=spec.object_key,
                    doc_metadata=dict(spec.metadata),
                    state="registered",
                ),
            )

            if kb.building_index_version is not None:
                index_version = kb.building_index_version
            elif kb.active_index_version is not None:
                index_version = kb.active_index_version
            else:
                index_version = 1
                kb.building_index_version = index_version
                kb.status = "indexing"
                await self._repo.add_index_version(
                    session,
                    KbIndexVersion(
                        kb_id=kb.id,
                        version=index_version,
                        workspace_id=kb.workspace_id,
                        state="building",
                    ),
                )

            await self._repo.add_document_ingestion(
                session,
                DocumentIngestion(
                    document_id=document.id,
                    revision=document.revision,
                    kb_id=kb.id,
                    index_version=index_version,
                    source_content_hash=document.content_hash,
                ),
            )

            await self._tasks.enqueue(
                session,
                TaskSpec(
                    queue="parse",
                    kind="document.parse",
                    workspace_id=kb.workspace_id,
                    kb_id=kb_id,
                    document_id=document.id,
                    payload={"revision": document.revision, "index_version": index_version},
                    dedupe_key=f"parse:{document.id}:{document.revision}",
                ),
            )
            await self._repo.adjust_counters(
                session, kb_id, docs=1, bytes_used=spec.size_bytes or 0
            )
            view = _document_view(document)

        await self._tasks.notify("parse")
        return DocumentRegistration(filename=spec.filename, status="accepted", document=view)

    async def update_document_state(
        self,
        doc_id: UUID,
        state: str,
        *,
        stage_detail: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        progress_pct: int | None = None,
        page_count: int | None = None,
        chunk_count: int | None = None,
        parsed_object_key: str | None = None,
    ) -> None:
        """Called by ingestion workers. Drives the per-document UI (FR-P-05)."""
        async with transaction() as session:
            document = await self._repo.get_document(session, doc_id, for_update=True)
            if document is None:
                raise NotFound("Document not found.")

            document.state = state
            document.stage_detail = stage_detail
            document.error_code = error_code
            document.error_detail = error_detail
            if progress_pct is not None:
                document.progress_pct = max(0, min(100, progress_pct))
            if page_count is not None:
                document.page_count = page_count
            if chunk_count is not None:
                document.chunk_count = chunk_count
            if parsed_object_key is not None:
                document.parsed_object_key = parsed_object_key
            if state == "indexed":
                document.indexed_at = utcnow()
                document.progress_pct = 100

    async def get_document(self, doc_id: UUID) -> DocumentView:
        async with session_scope() as session:
            document = await self._repo.get_document(session, doc_id)
            if document is None or document.deleted_at is not None:
                raise NotFound("Document not found.")
            return _document_view(document)

    async def list_documents(
        self,
        kb_id: UUID,
        *,
        state: str | None = None,
        source_type: str | None = None,
        search: str | None = None,
        limit: int = 50,
        cursor_id: UUID | None = None,
    ) -> list[DocumentView]:
        async with session_scope() as session:
            rows = await self._repo.list_documents(
                session,
                kb_id,
                state=state,
                source_type=source_type,
                search=search,
                limit=limit,
                cursor_id=cursor_id,
            )
        return [_document_view(row) for row in rows]

    async def document_state_counts(self, kb_id: UUID) -> dict[str, int]:
        async with session_scope() as session:
            return await self._repo.count_documents_by_state(session, kb_id)

    async def delete_document(self, actor: Principal, doc_id: UUID) -> None:
        async with transaction() as session:
            document = await self._repo.get_document(session, doc_id, for_update=True)
            if document is None:
                raise NotFound("Document not found.")

            before = document.snapshot()
            document.deleted_at = utcnow()
            document.state = "deleting"

            await self._tasks.cancel_for_document(doc_id)
            await self._tasks.enqueue(
                session,
                TaskSpec(
                    queue="maintain",
                    kind="document.purge",
                    workspace_id=document.workspace_id,
                    kb_id=document.kb_id,
                    document_id=doc_id,
                    payload={"document_id": str(doc_id)},
                    dedupe_key=f"document.purge:{doc_id}",
                ),
            )
            await self._repo.adjust_counters(
                session, document.kb_id, docs=-1, bytes_used=-(document.size_bytes or 0)
            )
            await self._audit.record(
                session,
                workspace_id=document.workspace_id,
                action="document.delete",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="document",
                resource_id=doc_id,
                before=before,
            )

    async def retry_document(self, actor: Principal, doc_id: UUID) -> None:
        """Re-enqueue a failed document from the parse stage."""
        async with transaction() as session:
            document = await self._repo.get_document(session, doc_id, for_update=True)
            if document is None:
                raise NotFound("Document not found.")
            if document.state != "failed":
                raise Conflict(f"Document is {document.state}, not failed.")

            document.state = "registered"
            document.error_code = None
            document.error_detail = None
            document.progress_pct = 0
            document.revision += 1  # a fresh revision frees the dedupe key

            kb = await self._repo.get_kb(session, document.kb_id, for_update=True)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            index_version = kb.building_index_version or kb.active_index_version
            if index_version is None:
                raise Conflict("The knowledge base has no index version available for retry.")
            await self._repo.add_document_ingestion(
                session,
                DocumentIngestion(
                    document_id=document.id,
                    revision=document.revision,
                    kb_id=document.kb_id,
                    index_version=index_version,
                    source_content_hash=document.content_hash,
                ),
            )

            await self._tasks.enqueue(
                session,
                TaskSpec(
                    queue="parse",
                    kind="document.parse",
                    workspace_id=document.workspace_id,
                    kb_id=document.kb_id,
                    document_id=doc_id,
                    payload={
                        "revision": document.revision,
                        "index_version": index_version,
                    },
                    dedupe_key=f"parse:{doc_id}:{document.revision}",
                ),
            )
        await self._tasks.notify("parse")

    # -------------------------------------------------------------------- chunks

    async def replace_chunks(
        self, kb_id: UUID, document_id: UUID, index_version: int, chunks: list[ChunkSpec]
    ) -> int:
        """Bulk-write a document's chunks, preserving manual edits (FR-F-09).

        A user who hand-fixed a badly-parsed table must not lose that work every
        time chunking is retuned. Edits are matched by ordinal; when a config
        change moves boundaries the ordinals no longer line up, so the edit is
        reported as orphaned rather than silently dropped or wrongly applied.
        """
        async with transaction() as session:
            document = await self._repo.get_document(session, document_id, for_update=True)
            if document is None:
                raise NotFound("Document not found.")

            preserved = {
                chunk.ordinal: chunk
                for chunk in await self._repo.edited_chunks(session, kb_id, document_id)
            }

            rows: list[dict[str, object]] = []
            carried = 0
            for spec in chunks:
                edit = preserved.get(spec.ordinal)
                if edit is not None:
                    carried += 1
                rows.append(
                    {
                        "kb_id": kb_id,
                        "id": spec.id,
                        "workspace_id": document.workspace_id,
                        "document_id": document_id,
                        "index_version": index_version,
                        "parent_id": spec.parent_id,
                        "ordinal": spec.ordinal,
                        "content": edit.content if edit is not None else spec.content,
                        "content_hash": (
                            edit.content_hash if edit is not None else spec.content_hash
                        ),
                        "token_count": spec.token_count,
                        "chunk_metadata": spec.metadata,
                        "is_edited": edit is not None,
                    }
                )

            written = await self._repo.replace_chunks(
                session, kb_id, document_id, index_version, rows
            )
            document.chunk_count = written

        orphaned = len(preserved) - carried
        if orphaned > 0:
            log.warning(
                "catalog.chunk_edits_orphaned",
                kb_id=str(kb_id),
                document_id=str(document_id),
                orphaned=orphaned,
                detail="chunk boundaries moved, so these manual edits no longer align",
            )
        return written

    async def list_chunks(
        self,
        kb_id: UUID,
        document_id: UUID,
        *,
        index_version: int | None = None,
        limit: int = 50,
        after_ordinal: int | None = None,
    ) -> list[ChunkView]:
        async with session_scope() as session:
            if index_version is None:
                kb = await self._repo.get_kb(session, kb_id)
                if kb is None:
                    raise NotFound("Knowledge base not found.")
                index_version = kb.active_index_version or 1
            rows = await self._repo.list_chunks(
                session,
                kb_id,
                document_id,
                index_version,
                limit=limit,
                after_ordinal=after_ordinal,
            )
        return [_chunk_view(row) for row in rows]

    async def edit_chunk(
        self, actor: Principal, kb_id: UUID, chunk_id: UUID, content: str
    ) -> ChunkView:
        if not content.strip():
            raise ValidationFailed("Chunk content cannot be empty.")

        async with transaction() as session:
            kb = await self._repo.get_kb(session, kb_id)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            chunk = await self._repo.get_chunk(session, kb_id, chunk_id, for_update=True)
            if chunk is None:
                raise NotFound("Chunk not found.")
            if chunk.index_version != kb.active_index_version:
                # Editing a retired or building version would be discarded by
                # the next switch, which looks like data loss to the user.
                raise ChunkNotEditable(
                    "This chunk belongs to an index version that is not active. "
                    "Wait for the current rebuild to finish."
                )

            from hashlib import sha256

            before = {"content": chunk.content[:200], "is_edited": chunk.is_edited}
            chunk.content = content
            chunk.content_hash = sha256(content.encode()).hexdigest()
            chunk.is_edited = True

            await self._tasks.enqueue(
                session,
                TaskSpec(
                    queue="embed",
                    kind="chunk.reembed",
                    workspace_id=kb.workspace_id,
                    kb_id=kb_id,
                    document_id=chunk.document_id,
                    payload={"chunk_id": str(chunk_id), "index_version": chunk.index_version},
                    priority=200,  # a user is waiting to see the effect
                    dedupe_key=f"reembed:{chunk_id}:{chunk.content_hash}",
                ),
            )
            await self._audit.record(
                session,
                workspace_id=kb.workspace_id,
                action="chunk.edit",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="chunk",
                resource_id=chunk_id,
                before=before,
                after={"content": content[:200], "is_edited": True},
            )
            view = _chunk_view(chunk)

        await self._tasks.notify("embed")
        return view

    # ----------------------------------------------------------- storage bindings

    async def _require_binding(
        self, session: AsyncSession, actor: Principal, binding_id: UUID, kind: str
    ) -> StorageBinding:
        """Resolve a binding for knowledge-base creation, or explain why not.

        Each check here corresponds to a failure that would otherwise surface
        much later — a cross-workspace binding as a tenancy leak, a swapped
        vector/object pair as an unreadable index, an unreachable backend as a
        pile of documents stuck in `fetching`.
        """
        binding = await self._repo.get_binding(session, binding_id)
        if binding is None or binding.workspace_id != actor.workspace_id:
            raise NotFound(f"Storage binding {binding_id} not found.")
        if binding.kind != kind:
            raise ValidationFailed(
                f"{binding.name!r} is a {binding.kind} binding, but a {kind} binding "
                "is required here."
            )
        if binding.health_state == "unavailable":
            raise ValidationFailed(
                f"{binding.name!r} is currently unreachable. Fix or re-test the binding "
                "before creating a knowledge base against it."
            )
        return binding

    async def create_binding(
        self,
        actor: Principal,
        *,
        kind: str,
        driver: str,
        name: str,
        config: dict[str, object] | None = None,
        is_default: bool = False,
    ) -> BindingRef:
        if kind not in ("vector", "object"):
            raise ValidationFailed("Binding kind must be 'vector' or 'object'.")

        async with transaction() as session:
            binding = await self._repo.add_binding(
                session,
                StorageBinding(
                    workspace_id=actor.workspace_id,
                    kind=kind,
                    driver=driver,
                    name=name,
                    config=dict(config or {}),
                    is_default=is_default,
                ),
            )
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="storage_binding.create",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="storage_binding",
                resource_id=binding.id,
                after={"kind": kind, "driver": driver, "name": name},
            )
            ref = _binding_ref(binding)

        # Verified immediately: a binding that cannot be reached should fail at
        # configuration time, not when the first document is uploaded.
        await self.test_binding(ref.id)
        return ref

    async def list_bindings(self, workspace_id: UUID, kind: str | None = None) -> list[BindingRef]:
        async with session_scope() as session:
            rows = await self._repo.list_bindings(session, workspace_id, kind)
        return [_binding_ref(row) for row in rows]

    async def test_binding(self, binding_id: UUID) -> bool:
        async with session_scope() as session:
            binding = await self._repo.get_binding(session, binding_id)
        if binding is None:
            raise NotFound("Storage binding not found.")

        healthy = False
        try:
            if binding.kind == "object":
                from cairn.objectstore.registry import ObjectBindingRef, get_object_registry

                store = await get_object_registry().for_binding(
                    ObjectBindingRef(binding.id, binding.driver, dict(binding.config))
                )
                healthy = await store.health()
            else:
                from cairn.vectorstore.registry import VectorBindingRef, get_vector_registry

                vector_store = await get_vector_registry().for_binding(
                    VectorBindingRef(binding.id, binding.driver, dict(binding.config))
                )
                healthy = (await vector_store.health()).healthy
        except Exception as exc:
            log.warning(
                "catalog.binding_unhealthy",
                binding_id=str(binding_id),
                driver=binding.driver,
                error=type(exc).__name__,
            )

        async with transaction() as session:
            await self._repo.set_binding_health(
                session, binding_id, "healthy" if healthy else "unavailable"
            )
        return healthy

    async def delete_binding(self, actor: Principal, binding_id: UUID) -> None:
        async with transaction() as session:
            binding = await self._repo.get_binding(session, binding_id)
            if binding is None or binding.workspace_id != actor.workspace_id:
                raise NotFound("Storage binding not found.")
            if await self._repo.binding_in_use(session, binding_id):
                # Deleting it would leave knowledge bases pointing at storage
                # nobody can resolve.
                raise BindingInUse(
                    "This binding is in use by at least one knowledge base. "
                    "Move or delete those knowledge bases first."
                )
            await session.delete(binding)
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="storage_binding.delete",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="storage_binding",
                resource_id=binding_id,
            )

    async def namespace_spec_for(self, kb_id: UUID) -> tuple[Namespace, NamespaceSpec]:
        """What the ingestion pipeline needs to write vectors."""
        async with session_scope() as session:
            kb = await self._repo.get_kb(session, kb_id)
            if kb is None:
                raise NotFound("Knowledge base not found.")
            version = kb.building_index_version or kb.active_index_version or 1
            return (
                Namespace(kb_id, version),
                NamespaceSpec(dim=kb.embedding_dim, metric=kb.metric),  # type: ignore[arg-type]
            )


def _runtime_key(kb_id: UUID) -> str:
    return f"kb:runtime:{kb_id}"


def _kb_view(kb: KnowledgeBase) -> KnowledgeBaseView:
    return KnowledgeBaseView(
        id=kb.id,
        workspace_id=kb.workspace_id,
        name=kb.name,
        slug=kb.slug,
        description=kb.description,
        icon_url=None,
        embedding_model_id=kb.embedding_model_id,
        embedding_dim=kb.embedding_dim,
        metric=kb.metric,
        vector_binding_id=kb.vector_binding_id,
        object_binding_id=kb.object_binding_id,
        chunk_config=ChunkConfig.model_validate(kb.chunk_config or {}),
        retrieval_config=RetrievalConfig.model_validate(kb.retrieval_config or {}),
        active_index_version=kb.active_index_version,
        building_index_version=kb.building_index_version,
        config_version=kb.config_version,
        owner_user_id=kb.owner_user_id,
        status=kb.status,  # type: ignore[arg-type]
        doc_count=kb.doc_count,
        chunk_count=kb.chunk_count,
        bytes_used=kb.bytes_used,
        last_indexed_at=kb.last_indexed_at,
        created_at=kb.created_at,
    )


def _with_reindex_flag(view: KnowledgeBaseView, required: bool) -> KnowledgeBaseView:
    from dataclasses import replace

    return replace(view, reindex_required=required)


def _document_view(document: Document) -> DocumentView:
    return DocumentView(
        id=document.id,
        kb_id=document.kb_id,
        title=document.title,
        source_type=document.source_type,
        source_ref=document.source_ref,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        content_hash=document.content_hash,
        state=document.state,  # type: ignore[arg-type]
        stage_detail=document.stage_detail,
        error_code=document.error_code,
        error_detail=document.error_detail,
        progress_pct=document.progress_pct,
        revision=document.revision,
        page_count=document.page_count,
        chunk_count=document.chunk_count,
        created_at=document.created_at,
        indexed_at=document.indexed_at,
    )


def _chunk_view(chunk: Chunk) -> ChunkView:
    return ChunkView(
        id=chunk.id,
        kb_id=chunk.kb_id,
        document_id=chunk.document_id,
        index_version=chunk.index_version,
        parent_id=chunk.parent_id,
        ordinal=chunk.ordinal,
        content=chunk.content,
        token_count=chunk.token_count,
        metadata=dict(chunk.chunk_metadata),
        is_edited=chunk.is_edited,
    )


def _binding_ref(binding: StorageBinding) -> BindingRef:
    return BindingRef(
        id=binding.id,
        kind=binding.kind,  # type: ignore[arg-type]
        driver=binding.driver,
        name=binding.name,
        config=dict(binding.config),
        health_state=binding.health_state,
    )


def _index_version_view(row: KbIndexVersion) -> IndexVersionView:
    return IndexVersionView(
        kb_id=row.kb_id,
        version=row.version,
        state=row.state,  # type: ignore[arg-type]
        layout=row.layout,
        chunk_total=row.chunk_total,
        chunk_done=row.chunk_done,
        started_at=row.started_at,
        completed_at=row.completed_at,
        error=row.error,
    )


_service: CatalogService | None = None


def get_catalog_service() -> CatalogService:
    global _service
    if _service is None:
        _service = CatalogService()
    return _service


def reset_catalog_service() -> None:
    global _service
    _service = None
