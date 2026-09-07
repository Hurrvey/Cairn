"""Workspace settings (FR-O-06).

Cached in-process with a short TTL. Some settings are hot-reloadable and some
require a restart; the model records which, so the UI can say so rather than
appearing to apply a change that has not taken effect.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select

from cairn.core.db import session_scope, transaction
from cairn.core.errors import NotFound, ValidationFailed
from cairn.core.logging import get_logger
from cairn.core.time import utcnow
from cairn.platform.models import Workspace

__all__ = ["HOT_RELOADABLE", "SettingsService", "WorkspaceSettings", "get_settings_service"]

log = get_logger(__name__)

AdminContentAccess = Literal["always", "on_grant", "break_glass"]

#: Everything else needs a restart to take effect. Surfaced to the UI so an
#: operator is never left wondering why a change did nothing.
HOT_RELOADABLE = frozenset(
    {
        "admin_content_access",
        "break_glass_ttl_minutes",
        "default_rate_limit_rpm",
        "max_upload_bytes",
        "audit_retention_days",
        "crawler_respect_robots",
    }
)


class PasswordPolicySettings(BaseModel):
    min_length: int = Field(default=12, ge=12, le=128)
    require_classes: int = Field(default=3, ge=1, le=4)


class WorkspaceSettings(BaseModel):
    #: FR-B-09. `break_glass` is the default: an administrator manages knowledge
    #: bases but must open a time-boxed, audited grant to read their content.
    #: The alternative is telling a customer that anyone with the admin password
    #: can read their contracts.
    admin_content_access: AdminContentAccess = "break_glass"
    break_glass_ttl_minutes: int = Field(default=60, ge=5, le=1440)

    password_policy: PasswordPolicySettings = Field(default_factory=PasswordPolicySettings)
    session_ttl_minutes: int = Field(default=480, ge=5)

    default_rate_limit_rpm: int = Field(default=600, ge=1)
    max_upload_bytes: int = Field(default=209_715_200, ge=1024)
    audit_retention_days: int = Field(default=365, ge=30)
    crawler_respect_robots: bool = True

    default_embedding_model_id: str | None = None


class SettingsService:
    def __init__(self, *, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[UUID, tuple[float, WorkspaceSettings]] = {}

    async def get(self, workspace_id: UUID) -> WorkspaceSettings:
        cached = self._cache.get(workspace_id)
        now = utcnow().timestamp()
        if cached and cached[0] > now:
            return cached[1]

        async with session_scope() as session:
            workspace = await session.scalar(select(Workspace).where(Workspace.id == workspace_id))
        if workspace is None:
            raise NotFound("Workspace not found.")

        settings = WorkspaceSettings.model_validate(workspace.settings or {})
        self._cache[workspace_id] = (now + self._ttl, settings)
        return settings

    async def update(self, workspace_id: UUID, patch: dict[str, Any]) -> WorkspaceSettings:
        current = await self.get(workspace_id)
        merged = {**current.model_dump(), **patch}
        try:
            updated = WorkspaceSettings.model_validate(merged)
        except Exception as exc:
            raise ValidationFailed(f"Invalid settings: {exc}") from exc

        async with transaction() as session:
            workspace = await session.scalar(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            if workspace is None:
                raise NotFound("Workspace not found.")
            workspace.settings = updated.model_dump()

        self._cache.pop(workspace_id, None)
        requires_restart = sorted(set(patch) - HOT_RELOADABLE)
        log.info(
            "settings.updated",
            workspace_id=str(workspace_id),
            changed=sorted(patch),
            requires_restart=requires_restart,
        )
        return updated

    def invalidate(self, workspace_id: UUID | None = None) -> None:
        if workspace_id is None:
            self._cache.clear()
        else:
            self._cache.pop(workspace_id, None)


_service: SettingsService | None = None


def get_settings_service() -> SettingsService:
    global _service
    if _service is None:
        _service = SettingsService()
    return _service


def reset_settings_service() -> None:
    global _service
    _service = None
