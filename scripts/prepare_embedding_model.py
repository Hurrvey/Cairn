"""Download the pinned optional local embedding model and verify every asset."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

import httpx

REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
BASE_URL = f"https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/{REVISION}"
FILES = {
    "config.json": "953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41",
    "model.safetensors": "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db",
    "README.md": "dcd602d2fd35c203a247304a06fec6654a12f7941b739f9221a064fe8dc3b7f0",
    "sentence_bert_config.json": "fc1993fde0a95c24ec6c022539d41cf6e2f7c9721e5415d6fb6897472a9cd4b7",
    "special_tokens_map.json": "303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3",
    "tokenizer_config.json": "acb92769e8195aabd29b7b2137a9e6d6e25c476a4f15aa4355c233426c61576b",
    "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
    "1_Pooling/config.json": "4be450dde3b0273bb9787637cfbd28fe04a7ba6ab9d36ac48e92b11e350ffc23",
}


def prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for filename, expected in FILES.items():
        target = root / filename
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(target.suffix + ".download")
        digest = hashlib.sha256()
        try:
            with (
                httpx.stream(
                    "GET", f"{BASE_URL}/{filename}", timeout=120, follow_redirects=True
                ) as response,
                pending.open("wb") as output,
            ):
                response.raise_for_status()
                for block in response.iter_bytes(1024 * 1024):
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != expected:
                raise ValueError(f"Embedding asset checksum mismatch: {filename}")
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)
    print(f"Verified pinned MiniLM assets at {root}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/models/minilm"))
    prepare(parser.parse_args().output)
