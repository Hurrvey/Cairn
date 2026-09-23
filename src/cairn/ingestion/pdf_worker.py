"""Isolated pypdfium2 worker used by :mod:`cairn.ingestion.pdf`.

The protocol is intentionally private and small. PDF bytes arrive on stdin;
stdout contains either extraction JSON or one rendered PNG. Failures use a
short JSON code on stderr so native error text never crosses the adapter API.
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
from collections.abc import Mapping
from typing import Any, NoReturn

from cairn.ingestion.pdf_layout import PositionedWord, layout_page

_EXIT_ENCRYPTED = 2
_EXIT_CORRUPT = 3
_EXIT_TOO_LARGE = 4
_EXIT_INTERNAL = 5


class _BoundExceeded(Exception):
    pass


def _fail(code: str, status: int) -> NoReturn:
    sys.stderr.buffer.write(json.dumps({"code": code}, separators=(",", ":")).encode())
    raise SystemExit(status)


def _positive_number(config: Mapping[str, Any], name: str) -> float:
    value = config.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("invalid_request", _EXIT_INTERNAL)
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        _fail("invalid_request", _EXIT_INTERNAL)
    return number


def _positive_integer(config: Mapping[str, Any], name: str) -> int:
    number = _positive_number(config, name)
    if not number.is_integer():
        _fail("invalid_request", _EXIT_INTERNAL)
    return int(number)


def _set_memory_limit(maximum: int) -> None:
    """Apply a hard address-space ceiling where the platform supports it."""
    if os.name == "nt":
        return
    try:
        import resource

        _, current_hard = resource.getrlimit(resource.RLIMIT_AS)  # type: ignore[attr-defined]
        hard = maximum if current_hard < 0 else min(current_hard, maximum)
        resource.setrlimit(  # type: ignore[attr-defined]
            resource.RLIMIT_AS,  # type: ignore[attr-defined]
            (min(maximum, hard), hard),
        )
    except (ImportError, OSError, ValueError):
        # Page/character/pixel/output bounds still apply on platforms without
        # RLIMIT_AS (notably Windows and some container configurations).
        return


def _load_pdf(data: bytes) -> Any:
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
        import pypdfium2.raw as pdfium_raw  # type: ignore[import-untyped]
    except (ImportError, OSError):
        _fail("unavailable", _EXIT_INTERNAL)

    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if exc.err_code in {pdfium_raw.FPDF_ERR_PASSWORD, pdfium_raw.FPDF_ERR_SECURITY}:
            _fail("encrypted", _EXIT_ENCRYPTED)
        _fail("corrupt", _EXIT_CORRUPT)


def _extract(data: bytes, config: Mapping[str, Any]) -> bytes:
    max_pages = _positive_integer(config, "max_pages")
    max_width = _positive_number(config, "max_page_width_points")
    max_height = _positive_number(config, "max_page_height_points")
    max_page_chars = _positive_integer(config, "max_page_chars")
    max_text_chars = _positive_integer(config, "max_text_chars")
    max_page_objects = _positive_integer(config, "max_page_objects")
    max_layout_words = _positive_integer(config, "max_layout_words")
    max_json_bytes = _positive_integer(config, "max_json_bytes")

    pdf = _load_pdf(data)
    try:
        page_count = len(pdf)
        if page_count <= 0:
            _fail("corrupt", _EXIT_CORRUPT)
        if page_count > max_pages:
            raise _BoundExceeded
        total_chars = 0
        pages: list[dict[str, Any]] = []
        for page_number in range(page_count):
            page = pdf[page_number]
            try:
                left, bottom, right, top = (float(value) for value in page.get_bbox())
                width = right - left
                height = top - bottom
                rotation = page.get_rotation()
                if (
                    not all(math.isfinite(value) for value in (left, bottom, right, top))
                    or width <= 0
                    or height <= 0
                    or width > max_width
                    or height > max_height
                    or rotation not in {0, 90, 180, 270}
                ):
                    raise _BoundExceeded
                has_page_objects, has_raster_objects = _page_object_flags(
                    page, maximum=max_page_objects
                )
                text_page = page.get_textpage()
                try:
                    char_count = text_page.count_chars()
                    if char_count < 0 or char_count > max_page_chars:
                        raise _BoundExceeded
                    total_chars += char_count
                    if total_chars > max_text_chars:
                        raise _BoundExceeded
                    words = _positioned_words(
                        text_page, char_count, max_layout_words=max_layout_words
                    )
                    extracted_characters = sum(len(word["text"].strip()) for word in words)
                    blocks = layout_page(
                        [PositionedWord(word["text"], tuple(word["bbox"])) for word in words],
                        page=page_number + 1,
                        width=width,
                        height=height,
                        source_url=None,
                    )
                finally:
                    text_page.close()
            finally:
                page.close()
            pages.append(
                {
                    "number": page_number + 1,
                    "width": width,
                    "height": height,
                    "bbox": [left, bottom, right, top],
                    "rotation": rotation,
                    "char_count": char_count,
                    "extracted_characters": extracted_characters,
                    "has_page_objects": has_page_objects,
                    "has_raster_objects": has_raster_objects,
                    "blocks": [
                        {"kind": block.kind, "text": block.text, "bbox": block.bbox}
                        for block in blocks
                    ],
                }
            )
        payload = json.dumps(
            {"worker_pid": os.getpid(), "pages": pages},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if len(payload) > max_json_bytes:
            raise _BoundExceeded
        return payload
    finally:
        pdf.close()


def _page_object_flags(page: Any, *, maximum: int) -> tuple[bool, bool]:
    import pypdfium2.raw as pdfium_raw

    count = 0
    has_raster_objects = False
    for page_object in page.get_objects(max_depth=15):
        count += 1
        if count > maximum:
            raise _BoundExceeded
        has_raster_objects = has_raster_objects or page_object.type == pdfium_raw.FPDF_PAGEOBJ_IMAGE
    return count > 0, has_raster_objects


def _positioned_words(
    text_page: Any, char_count: int, *, max_layout_words: int
) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    pieces: list[str] = []
    boxes: list[tuple[float, float, float, float]] = []

    def flush() -> None:
        if not pieces or not boxes:
            pieces.clear()
            boxes.clear()
            return
        text = "".join(pieces).strip()
        if text:
            if len(words) >= max_layout_words:
                raise _BoundExceeded
            words.append({"text": text, "bbox": _union(boxes)})
        pieces.clear()
        boxes.clear()

    for index in range(char_count):
        value = text_page.get_text_range(index, 1, errors="strict")
        if not value or value.isspace() or any(ord(char) < 32 for char in value):
            flush()
            continue
        try:
            box = tuple(float(number) for number in text_page.get_charbox(index))
        except Exception:
            flush()
            continue
        if len(box) != 4 or not all(math.isfinite(number) for number in box):
            flush()
            continue
        if boxes and not _same_word(boxes[-1], box):
            flush()
        pieces.append(value)
        boxes.append(box)
    flush()
    return words


def _same_word(
    previous: tuple[float, float, float, float], current: tuple[float, float, float, float]
) -> bool:
    previous_height = max(previous[3] - previous[1], 1.0)
    current_height = max(current[3] - current[1], 1.0)
    baseline_tolerance = max(previous_height, current_height) * 0.6
    gap_tolerance = max(previous_height, current_height) * 0.75
    return (
        abs(previous[1] - current[1]) <= baseline_tolerance
        and -gap_tolerance <= current[0] - previous[2] <= gap_tolerance
    )


def _union(boxes: list[tuple[float, float, float, float]]) -> list[float]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _render(data: bytes, config: Mapping[str, Any]) -> bytes:
    page_index = config.get("page_index")
    if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
        _fail("invalid_request", _EXIT_INTERNAL)
    scale = _positive_number(config, "scale")
    max_pixels = _positive_integer(config, "max_page_pixels")
    max_png_bytes = _positive_integer(config, "max_png_bytes")
    max_width = _positive_number(config, "max_page_width_points")
    max_height = _positive_number(config, "max_page_height_points")

    pdf = _load_pdf(data)
    try:
        if page_index >= len(pdf):
            _fail("corrupt", _EXIT_CORRUPT)
        page = pdf[page_index]
        left, bottom, right, top = (float(value) for value in page.get_bbox())
        width = right - left
        height = top - bottom
        if (
            not all(math.isfinite(value) for value in (left, bottom, right, top))
            or width <= 0
            or height <= 0
            or width > max_width
            or height > max_height
        ):
            raise _BoundExceeded
        display_width, display_height = page.get_size()
        pixel_width = math.ceil(display_width * scale)
        pixel_height = math.ceil(display_height * scale)
        if pixel_width <= 0 or pixel_height <= 0 or pixel_width * pixel_height > max_pixels:
            raise _BoundExceeded
        try:
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil()
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=False)
                payload = output.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()
        if len(payload) > max_png_bytes:
            raise _BoundExceeded
        return payload
    finally:
        pdf.close()


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"extract", "render"}:
        _fail("invalid_request", _EXIT_INTERNAL)
    try:
        config = json.loads(sys.argv[2])
    except (json.JSONDecodeError, UnicodeError):
        _fail("invalid_request", _EXIT_INTERNAL)
    if not isinstance(config, dict):
        _fail("invalid_request", _EXIT_INTERNAL)
    memory_limit = _positive_integer(config, "max_memory_bytes")
    input_limit = _positive_integer(config, "max_input_bytes")
    _set_memory_limit(memory_limit)
    data = sys.stdin.buffer.read(input_limit + 1)
    if len(data) > input_limit:
        _fail("too_large", _EXIT_TOO_LARGE)
    try:
        payload = _extract(data, config) if sys.argv[1] == "extract" else _render(data, config)
    except _BoundExceeded:
        _fail("too_large", _EXIT_TOO_LARGE)
    except (MemoryError, OverflowError):
        _fail("too_large", _EXIT_TOO_LARGE)
    except SystemExit:
        raise
    except Exception:
        _fail("corrupt", _EXIT_CORRUPT)
    sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    main()
