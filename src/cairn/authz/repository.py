"""Authorization data access."""

from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from sqlalchemy import any_, delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.authz.models import ApiKey, ResourceGrant
from cairn.core.time import utcnow

__all__ = ["AuthzRepository", "UserSummary"]


class UserSummary(TypedDict):
    """The minimum needed to build a principal.

    Declared as a TypedDict so the resolver can read fields without a cast at
    every use — and so a schema change breaks the type check rather than
    producing a runtime AttributeError somewhere downstream.
    """

    id: UUID
    workspace_id: UUID
    username: str
    role: str
    is_active: bool
    must_change_password: bool
    credential_version: int
    perm_version: int


class AuthzRepository:
    # --- grants -------------------------------------------------------------

    async def live_grants(
        self, session: AsyncSession, subject_type: str, subject_id: UUID
    ) -> list[ResourceGrant]:
        """Grants that are in force *right now*.

        Expiry and revocation are filtered here rather than swept by a job, so a
        break-glass window closes exactly when it says it will.
        """
        now = utcnow()
        stmt = select(ResourceGrant).where(
            ResourceGrant.subject_type == subject_type,
            ResourceGrant.subject_id == subject_id,
            ResourceGrant.revoked_at.is_(None),
            (ResourceGrant.expires_at.is_(None)) | (ResourceGrant.expires_at > now),
        )
        return list((await session.scalars(stmt)).all())

    async def grants_on_resource(
        self, session: AsyncSession, resource_type: str, resource_id: UUID
    ) -> list[ResourceGrant]:
        now = utcnow()
        stmt = select(ResourceGrant).where(
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.revoked_at.is_(None),
            (ResourceGrant.expires_at.is_(None)) | (ResourceGrant.expires_at > now),
        )
        return list((await session.scalars(stmt)).all())

    async def add_grant(self, session: AsyncSession, grant: ResourceGrant) -> ResourceGrant:
        session.add(grant)
        await session.flush()
        return grant

    async def get_grant(self, session: AsyncSession, grant_id: UUID) -> ResourceGrant | None:
        grant: ResourceGrant | None = await session.scalar(
            select(ResourceGrant).where(ResourceGrant.id == grant_id)
        )
        return grant

    async def revoke_grant(self, session: AsyncSession, grant_id: UUID) -> bool:
        result = await session.execute(
            update(ResourceGrant)
            .where(ResourceGrant.id == grant_id, ResourceGrant.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        return bool(getattr(result, "rowcount", 0))

    async def revoke_grants_for_subject(
        self, session: AsyncSession, subject_type: str, subject_id: UUID
    ) -> int:
        result = await session.execute(
            update(ResourceGrant)
            .where(
                ResourceGrant.subject_type == subject_type,
                ResourceGrant.subject_id == subject_id,
                ResourceGrant.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def purge_knowledge_base_access(
        self, session: AsyncSession, workspace_id: UUID, kb_id: UUID
    ) -> tuple[set[UUID], list[str]]:
        grants = list(
            (
                await session.scalars(
                    select(ResourceGrant).where(
                        ResourceGrant.workspace_id == workspace_id,
                        ResourceGrant.resource_type == "knowledge_base",
                        ResourceGrant.resource_id == kb_id,
                    )
                )
            ).all()
        )
        user_ids = {grant.subject_id for grant in grants if grant.subject_type == "user"}
        await session.execute(
            delete(ResourceGrant).where(
                ResourceGrant.workspace_id == workspace_id,
                ResourceGrant.resource_type == "knowledge_base",
                ResourceGrant.resource_id == kb_id,
            )
        )
        keys = list(
            (
                await session.scalars(
                    select(ApiKey).where(
                        ApiKey.workspace_id == workspace_id,
                        any_(ApiKey.kb_ids) == kb_id,
                    )
                )
            ).all()
        )
        for key in keys:
            remaining = [value for value in key.kb_ids if value != kb_id]
            if remaining:
                key.kb_ids = remaining
            else:
                key.revoked_at = utcnow()
        return user_ids, [key.key_hash for key in keys]

    # --- api keys -----------------------------------------------------------

    async def add_key(self, session: AsyncSession, key: ApiKey) -> ApiKey:
        session.add(key)
        await session.flush()
        return key

    async def get_key_by_hash(self, session: AsyncSession, key_hash: str) -> ApiKey | None:
        key: ApiKey | None = await session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
        return key

    async def get_key(self, session: AsyncSession, key_id: UUID) -> ApiKey | None:
        key: ApiKey | None = await session.scalar(select(ApiKey).where(ApiKey.id == key_id))
        return key

    async def list_keys(
        self, session: AsyncSession, workspace_id: UUID, owner_user_id: UUID | None = None
    ) -> list[ApiKey]:
        stmt = select(ApiKey).where(ApiKey.workspace_id == workspace_id)
        if owner_user_id is not None:
            stmt = stmt.where(ApiKey.owner_user_id == owner_user_id)
        return list((await session.scalars(stmt.order_by(ApiKey.created_at.desc()))).all())

    async def revoke_key(self, session: AsyncSession, key_id: UUID) -> bool:
        result = await session.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        return bool(getattr(result, "rowcount", 0))

    async def revoke_keys_for_owner(self, session: AsyncSession, owner_user_id: UUID) -> int:
        result = await session.execute(
            update(ApiKey)
            .where(ApiKey.owner_user_id == owner_user_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def record_key_use(self, session: AsyncSession, key_id: UUID) -> None:
        await session.execute(
            text(
                "UPDATE api_key SET last_used_at = now(), use_count = use_count + 1 WHERE id = :id"
            ),
            {"id": key_id},
        )

    # --- cache invalidation -------------------------------------------------

    async def bump_perm_version(self, session: AsyncSession, user_id: UUID) -> int:
        """Make every cached principal for this user unreachable (FR-B-12).

        Version bumping rather than key enumeration: SCAN+DEL is O(n) against a
        hot Redis and races with concurrent writes, whereas changing the key is
        O(1) and leaves stale entries to expire on their own.
        """
        result = await session.execute(
            text(
                'UPDATE "user" SET perm_version = perm_version + 1 '
                "WHERE id = :id RETURNING perm_version"
            ),
            {"id": user_id},
        )
        row = result.first()
        return int(row[0]) if row else 1

    async def user_summary(self, session: AsyncSession, user_id: UUID) -> UserSummary | None:
        """The minimum needed to build a principal, without importing M01's ORM."""
        result = await session.execute(
            text(
                "SELECT id, workspace_id, username, role, is_active, must_change_password, "
                'credential_version, perm_version FROM "user" '
                "WHERE id = :id AND deleted_at IS NULL"
            ),
            {"id": user_id},
        )
        row = result.mappings().first()
        return UserSummary(**dict(row)) if row else None  # type: ignore[typeddict-item]

    async def workspace_admin_content_access(
        self, session: AsyncSession, workspace_id: UUID
    ) -> str:
        """Read one settings field directly (FR-B-09).

        Deliberate exception to "read another module's tables through its
        facade": this runs on the data-plane authentication path, and importing
        ``cairn.platform.settings`` here would give M09 a transitive
        control-plane dependency the isolation contract forbids.

        Fails closed — an unreadable policy is treated as the strictest one.
        """
        try:
            value = await session.scalar(
                text(
                    "SELECT COALESCE(settings->>'admin_content_access', 'break_glass') "
                    "FROM workspace WHERE id = :ws"
                ),
                {"ws": workspace_id},
            )
        except Exception:
            return "break_glass"
        return str(value or "break_glass")

    async def workspace_kb_ids(self, session: AsyncSession, workspace_id: UUID) -> list[UUID]:
        """Knowledge bases in the workspace.

        Uses a guarded raw query because ``knowledge_base`` does not exist until
        Phase 2 — an administrator's implicit access must not depend on a table
        that has not been created yet.
        """
        exists = await session.scalar(text("SELECT to_regclass('public.knowledge_base')"))
        if not exists:
            return []
        result = await session.execute(
            text("SELECT id FROM knowledge_base WHERE workspace_id = :ws AND deleted_at IS NULL"),
            {"ws": workspace_id},
        )
        return [row[0] for row in result.all()]
