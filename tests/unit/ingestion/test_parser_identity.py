from uuid import uuid4

import pytest

from cairn.ingestion.base import Block, ParseContext, ParsedDocument
from cairn.ingestion.ir import bind_document, project_document
from cairn.ingestion.parsers import TextParser
from cairn.ingestion.pdf import PdfLimits, PdfParser
from cairn.ingestion.registry import ParserDescriptor, ParserRegistry, context_fingerprint


def test_descriptor_is_optional_and_immutable_identity_is_canonical():
    registry = ParserRegistry()
    registry.register(TextParser())
    assert registry.describe("text/plain") is None
    other = ParserRegistry()
    descriptor = ParserDescriptor(implementation="text/1", options={"b": 2, "a": 1})
    other.register(TextParser(), descriptor=descriptor)
    assert other.describe("text/plain; charset=utf-8") == descriptor
    assert (
        descriptor.fingerprint
        == ParserDescriptor(implementation="text/1", options={"a": 1, "b": 2}).fingerprint
    )
    assert context_fingerprint(ParseContext()) != context_fingerprint(ParseContext(language="zh"))


@pytest.mark.asyncio
async def test_undescribed_custom_parser_still_parses():
    registry = ParserRegistry()
    registry.register(TextParser())
    assert (
        await registry.parse(b"hello", mime="text/plain", ctx=ParseContext())
    ).markdown == "hello"


def test_cached_ir_cannot_bypass_lowered_pdf_limits():
    ir = bind_document(
        ParsedDocument("long text", [Block("paragraph", "long text", 0)]),
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )
    registry = ParserRegistry()
    registry.register(PdfParser(limits=PdfLimits(max_text_chars=3)))
    assert not registry.accepts_cached(
        project_document(ir), mime="application/pdf", source_bytes=10
    )


def test_ocr_pixel_budget_cannot_be_bypassed_by_ir_cache():
    ir = bind_document(
        ParsedDocument(
            "scan",
            [Block("paragraph", "scan", 0)],
            metadata={
                "format": "pdf",
                "ocr_pages": [1],
                "pdf_pages": [{"width": 600, "height": 800}],
            },
        ),
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )
    registry = ParserRegistry()
    registry.register(PdfParser(limits=PdfLimits(max_page_pixels=1)))
    assert not registry.accepts_cached(
        project_document(ir), mime="application/pdf", source_bytes=10
    )


@pytest.mark.parametrize(
    "bound,fact,value",
    [
        ("max_page_chars", "characters", 2),
        ("max_layout_words", "words", 2),
        ("max_page_objects", "objects", 2),
        ("max_raster_rectangles", "rasters", 2),
        ("max_page_pixels", "render_pixels", 2),
        ("max_render_bytes", "render_bytes", 2),
        ("max_output_bytes", None, 2),
    ],
)
def test_cache_rejects_lowered_applicable_budget(bound, fact, value):
    facts = {
        "schema": 1,
        "output_bytes": 2,
        "pages": [
            {
                "width": 100,
                "height": 100,
                "characters": 0,
                "words": 0,
                "objects": 0,
                "rasters": 0,
                "layout_blocks": 1,
                "render_pixels": 0,
                "render_bytes": 0,
            }
        ],
    }
    if fact:
        facts["pages"][0][fact] = value
    ir = bind_document(
        ParsedDocument(
            "x", [Block("paragraph", "x", 0)], metadata={"format": "pdf", "pdf_budget_facts": facts}
        ),
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )
    registry = ParserRegistry()
    registry.register(PdfParser(limits=PdfLimits(**{bound: 1})))
    assert not registry.accepts_cached(project_document(ir), mime="application/pdf", source_bytes=1)


def test_native_cache_without_render_does_not_use_page_area_as_pixels():
    facts = {
        "schema": 1,
        "output_bytes": 2,
        "pages": [
            {
                "width": 600,
                "height": 800,
                "characters": 1,
                "words": 1,
                "objects": 1,
                "rasters": 0,
                "layout_blocks": 1,
                "render_pixels": 0,
                "render_bytes": 0,
            }
        ],
    }
    ir = bind_document(
        ParsedDocument(
            "x", [Block("paragraph", "x", 0)], metadata={"format": "pdf", "pdf_budget_facts": facts}
        ),
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )
    registry = ParserRegistry()
    registry.register(PdfParser(limits=PdfLimits(max_page_pixels=1)))
    assert registry.accepts_cached(project_document(ir), mime="application/pdf", source_bytes=1)


def test_valid_ocr_dimensions_over_budget_are_too_large():
    from cairn.ingestion.ocr import OcrResult
    from cairn.ingestion.pdf import PdfTooLarge

    parser = PdfParser(limits=PdfLimits(max_page_pixels=1))
    with pytest.raises(PdfTooLarge):
        parser._validate_ocr_result(
            OcrResult(
                text="A",
                words=(),
                width=2,
                height=2,
                language="eng",
                engine="fixture",
                engine_version="1",
            )
        )


@pytest.mark.parametrize("text,limit", [("A B", 2), ("abc", 2)])
def test_emitted_source_text_cannot_bypass_page_character_limit(text, limit):
    from cairn.core.document_ir import Element, Location
    from cairn.ingestion.pdf import PdfTooLarge
    from cairn.ingestion.pdf_budget import validate_pdf_budget

    parsed = ParsedDocument(
        text,
        [],
        elements=[
            Element(
                local_id="t",
                order=0,
                type="text",
                text=text,
                locations=[Location(page=1, width=100, height=100, bbox=(0, 0, 10, 10))],
            )
        ],
        metadata={
            "pdf_budget_facts": {
                "schema": 1,
                "output_bytes": 10,
                "pages": [
                    {
                        "width": 100,
                        "height": 100,
                        "characters": 1,
                        "words": 1,
                        "objects": 1,
                        "rasters": 0,
                        "layout_blocks": 1,
                        "render_pixels": 0,
                        "render_bytes": 0,
                    }
                ],
            }
        },
    )
    with pytest.raises(PdfTooLarge):
        validate_pdf_budget(parsed, PdfLimits(max_page_chars=limit))
