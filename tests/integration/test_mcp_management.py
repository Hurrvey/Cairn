from datetime import timedelta

import httpx
import pytest
from tests.integration.test_ingestion_pipeline import pipeline_admin

from cairn.authz.model import Principal
from cairn.core.errors import Conflict, ValidationFailed
from cairn.core.mcp_management import ManagedMCPConfig, MCPAction, MCPConfigUpdate
from cairn.core.time import utcnow

__all__ = ["pipeline_admin"]


async def test_management_persists_and_fences_operations(
    pipeline_admin: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cairn.core.config import get_settings
    from cairn.platform.mcp import MCPManagement

    monkeypatch.setattr(get_settings().mcp, "managed", True)
    service = MCPManagement()
    view = await service.get(pipeline_admin.workspace_id)
    assert view.state == "unknown"
    await service.observe(
        view.generation, state="running", effective_port=8081, started_at=utcnow()
    )
    stopped = await service.action(
        pipeline_admin, MCPAction(expected_generation=view.generation, action="stop")
    )
    assert stopped.desired_state == "stopped"
    with pytest.raises(Conflict):
        await service.action(
            pipeline_admin, MCPAction(expected_generation=view.generation, action="start")
        )
    assert not await service.observe(view.generation, state="running", effective_port=8081)
    await service.observe(stopped.generation, state="stopped", effective_port=None)
    current = await service.get(pipeline_admin.workspace_id)
    assert current.state == "stopped" and current.observed_generation == stopped.generation
    with pytest.raises(ValidationFailed):
        await service.configure(
            pipeline_admin,
            MCPConfigUpdate(
                expected_generation=current.generation, config=ManagedMCPConfig(port=9000)
            ),
        )
    await service.observe(
        current.generation,
        state="stopped",
        effective_port=None,
        heartbeat_at=utcnow() - timedelta(seconds=30),
    )
    assert (await service.get(pipeline_admin.workspace_id)).state == "unknown"


async def test_management_api_requires_admin(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/mcp-service")).status_code == 401


async def test_management_cookie_csrf_and_machine_key_boundaries(
    pipeline_admin: Principal, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_ingestion_pipeline import STRONG_PASSWORD

    from cairn.authz.service import ApiKeySpec, AuthzService
    from cairn.core.config import get_settings
    from cairn.platform.mcp import MCPManagement

    monkeypatch.setattr(get_settings().mcp, "managed", True)
    await client.post("/v1/auth/login", json={"username": "admin", "password": STRONG_PASSWORD})
    view = (await client.get("/v1/mcp-service")).json()
    await MCPManagement().observe(view["generation"], state="running", effective_port=8081)
    body = {"expected_generation": view["generation"], "action": "stop"}
    assert (await client.post("/v1/mcp-service/actions", json=body)).status_code == 403
    headers = {"X-CSRF-Token": client.cookies["cairn_csrf"]}
    response = await client.post("/v1/mcp-service/actions", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["desired_state"] == "stopped"
    _key, raw = await AuthzService().create_api_key(
        pipeline_admin, ApiKeySpec(name="no-management", scopes=["kb:query"])
    )
    assert (
        await client.get("/v1/mcp-service", headers={"Authorization": f"Bearer {raw}"})
    ).status_code == 403
    logs = await client.get("/v1/mcp-service/logs/download")
    assert logs.status_code == 200
    assert "attachment" in logs.headers["content-disposition"]


async def test_foreign_workspace_and_autostart_persistence(
    pipeline_admin: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from uuid import uuid4

    from cairn.core.config import get_settings
    from cairn.core.errors import PermissionDenied
    from cairn.platform.mcp import MCPManagement

    monkeypatch.setattr(get_settings().mcp, "managed", True)
    store = MCPManagement()
    current = await store.get(pipeline_admin.workspace_id)
    with pytest.raises(PermissionDenied):
        await store.get(uuid4())
    await store.observe(current.generation, state="running", effective_port=8081)
    changed = await store.configure(
        pipeline_admin,
        MCPConfigUpdate(
            expected_generation=current.generation, config=ManagedMCPConfig(auto_start=False)
        ),
    )
    await store.observe(changed.generation, state="running", effective_port=8081)
    await store.boot()
    assert (await store.get()).desired_state == "stopped"


async def test_logs_are_bounded_filtered_and_not_caller_messages(pipeline_admin: Principal) -> None:
    from cairn.core.db import transaction
    from cairn.platform.mcp import MCPManagement
    from cairn.platform.models import MCPLog

    service = MCPManagement()
    await service.get(pipeline_admin.workspace_id)
    async with transaction() as session:
        session.add_all(
            [
                MCPLog(
                    workspace_id=pipeline_admin.workspace_id,
                    level="INFO",
                    event="request",
                    detail="MCP request completed.",
                )
                for _index in range(2001)
            ]
        )
    await service.append_log(event="start_failed", level="ERROR")
    logs = await service.logs(pipeline_admin.workspace_id, limit=1000, level="ERROR")
    assert len(logs.items) == 1 and logs.items[0].event == "start_failed"
    with pytest.raises(ValueError):
        await service.append_log(event="secret token")
    from sqlalchemy import func, select

    from cairn.core.db import session_scope

    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(MCPLog)) == 2000
