"""Actual network-disabled, bounded container inference and reproducible evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path


def verify(args: argparse.Namespace) -> None:
    args.output.mkdir(parents=True, exist_ok=True)
    identity = json.loads(args.manifest.read_text(encoding="utf-8"))["identity"]
    worker = Path("src/cairn/ingestion/docling_worker.py").resolve()
    records = []
    for source in args.sources:
        config = {
            "assets": "/models",
            "manifest": "/manifest.json",
            "identity": identity,
            "ocr": args.ocr,
            "max_pages": 500,
            "max_memory_bytes": 4 * 1024**3,
            "max_output_bytes": 64 * 1024**2,
            "diagnostics": True,
        }
        from cairn.ingestion.pdf import PdfLimits
        from cairn.ingestion.pdf_budget import worker_limits

        config.update(worker_limits(PdfLimits(max_memory_bytes=4 * 1024**3, timeout_seconds=600)))
        config.update(ocr_languages=["eng", "chi_sim", "chi_tra"], ocr_psm=3)
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--memory",
            "4g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,source={args.assets.resolve()},target=/models,readonly",
            "--mount",
            f"type=bind,source={args.manifest.resolve()},target=/manifest.json,readonly",
            "--mount",
            f"type=bind,source={worker},target=/opt/docling_worker.py,readonly",
            args.image,
            json.dumps(config),
        ]
        data = source.read_bytes()
        started = time.perf_counter()
        result = subprocess.run(command, input=data, capture_output=True, timeout=650)  # noqa: S603
        (args.output / f"{source.stem}.json").write_bytes(result.stdout)
        (args.output / f"{source.stem}.log").write_bytes(result.stderr)
        records.append(
            {
                "source": str(source),
                "sha256": hashlib.sha256(data).hexdigest(),
                "exitcode": result.returncode,
                "seconds": time.perf_counter() - started,
                "network": "none",
                "memory_bytes": 4 * 1024**3,
                "cpu": 2,
                "ocr": args.ocr,
                "identity": identity,
                "output_bytes": len(result.stdout),
            }
        )
        print(json.dumps(records[-1]), flush=True)
        (args.output / "results.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--image", default="cairn-pdf-worker:2.135.0")
    cli.add_argument("--manifest", type=Path, required=True)
    cli.add_argument("--assets", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--ocr", action="store_true")
    cli.add_argument("sources", type=Path, nargs="+")
    verify(cli.parse_args())
