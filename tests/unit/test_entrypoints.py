from pathlib import Path

import pytest
from alembic.config import Config
from typer.testing import CliRunner

from cairn.cli import app
from cairn.core.config import get_settings


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_DATABASE_URL", "postgresql+asyncpg://cairn:pg-secret@localhost/test")
    monkeypatch.setenv("CAIRN_REDIS_URL", "redis://:redis-secret@localhost:6379/0")
    monkeypatch.setenv("CAIRN_ROLE", "all")
    get_settings.cache_clear()


def test_cli_help_lists_operational_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("migrate", "bootstrap", "revision", "check-config"):
        assert name in result.stdout


def test_configuration_check_never_prints_service_credentials() -> None:
    result = CliRunner().invoke(app, ["check-config"])
    assert result.exit_code == 0
    assert "configuration is valid" in result.stdout
    assert "localhost/test" in result.stdout
    assert "pg-secret" not in result.stdout
    assert "redis-secret" not in result.stdout


def test_migration_command_resolves_real_scripts_and_sync_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Config, str]] = []
    monkeypatch.setattr(
        "alembic.command.upgrade", lambda config, revision: calls.append((config, revision))
    )
    result = CliRunner().invoke(app, ["migrate", "--revision", "0006_catalog"])
    assert result.exit_code == 0, result.output
    config, revision = calls[0]
    assert revision == "0006_catalog"
    assert Path(config.get_main_option("script_location"), "env.py").is_file()
    assert config.get_main_option("sqlalchemy.url") == "postgresql://cairn:pg-secret@localhost/test"


@pytest.mark.parametrize("queue", ["", "not-a-queue"])
def test_worker_rejects_missing_or_unknown_queue(
    monkeypatch: pytest.MonkeyPatch, queue: str
) -> None:
    from apps.worker.main import main

    monkeypatch.setenv("CAIRN_TASKS__QUEUE", queue)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 78


def test_maintenance_worker_registers_all_maintenance_handlers() -> None:
    from apps.worker.main import build_worker
    from cairn.platform.maintenance import MAINTENANCE_KINDS

    worker = build_worker("maintain")
    assert set(worker._handlers) == {*MAINTENANCE_KINDS, "kb.reindex_fanout"}
    assert worker.queue == "maintain"


@pytest.mark.parametrize("queue", ["parse", "chunk", "embed", "index"])
def test_ingestion_workers_register_their_stage_handler(queue: str) -> None:
    """T-M07-10: deployed worker entrypoints register the implemented ingestion stages."""
    from apps.worker.main import build_worker

    worker = build_worker(queue)
    assert worker.queue == queue
    expected = {f"document.{queue}"}
    if queue == "embed":
        expected.add("chunk.reembed")
    if queue == "index":
        expected.add("document.reindex_delete")
    assert set(worker._handlers) == expected
    assert worker._handlers[f"document.{queue}"].__name__ == f"handle_{queue}"
