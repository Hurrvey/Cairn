"""Catalog data access."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, exists, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.catalog.models import (
    Chunk,
    Document,
    KbIndexVersion,
    KnowledgeBase,
    StorageBinding,
)
from cairn.core.time import utcnow

__all__ = ["CatalogRepository"]


class CatalogRepository:
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
            stmt = stmt.with_for_update()
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
        self, session: AsyncSession, kb_id: UUID, version: int
    ) -> KbIndexVersion | None:
        row: KbIndexVersion | None = await session.scalar(
            select(KbIndexVersion).where(
                KbIndexVersion.kb_id == kb_id, KbIndexVersion.version == version
            )
        )
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
            stmt = stmt.with_for_update()
        document: Document | None = await session.scalar(stmt)
        return document

    async def get_document_by_hash(
        self, session: AsyncSession, kb_id: UUID, content_hash: str
    ) -> Document | None:
        document: Document | None = await session.scalar(
            select(Document).where(Document.kb_id == kb_id, Document.content_hash == content_hash)
        )
        return document

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

    async def get_chunk(
        self, session: AsyncSession, kb_id: UUID, chunk_id: UUID, *, for_update: bool = False
    ) -> Chunk | None:
        stmt = select(Chunk).where(Chunk.kb_id == kb_id, Chunk.id == chunk_id)
        if for_update:
            stmt = stmt.with_for_update()
        chunk: Chunk | None = await session.scalar(stmt)
        return chunk

    async def edited_chunks(
        self, session: AsyncSession, kb_id: UUID, document_id: UUID
    ) -> list[Chunk]:
        """Manual edits, so a reindex can carry them forward (FR-F-09)."""
        return list(
            (
                await session.scalars(
                    select(Chunk).where(
                        Chunk.kb_id == kb_id,
                        Chunk.document_id == document_id,
                        Chunk.is_edited.is_(True),
                    )
                )
            ).all()
        )

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
