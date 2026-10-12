"""Scans with an embedded text layer skip Docling's OCR; everything else goes to Docling."""

from __future__ import annotations

from tests.unit.ingestion.test_pdf import _image_pdf, _mixed_raster_text_pdf, _text_pdf

from cairn.ingestion.base import Block, ParseContext, ParsedDocument
from cairn.ingestion.pdf import PdfPageFacts, PdfParser
from cairn.ingestion.pdf_routing import (
    ScanTextLayerPolicy,
    ScanTextLayerRoutedPdfParser,
    decide,
)


class StubDocling:
    supported_mimes = frozenset({"application/pdf"})

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        self.calls += 1
        return ParsedDocument(
            markdown="docling",
            blocks=[Block("paragraph", "docling", 0)],
            metadata={"format": "pdf", "pdf_engine": "docling"},
        )

    async def aclose(self) -> None:
        self.closed = True


def _scan_with_text_layer() -> bytes:
    """A full-page image plus a dense producer-embedded text layer, like an archive scan."""
    lines = [
        (72, 760 - 18 * index, f"Line {index} of the embedded OCR text layer on this scanned page.")
        for index in range(40)
    ]
    return _mixed_raster_text_pdf(image_draws=[(0, 0, 612, 792)], text_lines=lines)


def _router() -> tuple[ScanTextLayerRoutedPdfParser, StubDocling]:
    docling = StubDocling()
    return ScanTextLayerRoutedPdfParser(docling=docling, baseline=PdfParser(ocr=None)), docling


async def test_scan_with_text_layer_is_read_by_the_baseline() -> None:
    router, docling = _router()
    parsed = await router.parse(_scan_with_text_layer(), ParseContext())
    assert docling.calls == 0
    assert parsed.metadata["pdf_engine"] == "pypdfium2"
    assert parsed.metadata["pdf_routing"]["engine"] == "pdfium-text-layer"
    assert parsed.metadata["pdf_routing"]["scan_pages"] == 1
    assert "embedded OCR text layer" in parsed.markdown


async def test_scan_without_text_goes_to_docling() -> None:
    router, docling = _router()
    parsed = await router.parse(_image_pdf(), ParseContext())
    assert docling.calls == 1
    routing = parsed.metadata["pdf_routing"]
    assert routing["engine"] == "docling"
    assert routing["reason"] == "some scanned pages lack a usable text layer"


async def test_native_text_document_goes_to_docling() -> None:
    router, docling = _router()
    parsed = await router.parse(_text_pdf("Native text only, no raster."), ParseContext())
    assert docling.calls == 1
    assert parsed.metadata["pdf_routing"]["reason"] == "document is not a full-page scan"


def test_symbol_soup_text_layer_is_not_trusted() -> None:
    page = PdfPageFacts(
        number=1,
        has_page_objects=True,
        has_raster_objects=True,
        raster_coverage=1.0,
        largest_raster_coverage=1.0,
        native_text_coverage=0.6,
        extracted_characters=400,
        text="|||| ---- .... ;;;; " * 20,
    )
    assert decide((page,), ScanTextLayerPolicy()).engine == "docling"


def test_mixed_cover_page_still_counts_as_a_scan_when_most_pages_are_scans() -> None:
    def scan(number: int) -> PdfPageFacts:
        return PdfPageFacts(number, True, True, 1.0, 1.0, 0.6, 900, "real words " * 90)

    cover = PdfPageFacts(1, True, False, 0.0, 0.0, 0.2, 300, "Cover page title " * 20)
    pages = (cover, *(scan(n) for n in range(2, 13)))
    decision = decide(pages, ScanTextLayerPolicy())
    assert decision.engine == "pdfium-text-layer"
    assert (decision.content_pages, decision.scan_pages, decision.text_layer_pages) == (12, 11, 11)


async def test_aclose_reaches_the_docling_child() -> None:
    router, docling = _router()
    await router.aclose()
    assert docling.closed
