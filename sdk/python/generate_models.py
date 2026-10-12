"""Generate or check the pinned OpenAPI-derived Pydantic models."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

GENERATOR_VERSION = "0.35.0"
RUFF_VERSION = "0.16.10"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    sdk = Path(__file__).resolve().parent
    schema = sdk.parents[1] / "apps" / "web" / "openapi.json"
    output = sdk / "src" / "cairn_client" / "models.py"
    with tempfile.TemporaryDirectory(prefix="cairn-models-") as temporary:
        target = Path(temporary) / "models.py" if args.check else output
        subprocess.run(
            [
                "uvx",
                "--from",
                f"datamodel-code-generator=={GENERATOR_VERSION}",
                "datamodel-codegen",
                "--input",
                str(schema),
                "--input-file-type",
                "openapi",
                "--output",
                str(target),
                "--output-model-type",
                "pydantic_v2.BaseModel",
                "--target-python-version",
                "3.11",
                "--use-annotated",
                "--use-standard-collections",
                "--use-union-operator",
                "--disable-timestamp",
            ],
            check=True,
        )
        subprocess.run(
            ["uvx", "--from", f"ruff=={RUFF_VERSION}", "ruff", "format", str(target)],
            check=True,
        )
        if args.check and target.read_bytes() != output.read_bytes():
            raise SystemExit("Generated models are stale; run generate_models.py")


if __name__ == "__main__":
    main()
