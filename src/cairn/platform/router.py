"""Audit log and settings endpoints (FR-O-02, FR-O-06)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from cairn.authz.deps import require_permission
from cairn.authz.model import Principal
from cairn.core.ids import decode_id
from cairn.core.pagination import CursorPage, decode_cursor
from cairn.core.time import parse_iso
from cairn.platform.query import AuditFilters, AuditQuery
from cairn.platform.settings import (
    HOT_RELOADABLE,
    SettingsService,
    WorkspaceSettings,
    get_settings_service,
)

__all__ = ["router"]

router = APIRouter(prefix="/v1", tags=["platform"])
PROBLEM: dict[int | str, dict[str, Any]] = {403: {"content": {"application/problem+json": {}}}}


class AuditEntryResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class SettingsUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any]


class SettingsResponse(BaseModel):
    settings: WorkspaceSettings
    #: Which of the submitted keys need a restart. Surfaced so the UI never
    #: implies a change took effect when it has not.
    requires_restart: list[str] = []
    hot_reloadable: list[str] = sorted(HOT_RELOADABLE)


def _query() -> AuditQuery:
    return AuditQuery()


def _settings() -> SettingsService:
    return get_settings_service()


@router.get(
    "/audit-log",
    responses=PROBLEM,
    summary="Query the audit log",
    description="Append-only. Filterable by actor, action, resource, outcome, and time range.",
)
async def query_audit_log(
    actor: Annotated[Principal, Depends(require_permission("platform:audit"))],
    query: Annotated[AuditQuery, Depends(_query)],
    actor_id: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    action_prefix: Annotated[str | None, Query(description="e.g. `break_glass.`")] = None,
    resource_type: Annotated[str | None, Query()] = None,
    outcome: Annotated[str | None, Query(pattern="^(success|failure|denied)$")] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    filters = AuditFilters(
        actor_id=decode_id("usr", actor_id) if actor_id else None,
        action=action,
        action_prefix=action_prefix,
        resource_type=resource_type,
        outcome=outcome,
        since=since,
        until=until,
    )
    cursor_at, cursor_id = None, None
    if cursor:
        keys = decode_cursor(cursor)
        cursor_at = parse_iso(str(keys["at"]))
        cursor_id = int(keys["id"])

    page: CursorPage[Any] = await query.page(
        actor.workspace_id, filters, limit=limit, cursor_at=cursor_at, cursor_id=cursor_id
    )
    return {
        "items": [entry.to_json() for entry in page.items],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


@router.get(
    "/audit-log/export",
    responses=PROBLEM,
    summary="Export the audit log as JSONL",
    description=(
        "Streamed in batches. An export covering a year must not materialise in the API process."
    ),
)
async def export_audit_log(
    actor: Annotated[Principal, Depends(require_permission("platform:audit"))],
    query: Annotated[AuditQuery, Depends(_query)],
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> StreamingResponse:
    filters = AuditFilters(since=since, until=until)

    async def lines() -> AsyncIterator[bytes]:
        async for entry in query.stream(actor.workspace_id, filters):
            yield (json.dumps(entry.to_json(), separators=(",", ":")) + "\n").encode()

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="cairn-audit.jsonl"'},
    )


@router.get(
    "/settings",
    response_model=SettingsResponse,
    responses=PROBLEM,
    summary="Workspace settings",
)
async def get_workspace_settings(
    actor: Annotated[Principal, Depends(require_permission("platform:settings"))],
    service: Annotated[SettingsService, Depends(_settings)],
) -> SettingsResponse:
    return SettingsResponse(settings=await service.get(actor.workspace_id))


@router.patch(
    "/settings",
    response_model=SettingsResponse,
    responses=PROBLEM,
    summary="Update workspace settings",
)
async def update_workspace_settings(
    body: SettingsUpdateRequest,
    actor: Annotated[Principal, Depends(require_permission("platform:settings"))],
    service: Annotated[SettingsService, Depends(_settings)],
) -> SettingsResponse:
    from cairn.platform.audit import get_audit_service

    before = await service.get(actor.workspace_id)
    updated = await service.update(actor.workspace_id, body.settings)
    await get_audit_service().record(
        workspace_id=actor.workspace_id,
        action="settings.update",
        actor_id=actor.id,
        actor_label=actor.username,
        before=before.model_dump(),
        after=updated.model_dump(),
    )
    return SettingsResponse(
        settings=updated,
        requires_restart=sorted(set(body.settings) - HOT_RELOADABLE),
    )


@router.get(
    "/tasks/queues",
    responses=PROBLEM,
    summary="Queue depth and starvation age",
    description=(
        "`oldest_ready_age_seconds` is the signal that matters: depth alone can "
        "look healthy while one task sits behind a saturated workspace."
    ),
)
async def queue_stats(
    _: Annotated[Principal, Depends(require_permission("platform:audit"))],
) -> dict[str, Any]:
    from cairn.tasks.service import get_task_service

    service = get_task_service()
    depth = await service.queue_depth()
    ages = await service.oldest_ready_age()
    return {
        "queues": [
            {
                "queue": queue,
                "ready": count,
                "oldest_ready_age_seconds": round(ages.get(queue, 0.0), 1),
            }
            for queue, count in sorted(depth.items())
        ]
    }
