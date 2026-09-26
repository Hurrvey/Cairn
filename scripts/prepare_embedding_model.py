"""Download pinned local model assets and verify every file.

    uv run python scripts/prepare_embedding_model.py            # MiniLM + bge-m3 tokenizer
    uv run python scripts/prepare_embedding_model.py --model bge-m3   # bge-m3 weights (2.3 GB)

The bge-m3 tokenizer is always prepared (17 MB): Cairn counts tokens with it for
bge-m3 and uses it as the budget estimate for hosted models whose tokenizers are
not published, so model registration must be able to reference it even where
the sidecar and its weights are not deployed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

import httpx

MODELS: dict[str, dict[str, object]] = {
    "minilm": {
        "repo": "sentence-transformers/all-MiniLM-L6-v2",
        "revision": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        "output": Path("data/models/minilm"),
        "files": {
            "config.json": "953f9c0d463486b10a6871cc2fd59f223b2c70184f49815e7efbcab5d8908b41",
            "model.safetensors": "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db",
            "README.md": "dcd602d2fd35c203a247304a06fec6654a12f7941b739f9221a064fe8dc3b7f0",
            "sentence_bert_config.json": (
                "fc1993fde0a95c24ec6c022539d41cf6e2f7c9721e5415d6fb6897472a9cd4b7"
            ),
            "special_tokens_map.json": (
                "303df45a03609e4ead04bc3dc1536d0ab19b5358db685b6f3da123d05ec200e3"
            ),
            "tokenizer_config.json": (
                "acb92769e8195aabd29b7b2137a9e6d6e25c476a4f15aa4355c233426c61576b"
            ),
            "tokenizer.json": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
            "1_Pooling/config.json": (
                "4be450dde3b0273bb9787637cfbd28fe04a7ba6ab9d36ac48e92b11e350ffc23"
            ),
        },
    },
    "bge-m3": {
        "repo": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "output": Path("data/models/bge-m3"),
        "files": {
            "config.json": "26159e7ad065073448460117eb24b7a4572f6f4e78eadff65dc0a11c052449fa",
            "tokenizer_config.json": (
                "a62b2b6784f990259fddef5f16388693a8043be4f69179e6a5257eeb3f9abac4"
            ),
            "special_tokens_map.json": (
                "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835"
            ),
            "tokenizer.json": "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08",
            "sentencepiece.bpe.model": (
                "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865"
            ),
            "sparse_linear.pt": "45c93804d2142b8f6d7ec6914ae23a1eee9c6a1d27d83d908a20d2afb3595ad9",
            "colbert_linear.pt": "19bfbae397c2b7524158c919d0e9b19393c5639d098f0a66932c91ed8f5f9abb",
            "pytorch_model.bin": "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38",
        },
    },
}
#: Enough for Cairn to count bge-m3 tokens without the weights.
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(name: str, root: Path | None = None, *, only: tuple[str, ...] | None = None) -> None:
    spec = MODELS[name]
    files: dict[str, str] = spec["files"]  # type: ignore[assignment]
    target_root = root or spec["output"]  # type: ignore[assignment]
    assert isinstance(target_root, Path)
    base_url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}"
    target_root.mkdir(parents=True, exist_ok=True)
    for filename, expected in files.items():
        if only is not None and filename not in only:
            continue
        target = target_root / filename
        if target.is_file() and _sha256(target) == expected:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(target.suffix + ".download")
        digest = hashlib.sha256()
        try:
            with (
                httpx.stream(
                    "GET", f"{base_url}/{filename}", timeout=600, follow_redirects=True
                ) as response,
                pending.open("wb") as output,
            ):
                response.raise_for_status()
                for block in response.iter_bytes(1024 * 1024):
                    digest.update(block)
                    output.write(block)
            if digest.hexdigest() != expected:
                raise ValueError(f"Model asset checksum mismatch: {name}/{filename}")
            os.replace(pending, target)
        finally:
            pending.unlink(missing_ok=True)
    scope = "tokenizer" if only is not None else "assets"
    print(f"Verified pinned {name} {scope} at {target_root}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--model", choices=sorted(MODELS), default="minilm")
    parser.add_argument("--output", type=Path, default=None)
    arguments = parser.parse_args()
    prepare(arguments.model, arguments.output)
    if arguments.model != "bge-m3":
        prepare("bge-m3", only=TOKENIZER_FILES)
