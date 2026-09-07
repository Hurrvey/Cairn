"""Audit recording (FR-O-01).

State-change audits are written **inside the caller's transaction**, so a
rolled-back operation leaves no misleading audit entry and a committed one is
never missing its record. Pass the session for those; omit it for
fire-and-forget events such as authorization denials.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from cairn.core.db import transaction
from cairn.core.logging import REDACTED, get_logger
from cairn.platform.models import AuditLog

__all__ = ["AuditService", "Outcome", "get_audit_service"]

log = get_logger(__name__)

Outcome = Literal["success", "failure", "denied"]

_SENSITIVE_FIELDS = frozenset(
    {"password", "password_hash", "token", "token_hash", "secret", "api_key", "master_key"}
)


def _scrub(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    return {k: (REDACTED if k in _SENSITIVE_FIELDS else v) for k, v in payload.items()}


class AuditService:
    async def record(
        self,
        session: AsyncSession | None = None,
        *,
        workspace_id: UUID,
        action: str,
        actor_label: str,
        outcome: Outcome = "success",
        actor_type: str = "user",
        actor_id: UUID | None = None,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        entry = AuditLog(
            workspace_id=workspace_id,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_label=actor_label,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            ip=ip,
            user_agent=user_agent,
            request_id=request_id,
            before=_scrub(before),
            after=_scrub(after),
            detail=detail or {},
        )

        if session is not None:
            session.add(entry)
        else:
            async with transaction() as own_session:
                own_session.add(entry)

        log.info(
            "audit.recorded",
            action=action,
            outcome=outcome,
            actor_id=str(actor_id) if actor_id else None,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
        )


_service = AuditService()


def get_audit_service() -> AuditService:
    return _service
