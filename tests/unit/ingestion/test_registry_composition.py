"""FR-D-03/FR-F-04: production parser composition includes Office and language detection."""

import pytest

from cairn.ingestion.base import ParseContext
from cairn.ingestion.registry import get_parser_registry


def test_default_registry_includes_office_formats() -> None:
    """FR-D-03: Office adapters are reachable through the production MIME dispatcher."""
    assert {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/csv",
    } <= get_parser_registry().supported_mimes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "This document describes the knowledge base ingestion process. Uploaded files "
            "are parsed into paragraphs and tables before their searchable content is indexed. "
            "The source document and page references are preserved throughout processing.",
            "en",
        ),
        (
            "知识库文档处理流程首先提取文档中的段落和表格。然后按照语义结构进行分块。"
            "每一个分块都需要保留原始文档的来源和页码。方便用户在检索结果中核对引用信息。",
            "zh",
        ),
    ],
)
async def test_registry_detects_language_without_explicit_hint(text: str, expected: str) -> None:
    """FR-F-04: real parser output receives a detected language before worker persistence."""
    parsed = await get_parser_registry().parse(text.encode(), mime="text/plain", ctx=ParseContext())
    assert parsed.language == expected


async def test_registry_does_not_override_explicit_language() -> None:
    """FR-F-04: explicit source language remains authoritative over statistical detection."""
    parsed = await get_parser_registry().parse(
        b"This is a long English paragraph describing the complete document ingestion process.",
        mime="text/plain",
        ctx=ParseContext(language="de"),
    )
    assert parsed.language == "de"


async def test_registry_csv_parser_preserves_header_and_source() -> None:
    """FR-D-03/FR-F-07: registry-dispatched CSV retains table structure and provenance."""
    parsed = await get_parser_registry().parse(
        b"name,value\nalpha,42\n", mime="text/csv", ctx=ParseContext(source_url="source:csv")
    )
    assert parsed.blocks[0].kind == "table"
    assert "name" in parsed.blocks[0].text and "42" in parsed.blocks[0].text
    assert parsed.blocks[0].source_url == "source:csv"
