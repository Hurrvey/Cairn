"""Small deterministic layout heuristics for positioned PDF words.

The module intentionally handles only evidence that can be recovered reliably:
aligned consecutive rows become tables and clearly separated bands become
columns. Irregular layouts remain individual paragraph blocks.
"""

from __future__ import annotations

import html
import math
import statistics
from dataclasses import dataclass

from cairn.ingestion.base import Block


@dataclass(frozen=True, slots=True)
class PositionedWord:
    text: str
    bbox: tuple[float, float, float, float]

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
class _Row:
    words: list[PositionedWord]
    anchor: float

    @property
    def top(self) -> float:
        return max(word.bbox[3] for word in self.words)

    @property
    def bottom(self) -> float:
        return min(word.bbox[1] for word in self.words)


def layout_page(
    words: list[PositionedWord],
    *,
    page: int,
    width: float,
    height: float,
    source_url: str | None,
) -> list[Block]:
    """Convert positioned words into conservative text/table blocks.

    Coordinates are retained in PDF's bottom-left point space. The heuristic
    does not attempt spanning cells, nested tables, arbitrary rotations, or
    magazine-style reading order.
    """
    if page <= 0 or width <= 0 or height <= 0:
        raise ValueError("page and dimensions must be positive")
    if not words:
        return []

    rows = _group_rows(words)
    cells = [_split_cells(row) for row in rows]
    table_ranges = _table_ranges(rows, cells, width=width)
    blocks: list[Block] = []
    cursor = 0
    for start, stop in table_ranges:
        blocks.extend(_paragraph_blocks(cells[cursor:start], page, width, source_url, len(blocks)))
        blocks.append(_table_block(cells[start:stop], page, source_url, len(blocks)))
        cursor = stop
    blocks.extend(_paragraph_blocks(cells[cursor:], page, width, source_url, len(blocks)))
    return blocks


def _group_rows(words: list[PositionedWord]) -> list[_Row]:
    ordered = sorted(words, key=lambda word: (-_center_y(word.bbox), word.bbox[0]))
    heights = [max(1.0, word.bbox[3] - word.bbox[1]) for word in ordered]
    tolerance = max(2.0, statistics.median(heights) * 0.6)
    rows: list[_Row] = []
    for word in ordered:
        center = _center_y(word.bbox)
        if not rows or abs(rows[-1].anchor - center) > tolerance:
            rows.append(_Row([word], center))
        else:
            rows[-1].words.append(word)
    rows.sort(key=lambda row: -row.top)
    for row in rows:
        row.words.sort(key=lambda word: word.bbox[0])
    return rows


def _split_cells(row: _Row) -> list[_Cell]:
    heights = [max(1.0, word.bbox[3] - word.bbox[1]) for word in row.words]
    gap_threshold = max(12.0, statistics.median(heights) * 2.5)
    groups: list[list[PositionedWord]] = []
    for word in row.words:
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


def _table_ranges(
    rows: list[_Row], cells: list[list[_Cell]], *, width: float
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(cells) - 1:
        stop = index + 1
        while stop < len(cells) and _rows_align(
            rows[stop - 1], cells[stop - 1], rows[stop], cells[stop], width=width
        ):
            stop += 1
        if stop - index >= 2:
            ranges.append((index, stop))
            index = stop
        else:
            index += 1
    return ranges


def _rows_align(
    first_row: _Row,
    first: list[_Cell],
    second_row: _Row,
    second: list[_Cell],
    *,
    width: float,
) -> bool:
    if len(first) < 2 or len(first) != len(second):
        return False
    if first[-1].bbox[0] - first[0].bbox[0] > width * 0.4:
        return False
    line_height = max(first_row.top - first_row.bottom, second_row.top - second_row.bottom, 1.0)
    if first_row.bottom - second_row.top > max(36.0, line_height * 3):
        return False
    tolerance = max(8.0, width * 0.04)
    return all(
        abs(left.bbox[0] - right.bbox[0]) <= tolerance
        for left, right in zip(first, second, strict=True)
    )


def _paragraph_blocks(
    row_cells: list[list[_Cell]],
    page: int,
    width: float,
    source_url: str | None,
    ordinal_start: int,
) -> list[Block]:
    flat = [cell for cells in row_cells for cell in cells]
    if not flat:
        return []

    columns: list[list[_Cell]] = []
    x_tolerance = max(12.0, width * 0.08)
    for cell in sorted(flat, key=lambda item: item.bbox[0]):
        if not columns or cell.bbox[0] - columns[-1][0].bbox[0] > x_tolerance:
            columns.append([cell])
        else:
            columns[-1].append(cell)

    columns.sort(key=lambda column: statistics.median(cell.bbox[0] for cell in column))
    ordered = [
        cell
        for column in columns
        for cell in sorted(column, key=lambda item: (-item.bbox[3], item.bbox[0]))
    ]
    return [
        Block(
            kind="paragraph",
            text=cell.text,
            ordinal=ordinal_start + offset,
            page=page,
            bbox=cell.bbox,
            source_url=source_url,
        )
        for offset, cell in enumerate(ordered)
    ]


def _table_block(rows: list[list[_Cell]], page: int, source_url: str | None, ordinal: int) -> Block:
    rendered: list[str] = []
    for row_number, row in enumerate(rows):
        rendered.append("| " + " | ".join(_escape_cell(cell.text) for cell in row) + " |")
        if row_number == 0:
            rendered.append("| " + " | ".join("---" for _ in row) + " |")
    return Block(
        kind="table",
        text="\n".join(rendered),
        ordinal=ordinal,
        page=page,
        bbox=_union_bbox([cell.bbox for row in rows for cell in row]),
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


def _union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
