from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from cairn.ingestion.base import ParseContext
from cairn.ingestion.ocr import OcrResult, OcrWord
from cairn.ingestion.pdf import (
    PdfCorruptFile,
    PdfEncrypted,
    PdfLimits,
    PdfOcrRequired,
    PdfParser,
    PdfTooLarge,
)
from cairn.ingestion.pdf_layout import PositionedWord, layout_page


def _pdf(content: bytes, *, width: int = 612, height: int = 792) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


def _text_pdf(text: str = "Hello Cairn PDF") -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return _pdf(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode())


@dataclass
class RecordingOcr:
    calls: int = 0

    async def recognize(self, data: bytes, *, language: str | None = None) -> OcrResult:
        self.calls += 1
        return OcrResult(
            text="Scanned page",
            words=(OcrWord("Scanned", 95.0, (10, 10, 80, 30)),),
            width=612,
            height=792,
            language=language or "eng",
            engine="recording",
            engine_version="1",
        )


def test_layout_orders_columns_and_emits_gfm_table() -> None:
    """FR-F-02: positioned words preserve columns and table structure."""
    words = [
        PositionedWord("Right", (330, 730, 380, 742)),
        PositionedWord("Left", (40, 730, 80, 742)),
        PositionedWord("A", (40, 650, 60, 662)),
        PositionedWord("B", (180, 650, 200, 662)),
        PositionedWord("1", (40, 630, 60, 642)),
        PositionedWord("2", (180, 630, 200, 642)),
    ]

    blocks = layout_page(words, page=1, width=612, height=792, source_url="source")

    assert blocks[0].text == "Left"
    assert blocks[1].text == "Right"
    table = next(block for block in blocks if block.kind == "table")
    assert table.text == "| A | B |\n| --- | --- |\n| 1 | 2 |"
    assert table.page == 1
    assert table.source_url == "source"


async def test_pdf_parser_extracts_text_in_isolated_process_with_provenance() -> None:
    """FR-F-01/02/07: PDF extraction returns page geometry from process isolation."""
    parsed = await PdfParser().parse(
        _text_pdf(), ParseContext(source_url="https://example.test/document.pdf", language="en")
    )

    assert "Hello Cairn PDF" in parsed.markdown
    assert parsed.pages == 1
    assert parsed.blocks[0].page == 1
    assert parsed.blocks[0].bbox is not None
    assert parsed.blocks[0].source_url == "https://example.test/document.pdf"
    assert parsed.metadata["pdf_engine"] == "pypdfium2"
    assert parsed.metadata["pdf_worker_pid"] != os.getpid()
    assert parsed.metadata["coordinate_space"] == "pdf_points_bottom_left"


async def test_low_density_pdf_routes_page_to_ocr() -> None:
    """FR-F-03: low-density pages are rendered and OCRed automatically."""
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr, limits=PdfLimits(ocr_text_threshold=100)).parse(
        _text_pdf("x"), ParseContext(language="en")
    )

    assert ocr.calls == 1
    assert parsed.markdown == "Scanned page"
    assert parsed.metadata["ocr_pages"] == [1]
    assert parsed.blocks[0].page == 1
    assert parsed.blocks[0].bbox is not None


async def test_low_density_pdf_without_engine_is_explicit() -> None:
    """FR-F-03: scans fail explicitly when OCR is not configured."""
    with pytest.raises(PdfOcrRequired) as caught:
        await PdfParser(limits=PdfLimits(ocr_text_threshold=100)).parse(
            _text_pdf("x"), ParseContext()
        )
    assert caught.value.code == "PDF_OCR_REQUIRED"


@pytest.mark.parametrize("data", [b"not a pdf", b"%PDF-1.7\ntruncated"])
async def test_corrupt_pdf_has_stable_error(data: bytes) -> None:
    """FR-F-01: malformed PDF bytes produce a stable non-retryable error."""
    with pytest.raises(PdfCorruptFile) as caught:
        await PdfParser().parse(data, ParseContext())
    assert caught.value.code == "PDF_CORRUPT_FILE"
    assert caught.value.retryable is False


async def test_encrypted_pdf_has_distinct_error() -> None:
    """FR-F-01: encrypted PDFs are distinguishable from corrupt files."""
    data = b"%PDF-1.7\n1 0 obj << /Encrypt 2 0 R >> endobj\n%%EOF"
    with pytest.raises(PdfEncrypted) as caught:
        await PdfParser().parse(data, ParseContext())
    assert caught.value.code == "PDF_ENCRYPTED"


async def test_direct_pdf_parser_enforces_its_own_limits() -> None:
    """FR-F-01: direct adapter calls cannot bypass PDF resource limits."""
    with pytest.raises(PdfTooLarge) as caught:
        await PdfParser(limits=PdfLimits(max_input_bytes=10)).parse(_text_pdf(), ParseContext())
    assert caught.value.code == "PDF_TOO_LARGE"


def test_invalid_pdf_limits_are_rejected() -> None:
    """FR-F-01/03: invalid PDF resource limits fail during construction."""
    with pytest.raises(ValueError, match="positive"):
        PdfLimits(max_pages=0)
