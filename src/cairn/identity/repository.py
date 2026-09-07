"""Data access for identity. Returns ORM objects; only the service converts to DTOs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.core.ids import new_uuid
from cairn.core.time import utcnow
from cairn.identity.models import Session, SystemBootstrap, User
from cairn.platform.models import Workspace

__all__ = ["IdentityRepository"]

DEFAULT_WORKSPACE_SLUG = "default"


class IdentityRepository:
    # --- bootstrap ----------------------------------------------------------

    async def admin_exists(self, session: AsyncSession) -> bool:
        stmt = select(exists().where(User.role == "admin", User.deleted_at.is_(None)))
        return bool(await session.scalar(stmt))

    async def is_initialized(self, session: AsyncSession) -> bool:
        return bool(await session.scalar(select(exists().where(SystemBootstrap.id == 1))))

    async def mark_initialized(self, session: AsyncSession, *, schema_version: str) -> None:
        session.add(SystemBootstrap(id=1, schema_version=schema_version, instance_id=new_uuid()))

    async def ensure_default_workspace(self, session: AsyncSession) -> Workspace:
        workspace = await session.scalar(
            select(Workspace).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)
        )
        if workspace is None:
            workspace = Workspace(
                name="Default",
                slug=DEFAULT_WORKSPACE_SLUG,
                settings={
                    # Default per FR-B-09: admins manage knowledge bases but must
                    # open an audited, time-boxed grant to read their content.
                    "admin_content_access": "break_glass",
                    "break_glass_ttl_minutes": 60,
                },
                quota={},
            )
            session.add(workspace)
            await session.flush()
        return workspace

    # --- users --------------------------------------------------------------

    async def get(
        self, session: AsyncSession, user_id: UUID, *, for_update: bool = False
    ) -> User | None:
        stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        if for_update:
            stmt = stmt.with_for_update()
        user: User | None = await session.scalar(stmt)
        return user

    async def get_by_username(
        self, session: AsyncSession, username: str, *, for_update: bool = False
    ) -> User | None:
        # Case-insensitive: "Admin" and "admin" must not be different accounts.
        stmt = select(User).where(
            func.lower(User.username) == username.lower(), User.deleted_at.is_(None)
        )
        if for_update:
            stmt = stmt.with_for_update()
        user: User | None = await session.scalar(stmt)
        return user

    async def username_taken(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        username: str,
        *,
        exclude: UUID | None = None,
    ) -> bool:
        conditions = [
            User.workspace_id == workspace_id,
            func.lower(User.username) == username.lower(),
            User.deleted_at.is_(None),
        ]
        if exclude is not None:
            conditions.append(User.id != exclude)
        return bool(await session.scalar(select(exists().where(*conditions))))

    async def list_users(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        *,
        limit: int = 50,
        cursor_id: UUID | None = None,
    ) -> list[User]:
        stmt = select(User).where(User.workspace_id == workspace_id, User.deleted_at.is_(None))
        if cursor_id is not None:
            stmt = stmt.where(User.id > cursor_id)
        stmt = stmt.order_by(User.id).limit(limit)
        return list((await session.scalars(stmt)).all())

    async def add(self, session: AsyncSession, user: User) -> User:
        session.add(user)
        await session.flush()
        return user

    async def count_active_admins(self, session: AsyncSession, workspace_id: UUID) -> int:
        stmt = select(func.count(User.id)).where(
            User.workspace_id == workspace_id,
            User.role == "admin",
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
        return int(await session.scalar(stmt) or 0)

    # --- sessions -----------------------------------------------------------

    async def add_session(self, session: AsyncSession, record: Session) -> Session:
        session.add(record)
        await session.flush()
        return record

    async def get_session_by_token_hash(
        self, session: AsyncSession, token_hash: str, *, for_update: bool = False
    ) -> Session | None:
        stmt = select(Session).where(Session.token_hash == token_hash)
        if for_update:
            stmt = stmt.with_for_update()
        record: Session | None = await session.scalar(stmt)
        return record

    async def revoke_session(self, session: AsyncSession, session_id: UUID) -> None:
        await session.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )

    async def revoke_all_sessions(self, session: AsyncSession, user_id: UUID) -> int:
        result = await session.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def touch_session(self, session: AsyncSession, session_id: UUID) -> None:
        await session.execute(
            update(Session).where(Session.id == session_id).values(last_seen_at=utcnow())
        )
