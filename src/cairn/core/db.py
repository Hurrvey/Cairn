"""Database engine, session, transaction, and advisory locks.

Rules that apply everywhere (see cross-cutting conventions §7):

* Services own their transactions via ``transaction()``.
* Repositories return ORM objects; services return DTOs. ORM objects never cross
  a module boundary.
* Every query filters on ``workspace_id``.
* The data plane opens no write transaction, and on the happy path no
  connection at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from cairn.core.config import Settings, get_settings

__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "advisory_xact_lock",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
    "transaction",
]

#: Deterministic constraint names, so Alembic autogenerate produces stable
#: migrations instead of database-assigned names that differ per environment.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every module.

    Shared *metadata* is not shared *ownership*: each module still owns its own
    tables exclusively, and no module may import another's ``models`` (enforced
    by the ``no_cross_orm`` import contract).
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        cfg = settings or get_settings()
        connect_args: dict[str, Any] = {
            "server_settings": {
                "application_name": f"cairn-{cfg.role}",
                "statement_timeout": str(cfg.db.statement_timeout_ms),
            }
        }
        _engine = create_async_engine(
            cfg.database_url,
            pool_size=cfg.db.pool_size,
            max_overflow=cfg.db.max_overflow,
            pool_pre_ping=cfg.db.pool_pre_ping,
            echo=cfg.db.echo,
            connect_args=connect_args,
        )
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def dispose_engine() -> None:
    """Close the pool. Called on shutdown, and between tests."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """A unit of work: commits on clean exit, rolls back on any exception.

    Phase 0 does not support nesting — a nested call opens an independent
    transaction. Savepoint-based nesting lands with M06 when task enqueue needs
    to join a caller's transaction.
    """
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A read-only session with no implicit transaction management."""
    maker = get_sessionmaker()
    async with maker() as session:
        yield session


async def advisory_xact_lock(session: AsyncSession, key: int) -> None:
    """Take a transaction-scoped advisory lock; released automatically at commit.

    Used by bootstrap (FR-A-02) and migrations (NFR-D-03) so that N replicas
    starting simultaneously serialise instead of racing.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
