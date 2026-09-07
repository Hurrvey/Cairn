"""Shared test configuration.

Two modes:

* **External services** (CI) — ``CAIRN_TEST_USE_EXTERNAL_SERVICES=1`` uses the
  Postgres and Redis already running as job services.
* **testcontainers** (local) — spins them up on demand.

Either way the tests run against a *real* database. Mocking the database tests
the mock (see test strategy §1).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

os.environ.setdefault("CAIRN_LOG_FORMAT", "console")
os.environ.setdefault("CAIRN_LOG_LEVEL", "WARNING")
os.environ.setdefault("CAIRN_ENVIRONMENT", "dev")
os.environ.setdefault("CAIRN_MASTER_KEY", "dGVzdC1tYXN0ZXIta2V5LWZvci1jaS1vbmx5LTMyYnl0ZXM=")
# Argon2 at the production cost makes a 20-test auth suite take minutes.
# Lowered here only; the production floor is enforced by the Settings model.
os.environ.setdefault("CAIRN_AUTH__ARGON2_MEMORY_KIB", "65536")
os.environ.setdefault("CAIRN_AUTH__COOKIE_SECURE", "false")


def _use_external() -> bool:
    return os.environ.get("CAIRN_TEST_USE_EXTERNAL_SERVICES") == "1"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A live PostgreSQL, as an asyncpg URL."""
    if _use_external():
        yield os.environ.get(
            "CAIRN_DATABASE_URL",
            "postgresql+asyncpg://cairn:cairn@localhost:5432/cairn_test",
        )
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers is not installed and no external services configured")

    with PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    if _use_external():
        yield os.environ.get("CAIRN_REDIS_URL", "redis://localhost:6379/0")
        return

    try:
        from testcontainers.redis import RedisContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers is not installed and no external services configured")

    with RedisContainer("redis:7.4-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Drop cached settings, engine, and service instances between tests.

    Module-level singletons are the right production shape and the wrong test
    shape; this fixture reconciles them rather than pushing DI plumbing into
    every call site.
    """
    yield
    from cairn.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(items: list[Any]) -> None:
    """Mark everything under tests/integration so `-m "not integration"` works."""
    for item in items:
        path = str(item.fspath).replace("\\", "/")
        if "/integration/" in path:
            item.add_marker(pytest.mark.integration)
        elif "/contract/" in path:
            item.add_marker(pytest.mark.contract)


@pytest.fixture
async def _noop() -> AsyncIterator[None]:  # pragma: no cover — placeholder
    yield None
