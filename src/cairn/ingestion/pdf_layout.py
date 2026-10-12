"""Deterministic layout of positioned PDF words into paragraphs and tables.

Reading order follows the extractor's own word order. pypdfium2 already emits
characters in the page reading order and marks line ends with newlines, so this
module must not re-sort words by geometry: sorting by coordinates scrambles
justified prose, display equations and multi-column text. Geometry is used only
to split a line into cells, to detect tables, and to merge wrapped lines into
paragraphs.

When the extractor does not supply explicit line breaks (for example Tesseract
words routed through the OCR layout action), consecutive words are grouped into
lines by their vertical position, and clearly separated vertical gutters are used
to recover column order.
"""

from __future__ import annotations

import html
import itertools
import math
import re
import statistics
from dataclasses import dataclass

from cairn.ingestion.base import Block


@dataclass(frozen=True, slots=True)
class PositionedWord:
    text: str
    bbox: tuple[float, float, float, float]
    line_break: bool = False

    def __post_init__(self) -> None:
        left, bottom, right, top = self.bbox
        if not self.text.strip():
            raise ValueError("positioned word text must not be empty")
        if not all(math.isfinite(value) for value in self.bbox):
            raise ValueError("positioned word coordinates must be finite")
        if left > right or bottom > top:
            raise ValueError("positioned word bounding box is inverted")


@dataclass(frozen=True, slots=True)
class _Cell:
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(slots=True)
class _Line:
    words: list[PositionedWord]
    cells: list[_Cell]

    @property
    def top(self) -> float:
        return max(word.bbox[3] for word in self.words)

    @property
    def bottom(self) -> float:
        return min(word.bbox[1] for word in self.words)

    @property
    def height(self) -> float:
        return max(1.0, self.top - self.bottom)


def layout_page(
    words: list[PositionedWord],
    *,
    page: int,
    width: float,
    height: float,
    source_url: str | None,
) -> list[Block]:
    """Convert positioned words into text and table blocks without reordering them."""
    if page <= 0 or width <= 0 or height <= 0:
        raise ValueError("page and dimensions must be positive")
    if not words:
        return []

    explicit = any(word.line_break for word in words)
    lines = _assemble_lines(words, explicit=explicit)
    table_ranges = _table_ranges(lines, width=width)
    blocks: list[Block] = []
    cursor = 0
    for start, stop in table_ranges:
        blocks.extend(
            _paragraph_blocks(
                lines[cursor:start], page, width, source_url, len(blocks), reorder=not explicit
            )
        )
        blocks.append(_table_block(lines[start:stop], page, source_url, len(blocks)))
        cursor = stop
    blocks.extend(
        _paragraph_blocks(
            lines[cursor:], page, width, source_url, len(blocks), reorder=not explicit
        )
    )
    return blocks


def _assemble_lines(words: list[PositionedWord], *, explicit: bool) -> list[_Line]:
    heights = [max(1.0, word.bbox[3] - word.bbox[1]) for word in words]
    tolerance = max(3.0, statistics.median(heights) * 0.6)
    lines: list[_Line] = []
    current: list[PositionedWord] = []
    for word in words:
        if current:
            if explicit:
                new_line = word.line_break
            else:
                new_line = abs(_center_y(word.bbox) - _line_center(current)) > tolerance
            if new_line:
                lines.append(_line(current))
                current = []
        current.append(word)
    if current:
        lines.append(_line(current))
    return lines


def _line(words: list[PositionedWord]) -> _Line:
    ordered = sorted(words, key=lambda word: word.bbox[0])
    return _Line(ordered, _split_cells(ordered))


def _line_center(words: list[PositionedWord]) -> float:
    return statistics.median(_center_y(word.bbox) for word in words)


def _split_cells(words: list[PositionedWord]) -> list[_Cell]:
    heights = [max(1.0, word.bbox[3] - word.bbox[1]) for word in words]
    gap_threshold = max(12.0, statistics.median(heights) * 2.5)
    groups: list[list[PositionedWord]] = []
    for word in words:
        if not groups or word.bbox[0] - groups[-1][-1].bbox[2] > gap_threshold:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [
        _Cell(
            text=" ".join(word.text.strip() for word in group),
            bbox=_union_bbox([word.bbox for word in group]),
        )
        for group in groups
    ]


def _table_ranges(lines: list[_Line], *, width: float) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(lines) - 1:
        stop = index + 1
        while stop < len(lines) and _lines_align(lines[stop - 1], lines[stop], width=width):
            stop += 1
        if stop - index >= 2 and _table_evidence(
            [line.cells for line in lines[index:stop]], width=width
        ):
            ranges.append((index, stop))
            index = stop
        else:
            index += 1
    return ranges


def _lines_align(first: _Line, second: _Line, *, width: float) -> bool:
    if len(first.cells) < 2 or len(first.cells) != len(second.cells):
        return False
    line_height = max(first.height, second.height, 1.0)
    if first.bottom - second.top > max(36.0, line_height * 3):
        return False
    tolerance = max(8.0, width * 0.04)
    return all(
        abs(left.bbox[0] - right.bbox[0]) <= tolerance
        for left, right in zip(first.cells, second.cells, strict=True)
    )


_NUMERIC_CELL = re.compile(r"^[+\-]?[$€£¥]?(?:\(\s*)?\d[\d,]*(?:\.\d+)?%?(?:\s*\))?$")
_FORMULA_MARKERS = ("=", "^", "∑", "∫")


def _table_evidence(rows: list[list[_Cell]], *, width: float) -> bool:
    span = rows[0][-1].bbox[0] - rows[0][0].bbox[0]
    if span <= width * 0.4:
        return True
    column_count = len(rows[0])
    body = rows[1:]
    if column_count == 2 and len(body) < 2:
        return False
    if any(_formula_cell(cell.text) for row in rows for cell in row):
        return False
    if any(_numeric_cell(row[0].text) for row in body):
        return False
    numeric_columns = [
        {index for index, cell in enumerate(row) if _numeric_cell(cell.text)} for row in body
    ]
    stable = set.intersection(*numeric_columns) if numeric_columns else set()
    required = 2 if column_count >= 3 else 1
    return len(stable) >= required


def _numeric_cell(text: str) -> bool:
    return bool(_NUMERIC_CELL.fullmatch(text.strip()))


def _formula_cell(text: str) -> bool:
    return any(marker in text for marker in _FORMULA_MARKERS)


def _paragraph_blocks(
    lines: list[_Line],
    page: int,
    width: float,
    source_url: str | None,
    ordinal_start: int,
    *,
    reorder: bool,
) -> list[Block]:
    if reorder:
        # No trustworthy order from the extractor: recover columns geometrically.
        cells = [cell for line in lines for cell in line.cells]
        ordered = _reading_order(cells, width) if cells else []
    else:
        # The extractor supplied reading order and line ends; keep every line whole.
        ordered = [_line_cell(line) for line in lines]
    if not ordered:
        return []
    blocks: list[Block] = []
    for group in _merge_paragraphs(ordered):
        blocks.append(
            Block(
                kind="paragraph",
                text=_paragraph_text(group),
                ordinal=ordinal_start + len(blocks),
                page=page,
                bbox=_union_bbox([cell.bbox for cell in group]),
                source_url=source_url,
            )
        )
    return blocks


def _line_cell(line: _Line) -> _Cell:
    return _Cell(
        text=" ".join(cell.text for cell in line.cells),
        bbox=_union_bbox([cell.bbox for cell in line.cells]),
    )


def _reading_order(cells: list[_Cell], width: float) -> list[_Cell]:
    spanning = [cell for cell in cells if cell.bbox[2] - cell.bbox[0] >= width * 0.6]
    if not spanning:
        return _column_order(cells, width)
    ordered_spanning = sorted(spanning, key=lambda item: -item.bbox[3])
    remaining = [cell for cell in cells if cell not in spanning]
    ordered: list[_Cell] = []
    upper = math.inf
    for band in ordered_spanning:
        local = [cell for cell in remaining if band.bbox[3] < cell.bbox[3] <= upper]
        ordered.extend(_column_order(local, width))
        ordered.append(band)
        remaining = [cell for cell in remaining if cell not in local]
        upper = band.bbox[1]
    ordered.extend(_column_order(remaining, width))
    return ordered


def _column_order(cells: list[_Cell], width: float) -> list[_Cell]:
    if len(cells) <= 1:
        return list(cells)
    split = _gutter_split(cells, width)
    if split is not None:
        left, right = split
        return _column_order(left, width) + _column_order(right, width)
    return sorted(cells, key=lambda cell: (-_center_y(cell.bbox), cell.bbox[0]))


def _gutter_split(cells: list[_Cell], width: float) -> tuple[list[_Cell], list[_Cell]] | None:
    """Split on the widest vertical whitespace band that no cell crosses."""
    intervals = sorted((cell.bbox[0], cell.bbox[2]) for cell in cells)
    merged: list[list[float]] = []
    for low, high in intervals:
        if merged and low <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], high)
        else:
            merged.append([low, high])
    best_gap = 0.0
    best_x: float | None = None
    for (_, high), (low, _) in itertools.pairwise(merged):
        gap = low - high
        if gap > best_gap:
            best_gap = gap
            best_x = (high + low) / 2
    heights = [max(1.0, cell.bbox[3] - cell.bbox[1]) for cell in cells]
    threshold = max(width * 0.08, statistics.median(heights) * 3.0)
    if best_x is None or best_gap < threshold:
        return None
    left = [cell for cell in cells if _center_x(cell.bbox) < best_x]
    right = [cell for cell in cells if _center_x(cell.bbox) >= best_x]
    if not left or not right:
        return None
    return left, right


def _merge_paragraphs(cells: list[_Cell]) -> list[list[_Cell]]:
    groups: list[list[_Cell]] = []
    for cell in cells:
        if groups and _same_paragraph(groups[-1][-1], cell):
            groups[-1].append(cell)
        else:
            groups.append([cell])
    return groups


def _same_paragraph(previous: _Cell, cell: _Cell) -> bool:
    height = max(1.0, previous.bbox[3] - previous.bbox[1])
    margin = max(16.0, height * 2.5)
    gap = previous.bbox[1] - cell.bbox[3]
    return abs(previous.bbox[0] - cell.bbox[0]) <= margin and -height * 0.5 <= gap <= height * 1.6


def _paragraph_text(cells: list[_Cell]) -> str:
    text = cells[0].text
    for previous, cell in itertools.pairwise(cells):
        joiner = "" if _joins_without_space(previous.text, cell.text) else " "
        text = f"{text}{joiner}{cell.text}"
    return text


def _joins_without_space(left: str, right: str) -> bool:
    return bool(left) and bool(right) and _is_cjk(left[-1]) and _is_cjk(right[0])


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3000 <= code <= 0x303F
        or 0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xFF00 <= code <= 0xFFEF
    )


def _table_block(lines: list[_Line], page: int, source_url: str | None, ordinal: int) -> Block:
    rendered: list[str] = []
    for row_number, line in enumerate(lines):
        rendered.append("| " + " | ".join(_escape_cell(cell.text) for cell in line.cells) + " |")
        if row_number == 0:
            rendered.append("| " + " | ".join("---" for _ in line.cells) + " |")
    return Block(
        kind="table",
        text="\n".join(rendered),
        ordinal=ordinal,
        page=page,
        bbox=_union_bbox([cell.bbox for line in lines for cell in line.cells]),
        source_url=source_url,
    )


def _escape_cell(text: str) -> str:
    return (
        html.escape(text, quote=False)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def _center_y(bbox: tuple[float, float, float, float]) -> float:
    return (bbox[1] + bbox[3]) / 2


def _center_x(bbox: tuple[float, float, float, float]) -> float:
    return (bbox[0] + bbox[2]) / 2


def _union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
