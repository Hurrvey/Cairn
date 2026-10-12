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
from typing import Any, NoReturn, cast

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
    max_raster_rectangles = _positive_integer(config, "max_raster_rectangles")
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
                statistics: dict[str, int] = {}
                has_page_objects, raster_rectangles = _page_object_geometry(
                    page,
                    maximum=max_page_objects,
                    maximum_rasters=max_raster_rectangles,
                    page_bbox=(left, bottom, right, top),
                    statistics=statistics,
                )
                page_area = width * height
                raster_coverage = _rectangle_union_area(raster_rectangles) / page_area
                largest_raster_coverage = max(
                    (_rectangle_area(rectangle) / page_area for rectangle in raster_rectangles),
                    default=0.0,
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
                    native_text_coverage, native_text_band_coverage = _native_text_geometry(
                        words, (left, bottom, right, top)
                    )
                    blocks = layout_page(
                        [
                            PositionedWord(
                                word["text"], tuple(word["bbox"]), bool(word["line_break"])
                            )
                            for word in words
                        ],
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
                    "object_count": statistics["objects"],
                    "word_count": len(words),
                    "raster_count": len(raster_rectangles),
                    "extracted_characters": extracted_characters,
                    "has_page_objects": has_page_objects,
                    "has_raster_objects": bool(raster_rectangles),
                    "raster_coverage": raster_coverage,
                    "largest_raster_coverage": largest_raster_coverage,
                    "native_text_coverage": native_text_coverage,
                    "native_text_band_coverage": native_text_band_coverage,
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


def _page_object_geometry(
    page: Any,
    *,
    maximum: int,
    maximum_rasters: int,
    page_bbox: tuple[float, float, float, float],
    statistics: dict[str, int] | None = None,
) -> tuple[bool, list[tuple[float, float, float, float]]]:
    import pypdfium2.raw as pdfium_raw

    count = 0
    raster_rectangles: list[tuple[float, float, float, float]] = []
    for page_object in page.get_objects(max_depth=15):
        count += 1
        if count > maximum:
            raise _BoundExceeded
        if page_object.type != pdfium_raw.FPDF_PAGEOBJ_IMAGE:
            continue
        if len(raster_rectangles) >= maximum_rasters:
            raise _BoundExceeded
        try:
            raw = _page_rectangle(page_object)
        except Exception as exc:
            raise _BoundExceeded from exc
        if len(raw) != 4 or not all(math.isfinite(value) for value in raw):
            raise _BoundExceeded
        clipped = _clip_rectangle(raw, page_bbox)
        if clipped is not None:
            raster_rectangles.append(clipped)
    if statistics is not None:
        statistics["objects"] = count
    return count > 0, raster_rectangles


def _page_rectangle(page_object: Any) -> tuple[float, float, float, float]:
    """Map object bounds through enclosing Form matrices into page space."""
    rectangle = tuple(float(value) for value in page_object.get_bounds())
    if len(rectangle) != 4 or not all(math.isfinite(value) for value in rectangle):
        raise _BoundExceeded
    points = [
        (rectangle[0], rectangle[1]),
        (rectangle[0], rectangle[3]),
        (rectangle[2], rectangle[1]),
        (rectangle[2], rectangle[3]),
    ]
    container = getattr(page_object, "container", None)
    depth = 0
    while container is not None:
        depth += 1
        if depth > 15:
            raise _BoundExceeded
        matrix = container.get_matrix()
        transformed: list[tuple[float, float]] = []
        for point in points:
            x, y = matrix.on_point(*point)
            transformed.append((float(x), float(y)))
        points = transformed
        if any(
            len(point) != 2 or not all(math.isfinite(value) for value in point) for point in points
        ):
            raise _BoundExceeded
        container = getattr(container, "container", None)
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _native_text_geometry(
    words: list[dict[str, Any]], page_bbox: tuple[float, float, float, float]
) -> tuple[float, float]:
    if not words:
        return 0.0, 0.0
    boxes = [
        clipped
        for word in words
        if (clipped := _clip_rectangle(tuple(word["bbox"]), page_bbox)) is not None
    ]
    if not boxes:
        return 0.0, 0.0
    native_coverage = _grid_rectangle_coverage(boxes, page_bbox)
    page_bottom, page_top = page_bbox[1], page_bbox[3]
    band_height = (page_top - page_bottom) / 8
    occupied = {
        min(7, max(0, int((((box[1] + box[3]) / 2) - page_bottom) / band_height))) for box in boxes
    }
    return native_coverage, len(occupied) / 8


def _grid_rectangle_coverage(
    rectangles: list[tuple[float, float, float, float]],
    bounds: tuple[float, float, float, float],
    *,
    grid_size: int = 32,
) -> float:
    """Bounded overlap-aware coverage estimate for native word rectangles."""
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    occupied_rows = [0] * grid_size
    for left, bottom, right, top in rectangles:
        x_start = min(grid_size - 1, max(0, int((left - bounds[0]) / width * grid_size)))
        x_stop = min(
            grid_size - 1,
            max(0, math.ceil((right - bounds[0]) / width * grid_size) - 1),
        )
        y_start = min(grid_size - 1, max(0, int((bottom - bounds[1]) / height * grid_size)))
        y_stop = min(
            grid_size - 1,
            max(0, math.ceil((top - bounds[1]) / height * grid_size) - 1),
        )
        mask = ((1 << (x_stop - x_start + 1)) - 1) << x_start
        for row in range(y_start, y_stop + 1):
            occupied_rows[row] |= mask
    return sum(row.bit_count() for row in occupied_rows) / (grid_size * grid_size)


def _clip_rectangle(
    rectangle: tuple[float, float, float, float],
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    left = max(rectangle[0], bounds[0])
    bottom = max(rectangle[1], bounds[1])
    right = min(rectangle[2], bounds[2])
    top = min(rectangle[3], bounds[3])
    if left >= right or bottom >= top:
        return None
    return (left, bottom, right, top)


def _rectangle_area(rectangle: tuple[float, float, float, float]) -> float:
    return (rectangle[2] - rectangle[0]) * (rectangle[3] - rectangle[1])


def _rectangle_union_area(rectangles: list[tuple[float, float, float, float]]) -> float:
    if not rectangles:
        return 0.0
    events: list[tuple[float, int, float, float]] = []
    for left, bottom, right, top in rectangles:
        events.append((left, 1, bottom, top))
        events.append((right, -1, bottom, top))
    events.sort()
    active: list[tuple[float, float]] = []
    area = 0.0
    previous_x = events[0][0]
    index = 0
    while index < len(events):
        x = events[index][0]
        if x > previous_x and active:
            area += (x - previous_x) * _interval_union_length(active)
        while index < len(events) and events[index][0] == x:
            _, direction, bottom, top = events[index]
            interval = (bottom, top)
            if direction == 1:
                active.append(interval)
            else:
                active.remove(interval)
            index += 1
        previous_x = x
    return area


def _interval_union_length(intervals: list[tuple[float, float]]) -> float:
    ordered = sorted(intervals)
    start, stop = ordered[0]
    total = 0.0
    for next_start, next_stop in ordered[1:]:
        if next_start > stop:
            total += stop - start
            start, stop = next_start, next_stop
        else:
            stop = max(stop, next_stop)
    return total + stop - start


def _positioned_words(
    text_page: Any, char_count: int, *, max_layout_words: int
) -> list[dict[str, Any]]:
    """Assemble words in pdfium reading order and mark words that begin a new line."""
    words: list[dict[str, Any]] = []
    pieces: list[str] = []
    boxes: list[tuple[float, float, float, float]] = []
    state = {"break_pending": True, "word_break": True}

    def flush() -> None:
        if pieces and boxes:
            text = "".join(pieces).strip()
            if text:
                if len(words) >= max_layout_words:
                    raise _BoundExceeded
                words.append(
                    {"text": text, "bbox": _union(boxes), "line_break": state["word_break"]}
                )
        pieces.clear()
        boxes.clear()

    for index in range(char_count):
        value = text_page.get_text_range(index, 1, errors="strict")
        if not value:
            continue
        if chr(10) in value or chr(13) in value:  # pdfium marks line ends with newlines
            flush()
            state["break_pending"] = True
            continue
        if value.isspace() or any(ord(char) < 32 for char in value):
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
        if not pieces:
            state["word_break"] = state["break_pending"]
        pieces.append(value)
        boxes.append(box)
        state["break_pending"] = False
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


def _layout_ocr(data: bytes, config: Mapping[str, Any]) -> bytes:
    max_layout_words = _positive_integer(config, "max_layout_words")
    max_json_bytes = _positive_integer(config, "max_json_bytes")
    page_number = _positive_integer(config, "page_number")
    width = _positive_number(config, "page_width")
    height = _positive_number(config, "page_height")
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeError):
        _fail("invalid_request", _EXIT_INTERNAL)
    if not isinstance(payload, dict) or not isinstance(payload.get("words"), list):
        _fail("invalid_request", _EXIT_INTERNAL)
    raw_words = payload["words"]
    if not 0 < len(raw_words) <= max_layout_words:
        raise _BoundExceeded
    words: list[PositionedWord] = []
    for raw_word in raw_words:
        if not isinstance(raw_word, dict) or not isinstance(raw_word.get("text"), str):
            _fail("invalid_request", _EXIT_INTERNAL)
        bbox = raw_word.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            _fail("invalid_request", _EXIT_INTERNAL)
        try:
            coordinates = cast(
                "tuple[float, float, float, float]", tuple(float(item) for item in bbox)
            )
            words.append(PositionedWord(raw_word["text"], coordinates))
        except (TypeError, ValueError):
            _fail("invalid_request", _EXIT_INTERNAL)
    blocks = layout_page(
        words,
        page=page_number,
        width=width,
        height=height,
        source_url=None,
    )
    output = json.dumps(
        {
            "blocks": [
                {"kind": block.kind, "text": block.text, "bbox": block.bbox} for block in blocks
            ]
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    if len(output) > max_json_bytes:
        raise _BoundExceeded
    return output


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in {"extract", "render", "layout"}:
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
        if sys.argv[1] == "extract":
            payload = _extract(data, config)
        elif sys.argv[1] == "render":
            payload = _render(data, config)
        else:
            payload = _layout_ocr(data, config)
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
