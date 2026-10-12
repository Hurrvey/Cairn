"""One trust-boundary budget validator for fresh and reused PDF evidence."""

from dataclasses import asdict
from typing import Any


def validate_pdf_budget(parsed: Any, limits: Any, *, require_facts: bool = True) -> None:
    from cairn.ingestion.pdf import PdfTooLarge

    if parsed.pages > limits.max_pages or len(parsed.markdown) > limits.max_text_chars:
        raise PdfTooLarge()
    if len(parsed.markdown.encode()) > limits.max_output_bytes:
        raise PdfTooLarge()
    elements = parsed.ir.elements if parsed.ir else parsed.elements
    page_characters: dict[int, int] = {}
    for element in elements:
        pages = {location.page for location in element.locations}
        if len(pages) == 1:
            page = next(iter(pages))
            characters = (
                sum(len(cell.text) for cell in element.table.cells)
                if element.table
                else len(element.orig or element.search_text)
            )
            page_characters[page] = page_characters.get(page, 0) + characters
        elif len(pages) > 1 and element.type not in ("table", "image"):
            for location in element.locations:
                source = element.orig if location.span_source == "orig" else element.text
                characters = len((source or "")[location.char_start : location.char_end])
                page_characters[location.page] = page_characters.get(location.page, 0) + characters
    if any(characters > limits.max_page_chars for characters in page_characters.values()):
        raise PdfTooLarge()
    if len(elements or parsed.blocks) > limits.max_elements:
        raise PdfTooLarge()
    if (
        sum(item.table.rows * item.table.columns for item in elements if item.table)
        > limits.max_table_cells
    ):
        raise PdfTooLarge()
    assets = parsed.ir.assets if parsed.ir else parsed.assets
    sizes = [
        asset.size_bytes if hasattr(asset, "size_bytes") else len(asset.data) for asset in assets
    ]
    if (
        len(assets) > limits.max_assets
        or sum(sizes) > limits.max_total_asset_bytes
        or any(size > limits.max_asset_bytes for size in sizes)
    ):
        raise PdfTooLarge()
    facts = parsed.metadata.get("pdf_budget_facts")
    if (
        not isinstance(facts, dict)
        or facts.get("schema") != 1
        or not isinstance(facts.get("pages"), list)
        or len(facts["pages"]) != parsed.pages
    ):
        if require_facts:
            raise ValueError("missing verified PDF budget facts")
        return
    mapping = {
        "characters": "max_page_chars",
        "words": "max_layout_words",
        "objects": "max_page_objects",
        "rasters": "max_raster_rectangles",
        "layout_blocks": "max_elements",
        "render_pixels": "max_page_pixels",
        "render_bytes": "max_render_bytes",
    }
    for page in facts["pages"]:
        if (
            page["width"] > limits.max_page_width_points
            or page["height"] > limits.max_page_height_points
        ):
            raise PdfTooLarge()
        for fact, bound in mapping.items():
            value = page.get(fact)
            if type(value) is not int or value < 0:
                raise ValueError("missing verified PDF resource fact")
            if value > getattr(limits, bound):
                raise PdfTooLarge()
    if type(facts.get("output_bytes")) is not int or facts["output_bytes"] <= 0:
        raise ValueError("missing verified output byte fact")
    if facts["output_bytes"] > limits.max_output_bytes:
        raise PdfTooLarge()


def worker_limits(limits: Any) -> dict[str, Any]:
    return asdict(limits)
