from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("queue", ["parse", "chunk", "embed", "index", "maintain"])
def test_standalone_worker_resolves_all_mapped_foreign_keys(queue: str) -> None:
    environment = {
        **os.environ,
        "CAIRN_DATABASE_URL": "postgresql+asyncpg://unused:unused@localhost/unused",
        "CAIRN_REDIS_URL": "redis://localhost:6379/0",
        "CAIRN_ROLE": "worker",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "from apps.worker.main import build_worker",
                    "from cairn.core.db import Base",
                    f"build_worker({queue!r})",
                    "for table in Base.metadata.tables.values():",
                    "    for foreign_key in table.foreign_keys:",
                    "        assert foreign_key.column is not None",
                ]
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
