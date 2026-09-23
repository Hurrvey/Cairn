"""Administrator routes for MCP desired state and bounded operational logs."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response

from cairn.authz.deps import require_permission
from cairn.authz.model import Principal
from cairn.core.mcp_management import MCPAction, MCPConfigUpdate, MCPLogs, MCPServiceView
from cairn.platform.mcp import MCPManagement

router = APIRouter(prefix="/v1/mcp-service", tags=["mcp management"])
Admin = Annotated[Principal, Depends(require_permission("platform:settings"))]


@router.get("", response_model=MCPServiceView)
async def get_service(actor: Admin) -> MCPServiceView:
    return await MCPManagement().get(actor.workspace_id)


@router.put("/config", response_model=MCPServiceView)
async def configure_service(actor: Admin, body: MCPConfigUpdate) -> MCPServiceView:
    return await MCPManagement().configure(actor, body)


@router.post("/actions", response_model=MCPServiceView)
async def service_action(actor: Admin, body: MCPAction) -> MCPServiceView:
    return await MCPManagement().action(actor, body)


@router.get("/logs", response_model=MCPLogs)
async def service_logs(
    actor: Admin,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    level: Literal["INFO", "WARNING", "ERROR"] | None = None,
    since: datetime | None = None,
) -> MCPLogs:
    return await MCPManagement().logs(
        actor.workspace_id, after_id=after_id, limit=limit, level=level, since=since
    )


@router.get("/logs/download")
async def download_logs(
    actor: Admin,
    level: Literal["INFO", "WARNING", "ERROR"] | None = None,
    since: datetime | None = None,
) -> Response:
    logs = await MCPManagement().logs(actor.workspace_id, limit=1000, level=level, since=since)
    content = "\n".join(item.model_dump_json() for item in logs.items)
    return Response(
        content,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="mcp-logs.jsonl"'},
    )
