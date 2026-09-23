"""Catalog data access."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.catalog.models import (
    Chunk,
    Document,
    DocumentIngestion,
    KbIndexVersion,
    KnowledgeBase,
    StorageBinding,
)
from cairn.core.time import utcnow

__all__ = ["CatalogRepository"]


class CatalogRepository:
    async def has_edited_chunks(
        self, session: AsyncSession, kb_id: UUID, index_version: int
    ) -> bool:
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        Chunk.kb_id == kb_id,
                        Chunk.index_version == index_version,
                        Chunk.is_edited.is_(True),
                    )
                )
            )
        )

    # --- knowledge bases ----------------------------------------------------

    async def add_kb(self, session: AsyncSession, kb: KnowledgeBase) -> KnowledgeBase:
        session.add(kb)
        await session.flush()
        return kb

    async def get_kb(
        self, session: AsyncSession, kb_id: UUID, *, for_update: bool = False
    ) -> KnowledgeBase | None:
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.id == kb_id, KnowledgeBase.deleted_at.is_(None)
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        kb: KnowledgeBase | None = await session.scalar(stmt)
        return kb

    async def get_kb_for_maintenance(
        self, session: AsyncSession, kb_id: UUID, *, for_update: bool = False
    ) -> KnowledgeBase | None:
        stmt = select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        kb: KnowledgeBase | None = await session.scalar(stmt)
        return kb

    async def slug_taken(
        self, session: AsyncSession, workspace_id: UUID, slug: str, *, exclude: UUID | None = None
    ) -> bool:
        conditions = [
            KnowledgeBase.workspace_id == workspace_id,
            KnowledgeBase.slug == slug,
            KnowledgeBase.deleted_at.is_(None),
        ]
        if exclude is not None:
            conditions.append(KnowledgeBase.id != exclude)
        return bool(await session.scalar(select(exists().where(*conditions))))

    async def list_kbs(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        *,
        kb_ids: list[UUID] | None = None,
        limit: int = 50,
        cursor_id: UUID | None = None,
    ) -> list[KnowledgeBase]:
        stmt = select(KnowledgeBase).where(
            KnowledgeBase.workspace_id == workspace_id, KnowledgeBase.deleted_at.is_(None)
        )
        if kb_ids is not None:
            # An empty list means "no access", which must return nothing rather
            # than silently degrading to "everything".
            stmt = stmt.where(KnowledgeBase.id.in_(kb_ids)) if kb_ids else stmt.where(text("FALSE"))
        if cursor_id is not None:
            stmt = stmt.where(KnowledgeBase.id > cursor_id)
        return list((await session.scalars(stmt.order_by(KnowledgeBase.id).limit(limit))).all())

    async def runtime_refresh_ids(
        self, session: AsyncSession, *, after: UUID | None, limit: int
    ) -> list[UUID]:
        stmt = select(KnowledgeBase.id)
        if after is not None:
            stmt = stmt.where(KnowledgeBase.id > after)
        return list((await session.scalars(stmt.order_by(KnowledgeBase.id).limit(limit))).all())

    async def model_is_referenced(
        self, session: AsyncSession, workspace_id: UUID, model_id: UUID
    ) -> bool:
        current = await session.scalar(
            select(
                exists().where(
                    KnowledgeBase.workspace_id == workspace_id,
                    KnowledgeBase.embedding_model_id == model_id,
                )
            )
        )
        if current:
            return True
        snapshots = await session.scalars(
            select(KbIndexVersion.config_snapshot).where(
                KbIndexVersion.workspace_id == workspace_id,
                KbIndexVersion.config_snapshot.is_not(None),
            )
        )
        return any(
            isinstance(snapshot, dict)
            and isinstance(snapshot.get("embedding_model"), dict)
            and snapshot["embedding_model"].get("id") == str(model_id)
            for snapshot in snapshots
        )

    async def bump_config_version(self, session: AsyncSession, kb_id: UUID) -> int:
        result = await session.execute(
            text(
                "UPDATE knowledge_base SET config_version = config_version + 1 "
                "WHERE id = :id RETURNING config_version"
            ),
            {"id": kb_id},
        )
        row = result.first()
        return int(row[0]) if row else 1

    async def adjust_counters(
        self,
        session: AsyncSession,
        kb_id: UUID,
        *,
        docs: int = 0,
        chunks: int = 0,
        bytes_used: int = 0,
    ) -> None:
        await session.execute(
            text(
                """
                UPDATE knowledge_base
                   SET doc_count   = GREATEST(0, doc_count + :docs),
                       chunk_count = GREATEST(0, chunk_count + :chunks),
                       bytes_used  = GREATEST(0, bytes_used + :bytes)
                 WHERE id = :id
                """
            ),
            {"id": kb_id, "docs": docs, "chunks": chunks, "bytes": bytes_used},
        )

    async def recount(self, session: AsyncSession, kb_id: UUID) -> None:
        """Recompute counters from the source of truth.

        They are maintained incrementally on the hot path, so a crash between an
        insert and its counter update leaves them drifted. Nightly reconciliation
        makes that self-correcting rather than permanently wrong.
        """
        await session.execute(
            text(
                """
                UPDATE knowledge_base kb
                   SET doc_count  = COALESCE(d.docs, 0),
                       bytes_used = COALESCE(d.bytes, 0)
                  FROM (
                        SELECT count(*) AS docs, COALESCE(sum(size_bytes), 0) AS bytes
                          FROM document
                         WHERE kb_id = :id AND deleted_at IS NULL
                       ) AS d
                 WHERE kb.id = :id
                """
            ),
            {"id": kb_id},
        )

    # --- index versions -----------------------------------------------------

    async def add_index_version(self, session: AsyncSession, row: KbIndexVersion) -> KbIndexVersion:
        session.add(row)
        await session.flush()
        return row

    async def get_index_version(
        self, session: AsyncSession, kb_id: UUID, version: int, *, for_update: bool = False
    ) -> KbIndexVersion | None:
        stmt = select(KbIndexVersion).where(
            KbIndexVersion.kb_id == kb_id, KbIndexVersion.version == version
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        row: KbIndexVersion | None = await session.scalar(stmt)
        return row

    async def list_index_versions(self, session: AsyncSession, kb_id: UUID) -> list[KbIndexVersion]:
        return list(
            (
                await session.scalars(
                    select(KbIndexVersion)
                    .where(KbIndexVersion.kb_id == kb_id)
                    .order_by(KbIndexVersion.version.desc())
                )
            ).all()
        )

    async def set_index_state(
        self,
        session: AsyncSession,
        kb_id: UUID,
        version: int,
        state: str,
        *,
        error: str | None = None,
        retire_after: Any = None,
    ) -> None:
        values: dict[str, Any] = {"state": state}
        if state in ("active", "failed"):
            values["completed_at"] = utcnow()
        if error is not None:
            values["error"] = error
        if retire_after is not None:
            values["retire_after"] = retire_after
        await session.execute(
            update(KbIndexVersion)
            .where(KbIndexVersion.kb_id == kb_id, KbIndexVersion.version == version)
            .values(**values)
        )

    async def set_index_progress(
        self, session: AsyncSession, kb_id: UUID, version: int, *, done: int, total: int | None
    ) -> None:
        values: dict[str, Any] = {"chunk_done": done}
        if total is not None:
            values["chunk_total"] = total
        await session.execute(
            update(KbIndexVersion)
            .where(KbIndexVersion.kb_id == kb_id, KbIndexVersion.version == version)
            .values(**values)
        )

    async def expired_index_versions(self, session: AsyncSession) -> list[KbIndexVersion]:
        return list(
            (
                await session.scalars(
                    select(KbIndexVersion).where(
                        KbIndexVersion.state == "retired",
                        KbIndexVersion.retire_after.is_not(None),
                        KbIndexVersion.retire_after < utcnow(),
                    )
                )
            ).all()
        )

    async def drop_index_version_row(
        self, session: AsyncSession, kb_id: UUID, version: int
    ) -> None:
        await session.execute(
            delete(KbIndexVersion).where(
                KbIndexVersion.kb_id == kb_id, KbIndexVersion.version == version
            )
        )

    async def finalize_index_drop(self, session: AsyncSession, kb_id: UUID, version: int) -> None:
        await session.execute(
            delete(Chunk).where(Chunk.kb_id == kb_id, Chunk.index_version == version)
        )
        await self.drop_index_version_row(session, kb_id, version)

    # --- documents ----------------------------------------------------------

    async def add_document(self, session: AsyncSession, document: Document) -> Document:
        session.add(document)
        await session.flush()
        return document

    async def get_document(
        self, session: AsyncSession, doc_id: UUID, *, for_update: bool = False
    ) -> Document | None:
        stmt = select(Document).where(Document.id == doc_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        document: Document | None = await session.scalar(stmt)
        return document

    async def get_document_by_hash(
        self, session: AsyncSession, kb_id: UUID, content_hash: str
    ) -> Document | None:
        document: Document | None = await session.scalar(
            select(Document).where(Document.kb_id == kb_id, Document.content_hash == content_hash)
        )
        return document

    async def fanout_document_ids(
        self,
        session: AsyncSession,
        kb_id: UUID,
        *,
        after: UUID | None,
        limit: int,
    ) -> list[UUID]:
        stmt = select(Document.id).where(
            Document.kb_id == kb_id,
            Document.deleted_at.is_(None),
            Document.state != "deleting",
        )
        if after is not None:
            stmt = stmt.where(Document.id > after)
        return list((await session.scalars(stmt.order_by(Document.id).limit(limit))).all())

    async def missing_enrollment_document_ids(
        self,
        session: AsyncSession,
        kb_id: UUID,
        index_version: int,
        *,
        limit: int,
    ) -> list[UUID]:
        rows = await session.scalars(
            select(Document.id)
            .where(
                Document.kb_id == kb_id,
                Document.deleted_at.is_(None),
                Document.state != "deleting",
                ~exists().where(
                    DocumentIngestion.document_id == Document.id,
                    DocumentIngestion.revision == Document.revision,
                    DocumentIngestion.kb_id == kb_id,
                    DocumentIngestion.index_version == index_version,
                    DocumentIngestion.source_content_hash == Document.content_hash,
                ),
            )
            .order_by(Document.id)
            .limit(limit)
        )
        return list(rows.all())

    async def lock_documents(
        self, session: AsyncSession, document_ids: list[UUID]
    ) -> list[Document]:
        if not document_ids:
            return []
        rows = await session.scalars(
            select(Document)
            .where(Document.id.in_(document_ids))
            .order_by(Document.id)
            .with_for_update()
        )
        return list(rows.all())

    async def enroll_document_ingestion(
        self, session: AsyncSession, run: DocumentIngestion
    ) -> bool:
        result = await session.execute(
            pg_insert(DocumentIngestion)
            .values(
                document_id=run.document_id,
                revision=run.revision,
                kb_id=run.kb_id,
                index_version=run.index_version,
                source_content_hash=run.source_content_hash,
                state="registered",
                prior_parsed_object_key=run.prior_parsed_object_key,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    DocumentIngestion.document_id,
                    DocumentIngestion.revision,
                    DocumentIngestion.index_version,
                ]
            )
            .returning(DocumentIngestion.document_id)
        )
        return result.scalar_one_or_none() is not None

    async def reusable_parsed_object_key(
        self,
        session: AsyncSession,
        document_id: UUID,
        revision: int,
        source_content_hash: str,
        *,
        exclude_index_version: int,
    ) -> str | None:
        return await session.scalar(
            select(DocumentIngestion.parsed_object_key)
            .where(
                DocumentIngestion.document_id == document_id,
                DocumentIngestion.revision == revision,
                DocumentIngestion.source_content_hash == source_content_hash,
                DocumentIngestion.index_version != exclude_index_version,
                DocumentIngestion.parsed_object_key.is_not(None),
                DocumentIngestion.state.in_(("parsed", "chunked", "embedded", "indexed")),
            )
            .order_by(DocumentIngestion.updated_at.desc())
            .limit(1)
        )

    async def add_document_ingestion(
        self, session: AsyncSession, run: DocumentIngestion
    ) -> DocumentIngestion:
        session.add(run)
        await session.flush()
        return run

    async def get_document_ingestion(
        self,
        session: AsyncSession,
        document_id: UUID,
        revision: int,
        index_version: int,
        *,
        for_update: bool = False,
    ) -> DocumentIngestion | None:
        stmt = select(DocumentIngestion).where(
            DocumentIngestion.document_id == document_id,
            DocumentIngestion.revision == revision,
            DocumentIngestion.index_version == index_version,
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        row: DocumentIngestion | None = await session.scalar(stmt)
        return row

    async def failed_document_ingestions(
        self,
        session: AsyncSession,
        document: Document,
        *,
        for_update: bool = False,
    ) -> list[DocumentIngestion]:
        stmt = (
            select(DocumentIngestion)
            .where(
                DocumentIngestion.document_id == document.id,
                DocumentIngestion.revision == document.revision,
                DocumentIngestion.source_content_hash == document.content_hash,
                DocumentIngestion.state == "failed",
            )
            .order_by(DocumentIngestion.index_version)
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return list((await session.scalars(stmt)).all())

    async def lock_document_ingestions(
        self, session: AsyncSession, document_id: UUID
    ) -> list[DocumentIngestion]:
        return list(
            (
                await session.scalars(
                    select(DocumentIngestion)
                    .where(DocumentIngestion.document_id == document_id)
                    .order_by(DocumentIngestion.revision, DocumentIngestion.index_version)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )

    async def lock_index_versions(self, session: AsyncSession, kb_id: UUID) -> list[KbIndexVersion]:
        return list(
            (
                await session.scalars(
                    select(KbIndexVersion)
                    .where(KbIndexVersion.kb_id == kb_id)
                    .order_by(KbIndexVersion.version)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )

    async def source_is_referenced_by_live_document(
        self,
        session: AsyncSession,
        *,
        kb_id: UUID,
        document_id: UUID,
        object_key: str,
        content_hash: str | None,
    ) -> bool:
        shared_source = Document.object_key == object_key
        if content_hash is not None:
            shared_source = or_(shared_source, Document.content_hash == content_hash)
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        Document.kb_id == kb_id,
                        Document.id != document_id,
                        Document.deleted_at.is_(None),
                        Document.state != "deleting",
                        shared_source,
                    )
                )
            )
        )

    async def count_live_chunks(
        self, session: AsyncSession, kb_id: UUID, index_version: int
    ) -> int:
        return int(
            await session.scalar(
                select(func.count(Chunk.id))
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Chunk.kb_id == kb_id,
                    Chunk.index_version == index_version,
                    Document.kb_id == kb_id,
                    Document.deleted_at.is_(None),
                    Document.state != "deleting",
                )
            )
            or 0
        )

    async def finalize_document_purge(
        self,
        session: AsyncSession,
        *,
        document: Document,
        kb: KnowledgeBase,
    ) -> None:
        await session.execute(
            delete(Chunk).where(
                Chunk.kb_id == document.kb_id,
                Chunk.document_id == document.id,
            )
        )
        await session.delete(document)
        await session.flush()
        kb.doc_count = await self.live_document_count(session, kb.id)
        kb.bytes_used = int(
            await session.scalar(
                select(func.coalesce(func.sum(Document.size_bytes), 0)).where(
                    Document.kb_id == kb.id,
                    Document.deleted_at.is_(None),
                    Document.state != "deleting",
                )
            )
            or 0
        )
        kb.chunk_count = (
            await self.count_live_chunks(session, kb.id, kb.active_index_version)
            if kb.active_index_version is not None
            else 0
        )

    async def finalize_kb_purge(self, session: AsyncSession, kb: KnowledgeBase) -> None:
        await session.execute(delete(Chunk).where(Chunk.kb_id == kb.id))
        await session.delete(kb)

    async def list_documents(
        self,
        session: AsyncSession,
        kb_id: UUID,
        *,
        state: str | None = None,
        source_type: str | None = None,
        search: str | None = None,
        limit: int = 50,
        cursor_id: UUID | None = None,
    ) -> list[Document]:
        stmt = select(Document).where(Document.kb_id == kb_id, Document.deleted_at.is_(None))
        if state is not None:
            stmt = stmt.where(Document.state == state)
        if source_type is not None:
            stmt = stmt.where(Document.source_type == source_type)
        if search:
            stmt = stmt.where(Document.title.ilike(f"%{search}%"))
        if cursor_id is not None:
            stmt = stmt.where(Document.id > cursor_id)
        return list((await session.scalars(stmt.order_by(Document.id).limit(limit))).all())

    async def count_documents_by_state(self, session: AsyncSession, kb_id: UUID) -> dict[str, int]:
        result = await session.execute(
            select(Document.state, func.count(Document.id))
            .where(Document.kb_id == kb_id, Document.deleted_at.is_(None))
            .group_by(Document.state)
        )
        return {str(row[0]): int(row[1]) for row in result.all()}

    # --- chunks -------------------------------------------------------------

    async def replace_chunks(
        self,
        session: AsyncSession,
        kb_id: UUID,
        document_id: UUID,
        index_version: int,
        rows: list[dict[str, Any]],
    ) -> int:
        await session.execute(
            delete(Chunk).where(
                Chunk.kb_id == kb_id,
                Chunk.document_id == document_id,
                Chunk.index_version == index_version,
            )
        )
        if not rows:
            return 0
        # Bulk insert. A per-row loop over 500 chunks is 500 round trips.
        await session.execute(insert(Chunk), rows)
        return len(rows)

    async def clear_document_version_chunks(
        self, session: AsyncSession, kb_id: UUID, document_id: UUID, index_version: int
    ) -> None:
        await session.execute(
            delete(Chunk).where(
                Chunk.kb_id == kb_id,
                Chunk.document_id == document_id,
                Chunk.index_version == index_version,
            )
        )
        await session.execute(
            update(DocumentIngestion)
            .where(
                DocumentIngestion.document_id == document_id,
                DocumentIngestion.kb_id == kb_id,
                DocumentIngestion.index_version == index_version,
            )
            .values(point_count=0)
        )

    async def list_chunks(
        self,
        session: AsyncSession,
        kb_id: UUID,
        document_id: UUID,
        index_version: int,
        *,
        limit: int = 50,
        after_ordinal: int | None = None,
    ) -> list[Chunk]:
        stmt = select(Chunk).where(
            Chunk.kb_id == kb_id,
            Chunk.document_id == document_id,
            Chunk.index_version == index_version,
        )
        if after_ordinal is not None:
            stmt = stmt.where(Chunk.ordinal > after_ordinal)
        return list((await session.scalars(stmt.order_by(Chunk.ordinal).limit(limit))).all())

    async def ingestion_chunks(
        self, session: AsyncSession, kb_id: UUID, document_id: UUID, index_version: int
    ) -> list[Chunk]:
        return list(
            (
                await session.scalars(
                    select(Chunk)
                    .where(
                        Chunk.kb_id == kb_id,
                        Chunk.document_id == document_id,
                        Chunk.index_version == index_version,
                    )
                    .order_by(Chunk.ordinal)
                )
            ).all()
        )

    async def has_document_version_chunks(
        self, session: AsyncSession, kb_id: UUID, document_id: UUID, index_version: int
    ) -> bool:
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        Chunk.kb_id == kb_id,
                        Chunk.document_id == document_id,
                        Chunk.index_version == index_version,
                    )
                )
            )
        )

    async def pending_ingestions(
        self, session: AsyncSession, kb_id: UUID, index_version: int
    ) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(DocumentIngestion)
                .join(Document, Document.id == DocumentIngestion.document_id)
                .where(
                    Document.revision == DocumentIngestion.revision,
                    Document.deleted_at.is_(None),
                    Document.state != "deleting",
                    DocumentIngestion.kb_id == kb_id,
                    DocumentIngestion.index_version == index_version,
                    DocumentIngestion.state.not_in(("indexed", "failed")),
                )
            )
            or 0
        )

    async def live_document_count(self, session: AsyncSession, kb_id: UUID) -> int:
        return int(
            await session.scalar(
                select(func.count(Document.id)).where(
                    Document.kb_id == kb_id,
                    Document.deleted_at.is_(None),
                    Document.state != "deleting",
                )
            )
            or 0
        )

    async def has_document_history(self, session: AsyncSession, kb_id: UUID) -> bool:
        return bool(await session.scalar(select(exists().where(Document.kb_id == kb_id))))

    async def missing_current_ingestions(
        self, session: AsyncSession, kb_id: UUID, index_version: int
    ) -> int:
        return int(
            await session.scalar(
                select(func.count(Document.id)).where(
                    Document.kb_id == kb_id,
                    Document.deleted_at.is_(None),
                    Document.state != "deleting",
                    ~exists().where(
                        DocumentIngestion.document_id == Document.id,
                        DocumentIngestion.revision == Document.revision,
                        DocumentIngestion.kb_id == kb_id,
                        DocumentIngestion.index_version == index_version,
                        DocumentIngestion.source_content_hash == Document.content_hash,
                        DocumentIngestion.state == "indexed",
                    ),
                )
            )
            or 0
        )

    async def deleted_target_point_count(
        self, session: AsyncSession, kb_id: UUID, index_version: int
    ) -> int:
        return int(
            await session.scalar(
                select(func.count(Chunk.id))
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Chunk.kb_id == kb_id,
                    Chunk.index_version == index_version,
                    Chunk.chunk_metadata["embed"].as_boolean().is_not(False),
                    (Document.deleted_at.is_not(None)) | (Document.state == "deleting"),
                )
            )
            or 0
        )

    async def ingestion_point_total(
        self, session: AsyncSession, kb_id: UUID, index_version: int, *, indexed_only: bool
    ) -> int:
        stmt = select(func.coalesce(func.sum(DocumentIngestion.point_count), 0)).where(
            Document.id == DocumentIngestion.document_id,
            Document.revision == DocumentIngestion.revision,
            Document.deleted_at.is_(None),
            Document.state != "deleting",
            DocumentIngestion.state != "failed",
            DocumentIngestion.kb_id == kb_id,
            DocumentIngestion.index_version == index_version,
        )
        if indexed_only:
            stmt = stmt.where(DocumentIngestion.state == "indexed")
        return int(await session.scalar(stmt) or 0)

    async def get_chunk(
        self, session: AsyncSession, kb_id: UUID, chunk_id: UUID, *, for_update: bool = False
    ) -> Chunk | None:
        stmt = select(Chunk).where(Chunk.kb_id == kb_id, Chunk.id == chunk_id)
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        chunk: Chunk | None = await session.scalar(stmt)
        return chunk

    async def edited_chunks(
        self,
        session: AsyncSession,
        kb_id: UUID,
        document_id: UUID,
        index_version: int | None = None,
    ) -> list[Chunk]:
        """Manual edits, so a reindex can carry them forward (FR-F-09)."""
        stmt = select(Chunk).where(
            Chunk.kb_id == kb_id,
            Chunk.document_id == document_id,
            Chunk.is_edited.is_(True),
        )
        if index_version is not None:
            stmt = stmt.where(Chunk.index_version == index_version)
        return list((await session.scalars(stmt.order_by(Chunk.ordinal))).all())

    async def chunk_hashes(
        self, session: AsyncSession, kb_id: UUID, document_id: UUID, index_version: int
    ) -> dict[UUID, str]:
        result = await session.execute(
            select(Chunk.id, Chunk.content_hash).where(
                Chunk.kb_id == kb_id,
                Chunk.document_id == document_id,
                Chunk.index_version == index_version,
            )
        )
        return {row[0]: row[1] for row in result.all()}

    async def count_chunks(
        self, session: AsyncSession, kb_id: UUID, index_version: int | None = None
    ) -> int:
        stmt = select(func.count()).select_from(Chunk).where(Chunk.kb_id == kb_id)
        if index_version is not None:
            stmt = stmt.where(Chunk.index_version == index_version)
        return int(await session.scalar(stmt) or 0)

    async def sum_tokens(self, session: AsyncSession, kb_id: UUID, index_version: int) -> int:
        total = await session.scalar(
            select(func.coalesce(func.sum(Chunk.token_count), 0)).where(
                Chunk.kb_id == kb_id, Chunk.index_version == index_version
            )
        )
        return int(total or 0)

    async def sum_document_tokens(
        self,
        session: AsyncSession,
        kb_id: UUID,
        document_id: UUID,
        index_version: int,
    ) -> int:
        total = await session.scalar(
            select(func.coalesce(func.sum(Chunk.token_count), 0)).where(
                Chunk.kb_id == kb_id,
                Chunk.document_id == document_id,
                Chunk.index_version == index_version,
            )
        )
        return int(total or 0)

    async def delete_chunks_for_document(
        self, session: AsyncSession, kb_id: UUID, document_id: UUID
    ) -> int:
        result = await session.execute(
            delete(Chunk).where(Chunk.kb_id == kb_id, Chunk.document_id == document_id)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_chunks_for_kb(self, session: AsyncSession, kb_id: UUID) -> int:
        result = await session.execute(delete(Chunk).where(Chunk.kb_id == kb_id))
        return int(getattr(result, "rowcount", 0) or 0)

    # --- storage bindings ---------------------------------------------------

    async def add_binding(self, session: AsyncSession, binding: StorageBinding) -> StorageBinding:
        session.add(binding)
        await session.flush()
        return binding

    async def get_binding(self, session: AsyncSession, binding_id: UUID) -> StorageBinding | None:
        binding: StorageBinding | None = await session.scalar(
            select(StorageBinding).where(StorageBinding.id == binding_id)
        )
        return binding

    async def list_bindings(
        self, session: AsyncSession, workspace_id: UUID, kind: str | None = None
    ) -> list[StorageBinding]:
        stmt = select(StorageBinding).where(StorageBinding.workspace_id == workspace_id)
        if kind is not None:
            stmt = stmt.where(StorageBinding.kind == kind)
        return list((await session.scalars(stmt.order_by(StorageBinding.name))).all())

    async def set_binding_health(self, session: AsyncSession, binding_id: UUID, state: str) -> None:
        await session.execute(
            update(StorageBinding)
            .where(StorageBinding.id == binding_id)
            .values(health_state=state, checked_at=utcnow())
        )

    async def binding_in_use(self, session: AsyncSession, binding_id: UUID) -> bool:
        return bool(
            await session.scalar(
                select(
                    exists().where(
                        (KnowledgeBase.vector_binding_id == binding_id)
                        | (KnowledgeBase.object_binding_id == binding_id),
                        KnowledgeBase.deleted_at.is_(None),
                    )
                )
            )
        )
