"""Create a local Compose configuration without overwriting an existing environment."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
from pathlib import Path


def initialize(path: Path) -> None:
    values = {
        "CAIRN_MASTER_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "POSTGRES_USER": "cairn",
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "POSTGRES_DB": "cairn",
        "CAIRN_REDIS_URL": "redis://redis:6379/0",
        "CAIRN_ROLE": "all",
        "CAIRN_ENVIRONMENT": "dev",
        "CAIRN_AUTH__COOKIE_SECURE": "false",
        "CAIRN_HTTP_BIND": "127.0.0.1",
        "CAIRN_HTTP_PORT": "8080",
        "CAIRN_MODEL_DIR": "./data/models/minilm",
    }
    values["CAIRN_DATABASE_URL"] = (
        f"postgresql+asyncpg://cairn:{values['POSTGRES_PASSWORD']}@postgres:5432/cairn"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    print(f"Created {path}; secrets are not printed. Keep this file private and backed up.")
    print("Local HTTP cookies are for loopback development; use HTTPS for remote access.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    arguments = parser.parse_args()
    try:
        initialize(arguments.output)
    except FileExistsError:
        parser.exit(1, f"Refusing to overwrite existing configuration: {arguments.output}\n")
