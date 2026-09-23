"""Killable native-tool calls with bounded pipes and cancellation cleanup."""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
from contextlib import suppress


class ProcessTimedOut(Exception):
    """The child exceeded its wall-clock budget."""


class ProcessOutputTooLarge(Exception):
    """One child pipe exceeded its byte budget."""


async def _read(stream: asyncio.StreamReader, maximum: int) -> bytes:
    output = bytearray()
    while chunk := await stream.read(min(65536, maximum + 1)):
        if len(output) + len(chunk) > maximum:
            raise ProcessOutputTooLarge
        output.extend(chunk)
    return bytes(output)


async def _write(stream: asyncio.StreamWriter, data: bytes) -> None:
    try:
        for offset in range(0, len(data), 65536):
            stream.write(data[offset : offset + 65536])
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await stream.wait_closed()


async def _discard(stream: asyncio.StreamReader) -> None:
    while await stream.read(65536):
        pass


async def run_bounded(
    command: tuple[str, ...],
    data: bytes,
    *,
    timeout_seconds: float,
    max_output_bytes: int,
    max_stderr_bytes: int = 65536,
) -> tuple[int, bytes, bytes]:
    """Execute an operator-owned argv, never a shell or document-supplied command."""
    if not command or any(not part or "\x00" in part for part in command):
        raise ValueError("command must contain nonempty arguments")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if max_output_bytes <= 0 or max_stderr_bytes <= 0:
        raise ValueError("pipe bounds must be positive")
    # Retain only the runtime environment; OMP prevents each OCR worker from
    # oversubscribing every CPU in the host/container.
    environment = dict(os.environ)
    environment["OMP_THREAD_LIMIT"] = "1"
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    writer = asyncio.create_task(_write(process.stdin, data))
    stdout = asyncio.create_task(_read(process.stdout, max_output_bytes))
    stderr = asyncio.create_task(_read(process.stderr, max_stderr_bytes))
    waiter = asyncio.create_task(process.wait())
    tasks = (writer, stdout, stderr, waiter)
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(*tasks)
        return waiter.result(), stdout.result(), stderr.result()
    except TimeoutError as exc:
        raise ProcessTimedOut from exc
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A full StreamReader pauses its transport. Drain after killing so that
        # wait() can observe pipe EOF even when a size violation stopped a reader.
        await asyncio.gather(_discard(process.stdout), _discard(process.stderr), process.wait())
