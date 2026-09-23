"""Durable control-plane state for the isolated MCP supervisor."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cairn.core.config import get_settings
from cairn.core.db import session_scope, transaction
from cairn.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from cairn.core.mcp_management import (
    ManagedMCPConfig,
    MCPAction,
    MCPConfigUpdate,
    MCPLogs,
    MCPLogView,
    MCPServiceView,
)
from cairn.core.time import utcnow
from cairn.platform.audit import get_audit_service
from cairn.platform.models import MCPLog, MCPService, Workspace


class Administrator(Protocol):
    @property
    def workspace_id(self) -> UUID: ...
    @property
    def id(self) -> UUID: ...
    @property
    def username(self) -> str: ...


class MCPManagement:
    async def _row(self, session: AsyncSession, workspace_id: UUID | None = None) -> MCPService:
        await session.execute(text("SELECT pg_advisory_xact_lock(73439210)"))
        row = await session.get(MCPService, 1, with_for_update=True)
        if row is None:
            owner = await session.scalar(
                select(Workspace.id).order_by(Workspace.created_at, Workspace.id).limit(1)
            )
            if owner is None:
                raise NotFound("Bootstrap the deployment before managing MCP.")
            settings = get_settings().mcp
            config = ManagedMCPConfig(
                port=settings.port_min,
                allowed_hosts=settings.allowed_hosts,
                allowed_origins=settings.allowed_origins,
                request_timeout_s=settings.request_timeout_s,
                max_request_bytes=settings.max_request_bytes,
            )
            row = MCPService(
                id=1,
                workspace_id=owner,
                config=config.model_dump(),
                generation=1,
                observed_generation=0,
                desired_state="running",
                state="unknown",
            )
            session.add(row)
            await session.flush()
        if workspace_id is not None and workspace_id != row.workspace_id:
            raise PermissionDenied("Only the deployment's owning workspace may manage MCP.")
        return row

    def _view(self, row: MCPService) -> MCPServiceView:
        cfg = get_settings().mcp
        stale = row.heartbeat_at is None or (utcnow() - row.heartbeat_at).total_seconds() > 10
        return MCPServiceView.model_validate(
            {
                "config": row.config,
                "generation": row.generation,
                "observed_generation": row.observed_generation,
                "desired_state": row.desired_state,
                "state": "unknown" if stale else row.state,
                "effective_port": None if stale else row.effective_port,
                "started_at": row.started_at,
                "heartbeat_at": row.heartbeat_at,
                "last_error": row.last_error,
                "allowed_ports": list(range(cfg.port_min, cfg.port_max + 1)),
                "managed": cfg.managed,
            }
        )

    async def get(self, workspace_id: UUID | None = None) -> MCPServiceView:
        async with transaction() as session:
            return self._view(await self._row(session, workspace_id))

    async def configure(self, actor: Administrator, body: MCPConfigUpdate) -> MCPServiceView:
        return await self._change(actor, body.expected_generation, config=body.config)

    async def action(self, actor: Administrator, body: MCPAction) -> MCPServiceView:
        return await self._change(actor, body.expected_generation, action=body.action)

    async def _change(
        self,
        actor: Administrator,
        generation: int,
        *,
        config: ManagedMCPConfig | None = None,
        action: str | None = None,
    ) -> MCPServiceView:
        workspace_id: UUID = actor.workspace_id
        if not get_settings().mcp.managed:
            raise Conflict("Managed MCP is not enabled by this deployment.")
        async with transaction() as session:
            row = await self._row(session, workspace_id)
            view = self._view(row)
            if row.generation != generation or (
                row.generation != row.observed_generation and view.state != "unknown"
            ):
                raise Conflict(
                    "The MCP operation is pending or its generation changed. Refresh first."
                )
            if config is not None:
                if config.port not in view.allowed_ports:
                    raise ValidationFailed(
                        "The selected port is outside the deployment's published range."
                    )
                row.config = config.model_dump()
            if action is not None:
                row.desired_state = "stopped" if action == "stop" else "running"
            row.generation += 1
            await get_audit_service().record(
                session,
                workspace_id=workspace_id,
                actor_id=actor.id,
                actor_label=actor.username,
                action="mcp.configure" if config else f"mcp.{action}",
                resource_type="mcp_service",
                after={
                    "generation": row.generation,
                    "port": row.config["port"],
                    "desired_state": row.desired_state,
                },
            )
            return self._view(row)

    async def boot(self) -> None:
        async with transaction() as session:
            row = await self._row(session)
            if not row.config.get("auto_start", True) and row.desired_state != "stopped":
                row.desired_state = "stopped"
                row.generation += 1
            row.state = "unknown"
            row.heartbeat_at = None

    async def observe(
        self,
        generation: int,
        *,
        state: str,
        effective_port: int | None,
        started_at: datetime | None = None,
        last_error: str | None = None,
        heartbeat_at: datetime | None = None,
        acknowledged: bool = True,
    ) -> bool:
        async with transaction() as session:
            row = await self._row(session)
            if row.generation != generation:
                return False
            row.state = state
            row.effective_port = effective_port
            row.started_at = started_at
            row.last_error = last_error
            row.heartbeat_at = heartbeat_at or utcnow()
            if acknowledged:
                row.observed_generation = generation
            return True

    async def append_log(
        self,
        *,
        event: str,
        level: str = "INFO",
        request_id: str | None = None,
        status: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        messages = {
            "discovery": "MCP tools/list completed.",
            "tool_error": "MCP tools/call returned a tool error.",
            "tool_success": "MCP tools/call completed successfully.",
            "started": "MCP listener started.",
            "stopped": "MCP listener stopped.",
            "start_failed": "MCP listener failed to start; check port availability and deployment.",
            "request": "MCP request completed.",
            "unavailable": "MCP supervisor dependency unavailable.",
            "crashed": "MCP listener exited unexpectedly.",
            "reconfigure": "Applying MCP configuration.",
        }
        if event not in messages or level not in {"INFO", "WARNING", "ERROR"}:
            raise ValueError("Unknown MCP log event")
        async with transaction() as session:
            row = await self._row(session)
            session.add(
                MCPLog(
                    workspace_id=row.workspace_id,
                    level=level,
                    event=event,
                    detail=messages[event],
                    request_id=request_id,
                    status=status,
                    duration_ms=duration_ms,
                )
            )
            await session.flush()
            cutoff = await session.scalar(
                select(MCPLog.id).order_by(MCPLog.id.desc()).offset(1999).limit(1)
            )
            if cutoff is not None:
                await session.execute(delete(MCPLog).where(MCPLog.id < cutoff))

    async def logs(
        self,
        workspace_id: UUID,
        *,
        after_id: int = 0,
        limit: int = 100,
        level: str | None = None,
        since: datetime | None = None,
    ) -> MCPLogs:
        await self.get(workspace_id)
        async with session_scope() as session:
            stmt = select(MCPLog).where(MCPLog.workspace_id == workspace_id, MCPLog.id > after_id)
            if level:
                stmt = stmt.where(MCPLog.level == level)
            if since:
                stmt = stmt.where(MCPLog.at >= since)
            rows = (
                await session.scalars(stmt.order_by(MCPLog.id.desc()).limit(min(limit, 1000)))
            ).all()
            return MCPLogs(
                items=[
                    MCPLogView(
                        id=row.id,
                        at=row.at,
                        level=row.level,
                        event=row.event,
                        detail=row.detail,
                        request_id=row.request_id,
                        status=row.status,
                        duration_ms=row.duration_ms,
                    )
                    for row in reversed(rows)
                ]
            )
