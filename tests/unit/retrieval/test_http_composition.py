from __future__ import annotations

import os
import subprocess
import sys

import httpx
import pytest

os.environ.setdefault("CAIRN_DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("CAIRN_REDIS_URL", "redis://localhost/0")
os.environ.setdefault("CAIRN_MASTER_KEY", "unit-test-key")

from apps.api.main import create_app
from cairn.core.config import Settings


def settings(role: str) -> Settings:
    return Settings(
        role=role,
        database_url="postgresql+asyncpg://x:x@localhost/x",
        redis_url="redis://localhost/0",
        master_key="unit-test-key",
        telemetry={"metrics_enabled": False},
    )


@pytest.mark.anyio
@pytest.mark.parametrize("role,expected", [("control", 404), ("data", 401), ("all", 401)])
async def test_retrieval_route_mounts_only_for_data_roles(role: str, expected: int) -> None:
    app = create_app(settings(role))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/retrieval/query",
            json={
                "targets": [{"knowledge_base_id": "kb_00000000000000000000000000"}],
                "query": "Saturn",
            },
        )

    assert response.status_code == expected


@pytest.mark.anyio
@pytest.mark.parametrize("role,available", [("control", False), ("data", True), ("all", True)])
async def test_meta_reports_only_mounted_retrieval_capabilities(role: str, available: bool) -> None:
    app = create_app(settings(role))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/v1/meta")

    assert response.status_code == 200
    capabilities = response.json()["capabilities"]
    assert capabilities["retrieval_query"] is available
    assert capabilities["hybrid_search"] is available
    assert capabilities["rerank"] is False
    assert capabilities["weighted_fusion"] is available
    assert capabilities["parent_expansion"] is available
    assert capabilities["filters"] is available
    assert capabilities["explain"] is available
    assert capabilities["batch"] is available


def test_data_role_import_does_not_initialize_control_plane_modules() -> None:
    environment = dict(os.environ)
    environment["CAIRN_ROLE"] = "data"
    command = (
        "import sys; import apps.api.main; "
        "blocked=('cairn.identity','cairn.catalog','cairn.platform','cairn.modelgw'); "
        "loaded=sorted(name for name in sys.modules if name.startswith(blocked)); "
        "assert not loaded, loaded"
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", command],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
