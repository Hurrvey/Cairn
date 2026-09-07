"""Integration fixtures — real PostgreSQL, real Redis, real migrations.

The schema is created by running the actual Alembic migration rather than
``metadata.create_all``: the partitioned ``audit_log`` and the case-insensitive
username index exist only in the migration, so ``create_all`` would test a
schema that no deployment ever has.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def _environment(postgres_url: str, redis_url: str) -> Iterator[None]:
    os.environ["CAIRN_DATABASE_URL"] = postgres_url
    os.environ["CAIRN_REDIS_URL"] = redis_url
    os.environ["CAIRN_ROLE"] = "all"

    from cairn.core.config import get_settings

    get_settings.cache_clear()
    yield


@pytest.fixture(scope="session", autouse=True)
def _migrated(_environment: None) -> Iterator[None]:
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        os.environ["CAIRN_DATABASE_URL"].replace("+asyncpg", "").replace("%", "%%"),
    )
    command.upgrade(config, "head")
    yield


@pytest.fixture(autouse=True)
async def clean_database(_migrated: None) -> AsyncIterator[None]:
    """Empty every table before each test.

    TRUNCATE rather than a rolled-back transaction: the code under test manages
    its own transactions (and asserts on what committed), so wrapping tests in
    an outer transaction would change the behaviour being verified.
    """
    from cairn.catalog.service import reset_catalog_service
    from cairn.core.db import get_sessionmaker
    from cairn.identity.service import reset_identity_service
    from cairn.modelgw.catalog import reset_model_catalog

    maker = get_sessionmaker()
    async with maker() as session:
        # `chunk` is listed explicitly: it deliberately carries no foreign key
        # to `knowledge_base` (see 0006_catalog), so CASCADE does not reach it
        # and rows would survive into the next test.
        await session.execute(
            text('TRUNCATE chunk, audit_log, session, "user", workspace RESTART IDENTITY CASCADE')
        )
        await session.commit()

    reset_identity_service()
    reset_model_catalog()
    reset_catalog_service()
    yield


@pytest.fixture(scope="session", autouse=True)
async def _dispose() -> AsyncIterator[None]:
    yield
    from cairn.core.cache import close_cache
    from cairn.core.db import dispose_engine

    await close_cache()
    await dispose_engine()


@pytest.fixture
def identity():  # type: ignore[no-untyped-def]  # reason: returns IdentityService
    from cairn.identity.service import get_identity_service

    return get_identity_service()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client bound to the ASGI app, exercising the full middleware stack.

    Going through the real stack matters here: the forced-credential-change
    guard *is* middleware, so a test that called the service directly would
    verify nothing about the property that actually protects the system.
    """
    from apps.api.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", follow_redirects=False
    ) as http_client:
        yield http_client
