"""Run-bound OCR checkpoints; parser hooks never expose object-store keys."""

from __future__ import annotations

import math
from contextvars import ContextVar
from dataclasses import asdict
from typing import Protocol

from cairn.core.parse_artifact import decode_payload, encode_payload
from cairn.ingestion.ocr import OcrResult, OcrWord


class ParseHooks(Protocol):
    async def get(self, image_sha: str) -> OcrResult | None: ...
    async def put(self, image_sha: str, result: OcrResult) -> None: ...
    async def progress(self, completed: int | None, total: int | None, phase: str) -> None: ...
    async def diagnostic(self, payload: dict[str, object]) -> None: ...


current_hooks: ContextVar[ParseHooks | None] = ContextVar("parse_hooks", default=None)


def encode_ocr(result: OcrResult, *, identity: str) -> bytes:
    return encode_payload(
        {"type": "ocr", "schema": 1, "identity": identity, "result": asdict(result)}
    )


def decode_ocr(raw: bytes, *, identity: str) -> OcrResult:
    payload = decode_payload(raw)
    if (
        payload.get("type") != "ocr"
        or payload.get("schema") != 1
        or payload.get("identity") != identity
    ):
        raise ValueError("OCR checkpoint identity mismatch")
    item = payload["result"]
    width, height = item["width"], item["height"]
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or width * height > 40_000_000
    ):
        raise ValueError("invalid OCR dimensions")
    if (
        not isinstance(item["text"], str)
        or not item["text"].strip()
        or len(item["text"]) > 2_000_000
    ):
        raise ValueError("invalid OCR text")
    if len(item["words"]) > 100000:
        raise ValueError("too many OCR words")
    words = []
    total_text = 0
    for word in item["words"]:
        if not isinstance(word["text"], str):
            raise ValueError("invalid OCR word text")
        total_text += len(word["text"])
        if (
            total_text > 2_000_000
            or not isinstance(word["confidence"], (int, float))
            or not math.isfinite(word["confidence"])
        ):
            raise ValueError("invalid OCR word resource or confidence")
        bbox = tuple(word["bbox"])
        if len(bbox) != 4 or not all(math.isfinite(value) for value in bbox):
            raise ValueError("invalid OCR geometry")
        if not (0 <= bbox[0] <= bbox[2] <= width and 0 <= bbox[1] <= bbox[3] <= height):
            raise ValueError("OCR geometry exceeds image")
        words.append(OcrWord(text=word["text"], confidence=word["confidence"], bbox=bbox))
    return OcrResult(
        text=item["text"],
        words=tuple(words),
        width=width,
        height=height,
        language=item["language"],
        engine=item["engine"],
        engine_version=item["engine_version"],
    )
