from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from io import BytesIO

import pytest
from PIL import Image

from cairn.ingestion.base import ParseContext
from cairn.ingestion.ocr import OcrResult, OcrWord
from cairn.ingestion.pdf import (
    PdfCorruptFile,
    PdfEncrypted,
    PdfEngineUnavailable,
    PdfLimits,
    PdfOcrRequired,
    PdfParser,
    PdfTimedOut,
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


def _image_pdf(*, rotation: int = 0) -> bytes:
    content = b"q 612 0 0 792 0 0 cm /Im1 Do Q"
    rotation_entry = f" /Rotate {rotation}" if rotation else ""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]{rotation_entry} "
            "/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>"
        ).encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
        ),
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


def _mixed_raster_text_pdf(
    *,
    image_draws: list[tuple[int, int, int, int]],
    text_lines: list[tuple[int, int, str]],
) -> bytes:
    commands: list[bytes] = []
    for x, y, text in text_lines:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        commands.append(f"BT /F1 12 Tf {x} {y} Td ({escaped}) Tj ET".encode())
    for x, y, width, height in image_draws:
        commands.append(f"q {width} 0 0 {height} {x} {y} cm /Im1 Do Q".encode())
    content = b" ".join(commands)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 6 0 R >> /XObject << /Im1 5 0 R >> >> "
            b"/Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
        ),
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


def _form_raster_text_pdf(*, image_draw: tuple[int, int, int, int]) -> bytes:
    x, y, width, height = image_draw
    page_content = (
        b"BT /F1 12 Tf 72 755 Td (NATIVE HEADER TEXT) Tj ET "
        + f"q {width} 0 0 {height} {x} {y} cm /Fm Do Q".encode()
    )
    form_content = b"q 1 0 0 1 0 0 cm /Im Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 7 0 R >> /XObject << /Fm 5 0 R >> >> "
            b"/Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(page_content)).encode()
        + b" >>\nstream\n"
        + page_content
        + b"\nendstream",
        (
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] "
            b"/Resources << /XObject << /Im 6 0 R >> >> /Length "
            + str(len(form_content)).encode()
            + b" >>\nstream\n"
            + form_content
            + b"\nendstream"
        ),
        (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
        ),
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


def _two_page_pdf() -> bytes:
    streams = [
        b"BT /F1 12 Tf 72 720 Td (First page) Tj ET",
        b"BT /F1 12 Tf 72 720 Td (Second page) Tj ET",
    ]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(streams[0])).encode()
        + b" >>\nstream\n"
        + streams[0]
        + b"\nendstream",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>"
        ),
        b"<< /Length "
        + str(len(streams[1])).encode()
        + b" >>\nstream\n"
        + streams[1]
        + b"\nendstream",
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


def _blank_cover_pdf() -> bytes:
    content = b"BT /F1 12 Tf 72 720 Td (Page after blank cover) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 6 0 R >> >> /Contents 5 0 R >>"
        ),
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


@dataclass
class ImageSizedOcr:
    async def recognize(self, data: bytes, *, language: str | None = None) -> OcrResult:
        del language
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
        return OcrResult(
            text="Rotated scan",
            words=(OcrWord("different", 95.0, (0, 0, width / 2, height / 2)),),
            width=width,
            height=height,
            language="eng",
            engine="recording",
            engine_version="1",
        )


@dataclass
class StructuredOcr:
    words: tuple[OcrWord, ...]
    text: str

    async def recognize(self, data: bytes, *, language: str | None = None) -> OcrResult:
        del language
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
        return OcrResult(
            text=self.text,
            words=self.words,
            width=width,
            height=height,
            language="eng",
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


async def test_valid_text_partly_outside_page_keeps_pdf_coordinates() -> None:
    parsed = await PdfParser().parse(
        _pdf(b"BT /F1 12 Tf -5 720 Td (Edge text) Tj ET"), ParseContext()
    )

    assert "Edge text" in parsed.markdown
    assert parsed.blocks[0].bbox is not None
    assert parsed.blocks[0].bbox[0] < 0


async def test_low_density_pdf_routes_page_to_ocr() -> None:
    """FR-F-03: low-density pages are rendered and OCRed automatically."""
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr, limits=PdfLimits(ocr_text_threshold=100)).parse(
        _image_pdf(), ParseContext(language="en")
    )

    assert ocr.calls == 1
    assert parsed.markdown == "Scanned page"
    assert parsed.metadata["ocr_pages"] == [1]
    assert parsed.blocks[0].page == 1
    assert parsed.blocks[0].bbox == (10.0, 762.0, 80.0, 782.0)


async def test_large_scan_with_native_header_routes_page_to_ocr() -> None:
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr).parse(
        _mixed_raster_text_pdf(
            image_draws=[(40, 40, 532, 680)],
            text_lines=[(72, 755, "NATIVE HEADER TEXT")],
        ),
        ParseContext(language="en"),
    )

    assert ocr.calls == 1
    assert parsed.markdown == "Scanned page"
    assert parsed.metadata["ocr_pages"] == [1]


async def test_large_scan_with_native_header_and_footer_routes_page_to_ocr() -> None:
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr).parse(
        _mixed_raster_text_pdf(
            image_draws=[(40, 40, 532, 680)],
            text_lines=[
                (72, 755, "NATIVE HEADER TEXT"),
                (72, 20, "NATIVE FOOTER TEXT"),
            ],
        ),
        ParseContext(language="en"),
    )

    assert ocr.calls == 1
    assert parsed.markdown == "Scanned page"


async def test_native_text_with_small_logo_does_not_route_to_ocr() -> None:
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr).parse(
        _mixed_raster_text_pdf(
            image_draws=[(520, 730, 40, 40)],
            text_lines=[
                (60, 700, "Ordinary native paragraph one"),
                (60, 650, "Ordinary native paragraph two"),
            ],
        ),
        ParseContext(language="en"),
    )

    assert ocr.calls == 0
    assert "Ordinary native paragraph" in parsed.markdown


async def test_full_ocr_text_layer_over_large_image_does_not_route_to_ocr() -> None:
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr).parse(
        _mixed_raster_text_pdf(
            image_draws=[(40, 40, 532, 680)],
            text_lines=[
                (70, 700, "Complete OCR line one"),
                (70, 580, "Complete OCR line two"),
                (70, 460, "Complete OCR line three"),
                (70, 340, "Complete OCR line four"),
                (70, 220, "Complete OCR line five"),
                (70, 100, "Complete OCR line six"),
            ],
        ),
        ParseContext(language="en"),
    )

    assert ocr.calls == 0
    assert "Complete OCR line six" in parsed.markdown


async def test_overlapping_rasters_use_union_coverage_not_summed_area() -> None:
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr).parse(
        _mixed_raster_text_pdf(
            image_draws=[(0, 40, 250, 680), (50, 40, 250, 680)],
            text_lines=[(72, 755, "Native document heading")],
        ),
        ParseContext(language="en"),
    )

    assert ocr.calls == 0
    assert parsed.markdown == "Native document heading"


async def test_large_scan_nested_in_form_routes_page_to_ocr() -> None:
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr).parse(
        _form_raster_text_pdf(image_draw=(40, 40, 532, 680)),
        ParseContext(language="en"),
    )

    assert ocr.calls == 1
    assert parsed.markdown == "Scanned page"


async def test_small_logo_nested_in_form_does_not_route_page_to_ocr() -> None:
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr).parse(
        _form_raster_text_pdf(image_draw=(520, 730, 40, 40)),
        ParseContext(language="en"),
    )

    assert ocr.calls == 0
    assert parsed.markdown == "NATIVE HEADER TEXT"


async def test_text_density_ignores_pdf_whitespace_characters() -> None:
    ocr = RecordingOcr()
    parsed = await PdfParser(ocr=ocr, limits=PdfLimits(ocr_text_threshold=1)).parse(
        _text_pdf("   "), ParseContext(language="en")
    )

    assert ocr.calls == 1
    assert parsed.markdown == "Scanned page"


async def test_short_text_only_page_does_not_spuriously_require_ocr() -> None:
    parsed = await PdfParser(limits=PdfLimits(ocr_text_threshold=100)).parse(
        _text_pdf("Preface"), ParseContext(language="en")
    )

    assert parsed.markdown == "Preface"
    assert parsed.metadata["ocr_pages"] == []


async def test_rotated_page_ocr_bbox_is_converted_back_to_pdf_point_space() -> None:
    rotated = _image_pdf(rotation=90)
    parsed = await PdfParser(ocr=ImageSizedOcr(), limits=PdfLimits(ocr_text_threshold=100)).parse(
        rotated, ParseContext(language="en")
    )

    assert parsed.markdown == "Rotated scan"
    assert parsed.blocks[0].bbox == pytest.approx((0.0, 0.0, 306.0, 396.0))


async def test_cropped_page_ocr_bbox_includes_pdf_crop_origin() -> None:
    cropped = _image_pdf().replace(
        b"/MediaBox [0 0 612 792]",
        b"/MediaBox [0 0 612 792] /CropBox [50 100 550 700]",
    )
    parsed = await PdfParser(ocr=ImageSizedOcr(), limits=PdfLimits(ocr_text_threshold=100)).parse(
        cropped, ParseContext(language="en")
    )

    assert parsed.blocks[0].bbox == pytest.approx((50.0, 400.0, 300.0, 700.0))


async def test_ocr_words_are_laid_out_as_two_columns_in_a_child_process() -> None:
    parsed = await PdfParser(
        ocr=StructuredOcr(
            text="L1\nL2\nR1\nR2",
            words=(
                OcrWord("L1", 99, (40, 100, 80, 120)),
                OcrWord("L2", 99, (40, 140, 80, 160)),
                OcrWord("R1", 99, (700, 100, 740, 120)),
                OcrWord("R2", 99, (700, 140, 740, 160)),
            ),
        ),
        limits=PdfLimits(ocr_text_threshold=100),
    ).parse(_image_pdf(), ParseContext(source_url="scan:two-columns", language="en"))

    assert [block.text for block in parsed.blocks] == ["L1 L2", "R1 R2"]
    assert len({block.bbox for block in parsed.blocks}) == 2
    assert all(block.source_url == "scan:two-columns" for block in parsed.blocks)
    assert parsed.metadata["pdf_worker_pid"] != os.getpid()


async def test_ocr_words_can_form_a_simple_table() -> None:
    parsed = await PdfParser(
        ocr=StructuredOcr(
            text="Name Amount\nA 42",
            words=(
                OcrWord("Name", 99, (40, 100, 100, 120)),
                OcrWord("Amount", 99, (300, 100, 370, 120)),
                OcrWord("A", 99, (40, 140, 60, 160)),
                OcrWord("42", 99, (300, 140, 330, 160)),
            ),
        ),
        limits=PdfLimits(ocr_text_threshold=100),
    ).parse(_image_pdf(), ParseContext(language="en"))

    assert len(parsed.blocks) == 1
    assert parsed.blocks[0].kind == "table"
    assert parsed.blocks[0].text == "| Name | Amount |\n| --- | --- |\n| A | 42 |"


@pytest.mark.parametrize(
    ("words", "reason"),
    [
        ((), "no_words"),
        ((OcrWord("Complete", 99, (40, 100, 120, 120)),), "incomplete_words"),
    ],
)
async def test_incomplete_ocr_words_preserve_authoritative_full_text(
    words: tuple[OcrWord, ...], reason: str
) -> None:
    parsed = await PdfParser(
        ocr=StructuredOcr(words=words, text="Complete body text"),
        limits=PdfLimits(ocr_text_threshold=100),
    ).parse(_image_pdf(), ParseContext(language="en"))

    assert parsed.markdown == "Complete body text"
    assert len(parsed.blocks) == 1
    assert parsed.metadata["ocr_layout_unavailable_pages"] == [1]
    assert parsed.metadata["ocr_layout_unavailable_reasons"] == {"1": reason}


async def test_invalid_ocr_word_geometry_is_not_downgraded_to_fallback() -> None:
    with pytest.raises(PdfCorruptFile):
        await PdfParser(
            ocr=StructuredOcr(
                words=(OcrWord("Complete", 99, (-1, 100, 120, 120)),),
                text="Complete",
            ),
            limits=PdfLimits(ocr_text_threshold=100),
        ).parse(_image_pdf(), ParseContext(language="en"))


async def test_ocr_layout_path_preserves_page_character_limit() -> None:
    with pytest.raises(PdfTooLarge):
        await PdfParser(
            ocr=StructuredOcr(
                words=(OcrWord("ABCDEFGHIJ", 99, (40, 100, 140, 120)),),
                text="ABCDEFGHIJ",
            ),
            limits=PdfLimits(ocr_text_threshold=100, max_page_chars=5),
        ).parse(_image_pdf(), ParseContext(language="en"))


async def test_ocr_layout_path_preserves_word_count_limit() -> None:
    with pytest.raises(PdfTooLarge):
        await PdfParser(
            ocr=StructuredOcr(
                words=(
                    OcrWord("A", 99, (40, 100, 60, 120)),
                    OcrWord("B", 99, (70, 100, 90, 120)),
                ),
                text="A B",
            ),
            limits=PdfLimits(ocr_text_threshold=100, max_layout_words=1),
        ).parse(_image_pdf(), ParseContext(language="en"))


async def test_ocr_layout_rejects_invalid_result_dimensions() -> None:
    class InvalidDimensionsOcr:
        async def recognize(self, data: bytes, *, language: str | None = None) -> OcrResult:
            del data, language
            return OcrResult(
                text="A",
                words=(OcrWord("A", 99, (0, 0, 0, 0)),),
                width=0,
                height=100,
                language="eng",
                engine="bad",
                engine_version="1",
            )

    with pytest.raises(PdfCorruptFile):
        await PdfParser(ocr=InvalidDimensionsOcr(), limits=PdfLimits(ocr_text_threshold=100)).parse(
            _image_pdf(), ParseContext(language="en")
        )


async def test_ocr_layout_rejects_worker_bbox_outside_visual_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn.ingestion.pdf as pdf_module

    original = pdf_module.run_bounded

    async def malformed_layout(command, data, **kwargs):
        if command[3] == "layout":
            return (
                0,
                json.dumps(
                    {"blocks": [{"kind": "paragraph", "text": "A", "bbox": [-1, 0, 1, 1]}]}
                ).encode(),
                b"",
            )
        return await original(command, data, **kwargs)

    monkeypatch.setattr(pdf_module, "run_bounded", malformed_layout)
    with pytest.raises(PdfCorruptFile):
        await PdfParser(
            ocr=StructuredOcr(
                words=(OcrWord("A", 99, (40, 100, 60, 120)),),
                text="A",
            ),
            limits=PdfLimits(ocr_text_threshold=100),
        ).parse(_image_pdf(), ParseContext(language="en"))


@pytest.mark.parametrize("rotation", [90, 270])
async def test_rotated_cropped_ocr_layout_uses_visual_order_then_pdf_citations(
    rotation: int,
) -> None:
    pdf = _image_pdf(rotation=rotation).replace(
        b"/MediaBox [0 0 612 792]",
        b"/MediaBox [0 0 612 792] /CropBox [50 100 550 700]",
    )
    parsed = await PdfParser(
        ocr=StructuredOcr(
            text="A1 A2\nB1 B2",
            words=(
                OcrWord("A1", 99, (100, 100, 140, 120)),
                OcrWord("A2", 99, (150, 100, 190, 120)),
                OcrWord("B1", 99, (100, 200, 140, 220)),
                OcrWord("B2", 99, (150, 200, 190, 220)),
            ),
        ),
        limits=PdfLimits(ocr_text_threshold=100),
    ).parse(pdf, ParseContext(language="en"))

    assert [block.text for block in parsed.blocks] == ["A1 A2", "B1 B2"]
    assert all(block.bbox is not None for block in parsed.blocks)
    assert all(50 <= value <= 700 for block in parsed.blocks for value in block.bbox or ())


async def test_low_density_pdf_without_engine_is_explicit() -> None:
    """FR-F-03: scans fail explicitly when OCR is not configured."""
    with pytest.raises(PdfOcrRequired) as caught:
        await PdfParser(limits=PdfLimits(ocr_text_threshold=100)).parse(
            _image_pdf(), ParseContext()
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
    """FR-F-01: only PDFium's password error is classified as encryption."""
    import cairn.ingestion.pdf as pdf_module

    async def encrypted_worker(*args: object, **kwargs: object) -> tuple[int, bytes, bytes]:
        del args, kwargs
        return 2, b"", json.dumps({"code": "encrypted"}).encode()

    original = pdf_module.run_bounded
    pdf_module.run_bounded = encrypted_worker
    try:
        with pytest.raises(PdfEncrypted) as caught:
            await PdfParser().parse(_text_pdf(), ParseContext())
    finally:
        pdf_module.run_bounded = original
    assert caught.value.code == "PDF_ENCRYPTED"


async def test_encrypt_token_in_malformed_bytes_is_not_treated_as_encrypted() -> None:
    data = b"%PDF-1.7\n1 0 obj << /Encrypt 2 0 R >> endobj\n%%EOF"
    with pytest.raises(PdfCorruptFile):
        await PdfParser().parse(data, ParseContext())


async def test_multi_page_text_preserves_source_page_and_order() -> None:
    parsed = await PdfParser().parse(
        _two_page_pdf(), ParseContext(source_url="source:two-pages", language="en")
    )

    assert parsed.pages == 2
    assert [block.page for block in parsed.blocks] == [1, 2]
    assert [block.ordinal for block in parsed.blocks] == [0, 1]
    assert [block.source_url for block in parsed.blocks] == [
        "source:two-pages",
        "source:two-pages",
    ]
    assert parsed.markdown.index("First page") < parsed.markdown.index("Second page")


async def test_structurally_blank_page_is_counted_without_requiring_ocr() -> None:
    parsed = await PdfParser().parse(_blank_cover_pdf(), ParseContext())

    assert parsed.pages == 2
    assert [block.page for block in parsed.blocks] == [2]
    assert parsed.markdown == "Page after blank cover"


@pytest.mark.parametrize(
    ("limits", "data"),
    [
        (PdfLimits(max_pages=1), _two_page_pdf()),
        (PdfLimits(max_page_width_points=100), _text_pdf()),
        (PdfLimits(max_page_chars=5), _text_pdf()),
        (PdfLimits(max_layout_words=1), _text_pdf()),
        (PdfLimits(max_output_bytes=32), _text_pdf()),
    ],
)
async def test_pdf_worker_resource_bounds_are_stable(limits: PdfLimits, data: bytes) -> None:
    with pytest.raises(PdfTooLarge) as caught:
        await PdfParser(limits=limits).parse(data, ParseContext())
    assert caught.value.code == "PDF_TOO_LARGE"


async def test_render_pixel_bound_is_checked_before_ocr() -> None:
    ocr = RecordingOcr()
    parser = PdfParser(
        ocr=ocr,
        limits=PdfLimits(ocr_text_threshold=100, max_page_pixels=100),
    )
    with pytest.raises(PdfTooLarge):
        await parser.parse(_image_pdf(), ParseContext())
    assert ocr.calls == 0


async def test_rendered_page_output_is_bounded() -> None:
    parser = PdfParser(
        ocr=RecordingOcr(),
        limits=PdfLimits(ocr_text_threshold=100, max_render_bytes=64),
    )
    with pytest.raises(PdfTooLarge):
        await parser.parse(_image_pdf(), ParseContext())


async def test_missing_pdf_engine_has_stable_sanitized_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cairn.ingestion.pdf as pdf_module

    async def unavailable_worker(*args: object, **kwargs: object) -> tuple[int, bytes, bytes]:
        del args, kwargs
        return 5, b"", b'{"code":"unavailable"}'

    monkeypatch.setattr(pdf_module, "run_bounded", unavailable_worker)
    with pytest.raises(PdfEngineUnavailable) as caught:
        await PdfParser().parse(_text_pdf(), ParseContext())
    assert caught.value.code == "PDF_ENGINE_UNAVAILABLE"


async def test_parse_deadline_maps_to_stable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn.ingestion.pdf as pdf_module

    async def never_finishes(*args: object, **kwargs: object) -> tuple[int, bytes, bytes]:
        del args, kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(pdf_module, "run_bounded", never_finishes)
    with pytest.raises(PdfTimedOut) as caught:
        await PdfParser(limits=PdfLimits(timeout_seconds=0.01)).parse(_text_pdf(), ParseContext())
    assert caught.value.code == "PDF_TIMED_OUT"


async def test_parser_cancellation_is_not_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    import cairn.ingestion.pdf as pdf_module

    entered = asyncio.Event()

    async def never_finishes(*args: object, **kwargs: object) -> tuple[int, bytes, bytes]:
        del args, kwargs
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(pdf_module, "run_bounded", never_finishes)
    task = asyncio.create_task(PdfParser().parse(_text_pdf(), ParseContext()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_direct_pdf_parser_enforces_its_own_limits() -> None:
    """FR-F-01: direct adapter calls cannot bypass PDF resource limits."""
    with pytest.raises(PdfTooLarge) as caught:
        await PdfParser(limits=PdfLimits(max_input_bytes=10)).parse(_text_pdf(), ParseContext())
    assert caught.value.code == "PDF_TOO_LARGE"


def test_invalid_pdf_limits_are_rejected() -> None:
    """FR-F-01/03: invalid PDF resource limits fail during construction."""
    with pytest.raises(ValueError, match="positive"):
        PdfLimits(max_pages=0)
    with pytest.raises(ValueError, match="integer"):
        PdfLimits(max_pages=1.5)  # type: ignore[arg-type]
