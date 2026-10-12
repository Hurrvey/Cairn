"""Inspect local Tesseract identity without importing a model or making network calls."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path


def inspect_ocr(languages: tuple[str, ...]) -> dict[str, object]:
    if not languages or any(not re.fullmatch(r"[a-zA-Z0-9_]+", value) for value in languages):
        raise ValueError("invalid OCR languages")
    command = shutil.which("tesseract")
    if command is None:
        raise ValueError("OCR binary unavailable")
    version = subprocess.run([command, "--version"], capture_output=True, timeout=5, check=True)  # noqa: S603
    listing = subprocess.run([command, "--list-langs"], capture_output=True, timeout=5, check=True)  # noqa: S603
    match = re.search(
        r'List of available languages in "([^"]+)"',
        (listing.stdout + listing.stderr).decode(errors="replace"),
    )
    if not match:
        raise ValueError("OCR data directory unavailable")
    root = Path(match[1])
    hashes = {}
    for language in languages:
        with (root / f"{language}.traineddata").open("rb") as stream:
            hashes[language] = hashlib.file_digest(stream, "sha256").hexdigest()
    with Path(command).open("rb") as stream:
        binary = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "binary_sha256": binary,
        "version": version.stdout.decode(errors="replace").splitlines()[0],
        "traineddata": hashes,
        "languages": list(languages),
    }


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(inspect_ocr(tuple(json.loads(sys.argv[1])))))
