"""Ingestion compatibility exports for the shared supervised process primitive."""

from cairn.core.native_process import (
    ProcessOutputTooLarge,
    ProcessSupervisionUnavailable,
    ProcessTimedOut,
    run_bounded,
)

__all__ = [
    "ProcessOutputTooLarge",
    "ProcessSupervisionUnavailable",
    "ProcessTimedOut",
    "run_bounded",
]
