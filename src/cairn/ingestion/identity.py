"""Owned deployment facts for parser identity, gathered without model imports."""

from __future__ import annotations

import importlib.metadata
import subprocess

from cairn.ingestion.ir import digest


def ocr_identity(languages: tuple[str, ...]) -> str:
    from cairn.core.ocr_environment import inspect_ocr
    try:
        return digest({"engine": "tesseract", **inspect_ocr(languages)})
    except (OSError, ValueError, subprocess.SubprocessError):
        return digest({"engine": "tesseract", "available": False, "languages": languages})


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"
