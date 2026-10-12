"""Single deterministic, engine-independent projection used by indexing and reading."""

from __future__ import annotations

from dataclasses import dataclass

from cairn.core.document_ir import DocumentIR, Element, Location, Table
from cairn.core.errors import CairnError


class UnsafeProjection(CairnError, ValueError):
    code = "PDF_UNSAFE_PROJECTION"
    http_status = 422
    title = "Source regions cannot be projected safely without losing or duplicating content."
    retryable = False

    def __init__(self) -> None:
        super().__init__("PDF_UNSAFE_PROJECTION")


@dataclass(frozen=True, slots=True)
class ProjectedBlock:
    element: Element
    text: str
    location: Location | None
    locations: tuple[Location, ...]

    @property
    def degraded(self) -> bool:
        """True when the content was kept but its page geometry could not be proven."""
        return self.location is None and not self.locations and bool(self.element.locations)


def project_ir(ir: DocumentIR, *, strict: bool = True) -> tuple[ProjectedBlock, ...]:
    """Project every element in order.

    With ``strict=True`` an element whose multi-page geometry cannot be sliced
    without losing or duplicating content raises ``UnsafeProjection``. With
    ``strict=False`` such an element is still emitted with its full text but
    without any page or bounding box claim, so the document stays searchable
    instead of being rejected outright.
    """
    result: list[ProjectedBlock] = []
    for element in sorted(ir.elements, key=lambda item: item.order):
        try:
            result.extend(_project_element(element))
        except UnsafeProjection:
            if strict:
                raise
            text = element.search_text
            if text:
                result.append(ProjectedBlock(element, text, None, ()))
    return tuple(result)


def _project_element(element: Element) -> list[ProjectedBlock]:
    locations = element.locations
    pages = {location.page for location in locations}
    if len(pages) > 1 and element.type in ("table", "image"):
        if element.type == "image" or element.table is None:
            raise UnsafeProjection()
        rows: dict[int, int] = {}
        for cell in element.table.cells:
            cell_pages = {location.page for location in cell.locations}
            if len(cell_pages) != 1 or not cell_pages.issubset(pages):
                raise UnsafeProjection()
            page = next(iter(cell_pages))
            for row in range(cell.row_start, cell.row_end):
                if row in rows and rows[row] != page:
                    raise UnsafeProjection()
                rows[row] = page
        if set(rows) != set(range(element.table.rows)):
            raise UnsafeProjection()
        result: list[ProjectedBlock] = []
        for page in sorted(pages):
            selected = sorted(row for row, owner in rows.items() if owner == page)
            if not selected:
                continue
            if selected != list(range(selected[0], selected[-1] + 1)):
                raise UnsafeProjection()
            cells = [
                cell.model_copy(
                    update={
                        "row_start": cell.row_start - selected[0],
                        "row_end": cell.row_end - selected[0],
                    }
                )
                for cell in element.table.cells
                if rows[cell.row_start] == page
            ]
            grid = Table(rows=len(selected), columns=element.table.columns, cells=cells)
            page_locations = tuple(location for location in locations if location.page == page)
            result.append(
                ProjectedBlock(element, grid.markdown(), _union(page_locations), page_locations)
            )
        return result
    text = element.search_text
    if not text:
        return []
    if len(pages) <= 1:
        return [ProjectedBlock(element, text, _union(locations), locations)]
    if not any(location.char_end is not None for location in locations):
        # Multipage text without provable spans cannot be sliced: keep it once, unplaced.
        return [ProjectedBlock(element, text, None, ())]
    projected: list[ProjectedBlock] = []
    for location in sorted(locations, key=lambda item: item.char_start or 0):
        source = element.orig if location.span_source == "orig" else element.text
        projected.append(
            ProjectedBlock(
                element,
                (source or "")[location.char_start : location.char_end],
                location,
                (location,),
            )
        )
    return projected


def _union(locations: tuple[Location, ...]) -> Location | None:
    if not locations:
        return None
    first = locations[0]
    if any(
        (loc.page, loc.width, loc.height, loc.crop, loc.rotation)
        != (first.page, first.width, first.height, first.crop, first.rotation)
        for loc in locations
    ):
        raise UnsafeProjection()
    return first.model_copy(
        update={
            "bbox": (
                min(loc.bbox[0] for loc in locations),
                min(loc.bbox[1] for loc in locations),
                max(loc.bbox[2] for loc in locations),
                max(loc.bbox[3] for loc in locations),
            ),
            "char_start": None,
            "char_end": None,
            "span_source": None,
        }
    )
