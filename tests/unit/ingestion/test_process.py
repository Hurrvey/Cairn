from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from pathlib import Path

import pytest


async def test_process_roundtrips_and_drains_stderr() -> None:
    from cairn.ingestion.process import run_bounded

    code, stdout, stderr = await run_bounded(
        (sys.executable, "-c", "import sys; sys.stderr.write('note'); print(sys.stdin.read())"),
        b"source",
        timeout_seconds=5,
        max_output_bytes=100,
    )
    assert code == 0 and stdout.strip() == b"source" and stderr == b"note"


async def test_process_enforces_output_bound() -> None:
    from cairn.ingestion.process import ProcessOutputTooLarge, run_bounded

    with pytest.raises(ProcessOutputTooLarge):
        await run_bounded(
            (sys.executable, "-c", "print('x'*1000000)"),
            b"",
            timeout_seconds=5,
            max_output_bytes=100,
        )


async def test_process_timeout_reaps_child(tmp_path: Path) -> None:
    from cairn.ingestion.process import ProcessTimedOut, run_bounded

    marker = tmp_path / "late"
    with pytest.raises(ProcessTimedOut):
        await run_bounded(
            (
                sys.executable,
                "-c",
                "import time,pathlib,sys; time.sleep(1); pathlib.Path(sys.argv[1]).touch()",
                str(marker),
            ),
            b"",
            timeout_seconds=0.1,
            max_output_bytes=100,
        )
    await asyncio.sleep(1.1)
    assert not marker.exists()


async def test_process_cancellation_reaps_child(tmp_path: Path) -> None:
    from cairn.ingestion.process import run_bounded

    marker = tmp_path / "late"
    task = asyncio.create_task(
        run_bounded(
            (
                sys.executable,
                "-c",
                "import time,pathlib,sys; time.sleep(1); pathlib.Path(sys.argv[1]).touch()",
                str(marker),
            ),
            b"",
            timeout_seconds=10,
            max_output_bytes=100,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.1)
    assert not marker.exists()


async def test_timeout_reaps_spawned_grandchild(tmp_path: Path) -> None:
    from cairn.ingestion.process import ProcessTimedOut, run_bounded

    marker = tmp_path / "grandchild-late"
    child = "import time,pathlib,sys;time.sleep(2);pathlib.Path(sys.argv[1]).touch()"
    parent = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(10)"
    )
    with pytest.raises(ProcessTimedOut):
        await run_bounded(
            (sys.executable, "-c", parent, child, str(marker)),
            b"",
            timeout_seconds=0.4,
            max_output_bytes=100,
        )
    await asyncio.sleep(2.1)
    assert not marker.exists()


async def test_leader_exit_does_not_leave_pipe_holding_descendant(tmp_path: Path):
    import time

    from cairn.core.native_process import ProcessTimedOut, run_bounded

    marker = tmp_path / "descendant"
    child = "import time,pathlib,sys;time.sleep(2);pathlib.Path(sys.argv[1]).touch()"
    parent = "import subprocess,sys;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])"
    started = time.monotonic()
    with suppress(ProcessTimedOut):
        await run_bounded(
            (sys.executable, "-c", parent, child, str(marker)),
            b"",
            timeout_seconds=0.5,
            max_output_bytes=100,
        )
    assert time.monotonic() - started < 1.2
    await asyncio.sleep(2.1)
    assert not marker.exists()


async def test_repeated_cancel_reaps_descendants_with_bounded_cleanup(tmp_path):
    from cairn.core.native_process import run_bounded

    marker = tmp_path / "repeat-cancel"
    child = "import time,pathlib,sys;time.sleep(2);pathlib.Path(sys.argv[1]).touch()"
    parent = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(10)"
    )
    task = asyncio.create_task(
        run_bounded(
            (sys.executable, "-c", parent, child, str(marker)),
            b"",
            timeout_seconds=10,
            max_output_bytes=100,
        )
    )
    await asyncio.sleep(0.2)
    start = asyncio.get_running_loop().time()
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert asyncio.get_running_loop().time() - start < 1
    await asyncio.sleep(2.1)
    assert not marker.exists()
