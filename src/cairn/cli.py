"""Cairn command line.

``cairn migrate --and-bootstrap`` is what the one-shot ``migrate`` container
runs (NFR-D-03). Migrations and bootstrap both take a PostgreSQL advisory lock,
so running this concurrently on N replicas is safe: the losers wait, observe the
work is done, and exit cleanly.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer

from cairn.core.config import get_settings
from cairn.core.logging import configure_logging, get_logger

app = typer.Typer(
    name="cairn",
    help="Cairn — Knowledge Base Platform / RAG Infrastructure Server",
    no_args_is_help=True,
    add_completion=False,
)

log = get_logger(__name__)

_MIGRATION_LOCK_KEY = 0x0CA1_1000


def _alembic_config() -> object:
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    # Alembic runs synchronously; strip the async driver from the URL.
    settings = get_settings()
    config.set_main_option(
        "sqlalchemy.url", settings.database_url.replace("+asyncpg", "").replace("%", "%%")
    )
    return config


@app.command()
def migrate(
    and_bootstrap: bool = typer.Option(
        False, "--and-bootstrap", help="Create the initial administrator after migrating."
    ),
    revision: str = typer.Option("head", help="Target revision."),
) -> None:
    """Apply database migrations, optionally bootstrapping the admin account."""
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    from alembic import command

    log.info("migrate.starting", target=revision)
    command.upgrade(_alembic_config(), revision)  # type: ignore[arg-type]
    log.info("migrate.completed", target=revision)

    if and_bootstrap:
        asyncio.run(_bootstrap())


@app.command()
def bootstrap() -> None:
    """Create the initial administrator if none exists. Idempotent."""
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    asyncio.run(_bootstrap())


async def _bootstrap() -> None:
    from cairn.core.db import dispose_engine
    from cairn.identity.bootstrap import print_bootstrap_banner
    from cairn.identity.service import get_identity_service

    try:
        result = await get_identity_service().bootstrap()
        if result is None:
            log.info("bootstrap.noop", reason="an administrator already exists")
            return
        print_bootstrap_banner(result)
        if not result.password_printed:
            log.info(
                "bootstrap.created",
                username=result.username,
                note="password taken from CAIRN_INITIAL_ADMIN_PASSWORD; not printed",
            )
    finally:
        await dispose_engine()


@app.command()
def revision(message: str = typer.Option(..., "-m", "--message")) -> None:
    """Autogenerate a migration."""
    from alembic import command

    command.revision(_alembic_config(), message=message, autogenerate=True)  # type: ignore[arg-type]


@app.command()
def check_config() -> None:
    """Validate configuration and exit. Useful as a container pre-flight."""
    settings = get_settings()
    typer.echo(f"role         : {settings.role}")
    typer.echo(f"environment  : {settings.environment}")
    typer.echo(f"database     : {settings.database_url.split('@')[-1]}")
    typer.echo(f"redis        : {settings.redis_url.split('@')[-1]}")
    typer.echo("configuration is valid")


def main() -> None:  # pragma: no cover
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
