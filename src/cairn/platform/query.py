"""Audit querying and export (FR-O-02).

There is deliberately no update or delete path anywhere in this module. The
audit log is append-only not by policy but by the absence of a method to call.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from cairn.core.db import session_scope
from cairn.core.pagination import CursorPage, encode_cursor
from cairn.core.time import isoformat

__all__ = ["AuditEntry", "AuditFilters", "AuditQuery"]


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    at: datetime
    actor_type: str
    actor_id: UUID | None
    actor_label: str
    action: str
    resource_type: str | None
    resource_id: UUID | None
    outcome: str
    ip: str | None
    request_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    detail: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": isoformat(self.at),
            "actor_type": self.actor_type,
            "actor_id": str(self.actor_id) if self.actor_id else None,
            "actor_label": self.actor_label,
            "action": self.action,
            "resource_type": self.resource_type,
            "resource_id": str(self.resource_id) if self.resource_id else None,
            "outcome": self.outcome,
            "ip": self.ip,
            "request_id": self.request_id,
            "before": self.before,
            "after": self.after,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class AuditFilters:
    actor_id: UUID | None = None
    action: str | None = None
    action_prefix: str | None = None
    resource_type: str | None = None
    resource_id: UUID | None = None
    outcome: str | None = None
    since: datetime | None = None
    until: datetime | None = None


_COLUMNS = (
    "id, at, actor_type, actor_id, actor_label, action, resource_type, "
    "resource_id, outcome, host(ip) AS ip, request_id, before, after, detail"
)


def _where(filters: AuditFilters) -> tuple[str, dict[str, Any]]:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {}
    if filters.actor_id:
        clauses.append("actor_id = :actor_id")
        params["actor_id"] = filters.actor_id
    if filters.action:
        clauses.append("action = :action")
        params["action"] = filters.action
    if filters.action_prefix:
        clauses.append("action LIKE :action_prefix")
        params["action_prefix"] = f"{filters.action_prefix}%"
    if filters.resource_type:
        clauses.append("resource_type = :resource_type")
        params["resource_type"] = filters.resource_type
    if filters.resource_id:
        clauses.append("resource_id = :resource_id")
        params["resource_id"] = filters.resource_id
    if filters.outcome:
        clauses.append("outcome = :outcome")
        params["outcome"] = filters.outcome
    if filters.since:
        clauses.append("at >= :since")
        params["since"] = filters.since
    if filters.until:
        clauses.append("at < :until")
        params["until"] = filters.until
    return " AND ".join(clauses), params


class AuditQuery:
    async def page(
        self,
        workspace_id: UUID,
        filters: AuditFilters,
        *,
        limit: int = 50,
        cursor_at: datetime | None = None,
        cursor_id: int | None = None,
    ) -> CursorPage[AuditEntry]:
        where, params = _where(filters)
        params["workspace_id"] = workspace_id
        params["limit"] = limit + 1  # one extra row answers has_more without a COUNT

        keyset = ""
        if cursor_at is not None and cursor_id is not None:
            # Keyset on the full sort key. Sorting by `at` alone would skip or
            # repeat rows whenever two entries share a timestamp — which they do,
            # constantly, because a single request writes several.
            keyset = " AND (at, id) < (:cursor_at, :cursor_id)"
            params["cursor_at"] = cursor_at
            params["cursor_id"] = cursor_id

        async with session_scope() as session:
            result = await session.execute(
                # `where`/`keyset` are assembled from a fixed clause vocabulary
                # in `_where`. Every user-supplied value is a bound parameter.
                text(
                    f"SELECT {_COLUMNS} FROM audit_log WHERE {where}{keyset} "
                    "ORDER BY at DESC, id DESC LIMIT :limit"
                ),
                params,
            )
            rows = [_to_entry(row) for row in result.mappings().all()]

        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = (
            encode_cursor(at=isoformat(items[-1].at), id=items[-1].id)
            if has_more and items
            else None
        )
        return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)

    async def stream(
        self, workspace_id: UUID, filters: AuditFilters, *, batch: int = 1000
    ) -> AsyncIterator[AuditEntry]:
        """Stream every match for export.

        Batched keyset iteration rather than one large result set: an export
        covering a year must not materialise the whole thing in the API process.
        """
        where, params = _where(filters)
        params["workspace_id"] = workspace_id
        params["limit"] = batch
        cursor_at: datetime | None = None
        cursor_id: int | None = None

        while True:
            keyset = ""
            if cursor_at is not None:
                keyset = " AND (at, id) < (:cursor_at, :cursor_id)"
                params["cursor_at"] = cursor_at
                params["cursor_id"] = cursor_id

            async with session_scope() as session:
                result = await session.execute(
                    text(
                        f"SELECT {_COLUMNS} FROM audit_log WHERE {where}{keyset} "
                        "ORDER BY at DESC, id DESC LIMIT :limit"
                    ),
                    params,
                )
                rows = [_to_entry(row) for row in result.mappings().all()]

            if not rows:
                return
            for entry in rows:
                yield entry
            cursor_at, cursor_id = rows[-1].at, rows[-1].id
            if len(rows) < batch:
                return


def _to_entry(row: Any) -> AuditEntry:
    return AuditEntry(
        id=int(row["id"]),
        at=row["at"],
        actor_type=row["actor_type"],
        actor_id=row["actor_id"],
        actor_label=row["actor_label"],
        action=row["action"],
        resource_type=row["resource_type"],
        resource_id=row["resource_id"],
        outcome=row["outcome"],
        ip=row["ip"],
        request_id=row["request_id"],
        before=row["before"],
        after=row["after"],
        detail=row["detail"] or {},
    )
