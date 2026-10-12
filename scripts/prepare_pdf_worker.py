"""Verify a fixed Docling asset inventory and emit a deployable identity manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def prepare(
    root: Path,
    inventory: Path,
    output: Path,
    runtime_packages: Path,
    ocr_environment: Path | None = None,
) -> None:
    trusted = json.loads(inventory.read_text(encoding="utf-8"))
    if trusted["docling"] != "2.135.0":
        raise ValueError("only the reviewed Docling version is supported")
    files = trusted["files"]
    resolved_root = root.resolve()
    for entry in files:
        path = (resolved_root / entry["path"]).resolve()
        if not path.is_relative_to(resolved_root):
            raise ValueError("asset path escapes model root")
        if path.stat().st_size != entry["bytes"]:
            raise ValueError(f"asset size mismatch: {entry['path']}")
        if hashlib.file_digest(path.open("rb"), "sha256").hexdigest() != entry["sha256"]:
            raise ValueError(f"asset hash mismatch: {entry['path']}")
    manifest = {
        "schema_version": 1,
        "docling": "2.135.0",
        "files": files,
        "models": {
            "heron": "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
            "tableformer": "fc0f2d45e2218ea24bce5045f58a389aed16dc23",
        },
        "pipeline": "standard",
        "do_cell_matching": True,
        "packages": json.loads(runtime_packages.read_text(encoding="utf-8-sig")),
        "ocr_environment": json.loads(ocr_environment.read_text(encoding="utf-8-sig"))
        if ocr_environment
        else None,
    }
    manifest["identity"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Verified {len(files)} assets; identity {manifest['identity']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-packages", type=Path, required=True)
    parser.add_argument("--ocr-environment", type=Path)
    parser.add_argument(
        "--download", action="store_true", help="Fetch reviewed revisions before SHA verification"
    )
    args = parser.parse_args()
    if args.download:
        from huggingface_hub import snapshot_download

        for name, revision in {
            "docling-layout-heron": "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
            "docling-layout-heron-onnx": "40bde044036bb181c130ddf6c51792187268748f",
            "docling-models": "fc0f2d45e2218ea24bce5045f58a389aed16dc23",
        }.items():
            snapshot_download(
                repo_id=f"docling-project/{name}",
                revision=revision,
                local_dir=args.assets / f"docling-project--{name}",
            )
    prepare(args.assets, args.inventory, args.output, args.runtime_packages, args.ocr_environment)
