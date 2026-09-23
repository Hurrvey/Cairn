from __future__ import annotations

import asyncio
import json

import pytest

from cairn.ingestion.base import Block, ParseContext, ParsedDocument, Parser
from cairn.ingestion.errors import (
    ParseCorruptFile,
    ParseEmptyContent,
    ParseTooLarge,
    ParseUnsupportedMime,
)
from cairn.ingestion.parsers import HtmlParser, JsonParser, MarkdownParser, TextParser
from cairn.ingestion.registry import ParserRegistry, get_parser_registry


@pytest.mark.parametrize("mime", ["text/markdown", "TEXT/MARKDOWN; charset=utf-8"])
async def test_registry_normalizes_mime_and_preserves_context(mime: str) -> None:
    parsed = await get_parser_registry().parse(
        "# 知识库\n\n你好。".encode(),
        mime=mime,
        ctx=ParseContext(source_url="https://example.org/docs", language="zh"),
    )
    assert parsed.language == "zh"
    assert parsed.blocks[1].heading_path == ("知识库",)
    assert parsed.blocks[1].source_url == "https://example.org/docs"
    assert parsed.blocks[1].page is None


async def test_registry_rejects_unsupported_mime_without_fallback() -> None:
    with pytest.raises(ParseUnsupportedMime) as caught:
        await get_parser_registry().parse(
            b"unsupported", mime="application/x-unsupported", ctx=ParseContext()
        )
    assert caught.value.code == "PARSE_UNSUPPORTED_MIME"
    assert caught.value.retryable is False


def test_registration_is_atomic_and_cannot_silently_replace_parser() -> None:
    registry = ParserRegistry()
    registry.register(TextParser())

    class ConflictingParser(TextParser):
        supported_mimes = frozenset({"text/markdown", "text/plain"})

    with pytest.raises(ValueError, match="already registered"):
        registry.register(ConflictingParser())
    assert registry.supported_mimes == frozenset({"text/plain"})
    registry.register(MarkdownParser())
    assert "text/markdown" in registry.supported_mimes


async def test_size_limit_is_checked_before_dispatch() -> None:
    registry = ParserRegistry(max_bytes=4)
    registry.register(TextParser())
    with pytest.raises(ParseTooLarge):
        await registry.parse(b"12345", mime="text/plain", ctx=ParseContext())
    assert (await registry.parse(b"1234", mime="text/plain", ctx=ParseContext())).markdown == "1234"


@pytest.mark.parametrize("max_bytes", [0, -1])
def test_registry_rejects_invalid_limit(max_bytes: int) -> None:
    with pytest.raises(ValueError):
        ParserRegistry(max_bytes=max_bytes)


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown", "application/json", "text/html"])
async def test_empty_input_has_a_stable_nonretryable_error(mime: str) -> None:
    with pytest.raises(ParseEmptyContent) as caught:
        await get_parser_registry().parse(b" \r\n\t", mime=mime, ctx=ParseContext())
    assert caught.value.code == "PARSE_EMPTY_CONTENT"
    assert caught.value.retryable is False


@pytest.mark.parametrize("parser", [TextParser(), MarkdownParser(), JsonParser(), HtmlParser()])
async def test_invalid_encoding_is_not_silently_replaced(parser: Parser) -> None:
    with pytest.raises(ParseCorruptFile):
        await parser.parse(b"\xff\xfe\xff", ParseContext())


async def test_plain_text_is_not_interpreted_as_markdown() -> None:
    parsed = await TextParser().parse(b"\xef\xbb\xbf# literal\r\n\r\n* literal", ParseContext())
    assert parsed.markdown == "# literal\n\n* literal"
    assert [block.kind for block in parsed.blocks] == ["paragraph", "paragraph"]
    assert all(not block.heading_path for block in parsed.blocks)
    assert parsed.pages == 1
    assert parsed.language == "und"
    assert parsed.assets == []


async def test_explicit_encoding() -> None:
    parsed = await TextParser().parse("中文".encode("gb18030"), ParseContext(encoding="gb18030"))
    assert parsed.markdown == "中文"
    with pytest.raises(ParseCorruptFile):
        await TextParser().parse(b"content", ParseContext(encoding="not-an-encoding"))


async def test_markdown_keeps_tables_code_and_nested_lists_atomic() -> None:
    markdown = (
        "# Root\n\nIntro\n\n### Deep\n\n"
        "| Name | Value |\n| --- | --- |\n| 中文 | 42 |\n\n"
        "- first\n  - nested\n- second\n\n"
        "```python\n# not a heading\nprint('hi')\n```\n\n"
        "## Sibling\n\nTail\n"
    )
    parsed = await MarkdownParser().parse(markdown.encode(), ParseContext())
    assert parsed.markdown == markdown.strip()
    assert [block.kind for block in parsed.blocks] == [
        "heading",
        "paragraph",
        "heading",
        "table",
        "list",
        "code",
        "heading",
        "paragraph",
    ]
    assert parsed.blocks[3].heading_path == ("Root", "Deep")
    assert "| 中文 | 42 |" in parsed.blocks[3].text
    assert "nested" in parsed.blocks[4].text
    assert "# not a heading" in parsed.blocks[5].text
    assert parsed.blocks[-1].heading_path == ("Root", "Sibling")
    assert [block.ordinal for block in parsed.blocks] == list(range(8))


async def test_setext_headings_and_new_root() -> None:
    parsed = await MarkdownParser().parse(
        b"First\n=====\n\n## Child\n\nBody\n\nSecond\n======\n\nEnd", ParseContext()
    )
    assert parsed.blocks[0].heading_level == 1
    assert parsed.blocks[2].heading_path == ("First", "Child")
    assert parsed.blocks[-1].heading_path == ("Second",)


async def test_json_is_validated_and_serialized_without_losing_unicode() -> None:
    parsed = await JsonParser().parse('{"title":"中文","count":42}'.encode(), ParseContext())
    assert "中文" in parsed.markdown
    assert parsed.blocks[0].kind == "code"
    assert json.loads(parsed.blocks[0].text.removeprefix("```json\n").removesuffix("\n```")) == {
        "title": "中文",
        "count": 42,
    }


async def test_json_fields_extract_text_in_requested_order() -> None:
    parsed = await JsonParser().parse(
        b'{"secret":"hidden","body":"Content","title":"Title"}',
        ParseContext(json_fields=("title", "body")),
    )
    assert [block.text for block in parsed.blocks] == ["Title", "Content"]
    assert "hidden" not in parsed.markdown


@pytest.mark.parametrize("data", [b"{broken}", b'{"value":NaN}', b'{"value":Infinity}'])
async def test_invalid_json_is_reported_without_leaking_content(data: bytes) -> None:
    with pytest.raises(ParseCorruptFile) as caught:
        await JsonParser().parse(data, ParseContext())
    assert data.decode() not in caught.value.detail


@pytest.mark.parametrize("data", [b"{}", b'{"body":null}', b'{"body":""}'])
async def test_missing_or_empty_json_fields_do_not_fall_back_to_all_content(data: bytes) -> None:
    with pytest.raises(ParseEmptyContent):
        await JsonParser().parse(data, ParseContext(json_fields=("body",)))


async def test_html_extracts_article_without_scripts_navigation_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def no_network(*args: object, **kwargs: object) -> None:
        pytest.fail("HTML parsing must never fetch a URL")

    monkeypatch.setattr(socket, "create_connection", no_network)
    paragraph = (
        "Knowledge belongs in an independent platform. Documents retain their source context. "
    )
    html = (
        "<html><head><title>Guide</title><script>SECRET_SCRIPT</script></head><body>"
        '<nav><a href="http://127.0.0.1/admin">SECRET_NAV</a></nav>'
        f"<article><h1>Guide</h1><p>{paragraph * 4}</p>"
        "<h2>Indexing</h2><p>Each document is parsed before indexing.</p>"
        '<img src="http://127.0.0.1/private"></article>'
        "<footer>SECRET_FOOTER</footer></body></html>"
    )
    parsed = await HtmlParser().parse(html.encode(), ParseContext())
    assert "Knowledge belongs" in parsed.markdown
    assert "SECRET_" not in parsed.markdown
    assert any(block.kind == "heading" for block in parsed.blocks)


async def test_metadata_and_layout_are_json_serializable_without_invented_coordinates() -> None:
    parsed = await MarkdownParser().parse(b"# Title\n\nBody", ParseContext())
    layout = parsed.layout()
    assert json.loads(json.dumps(layout)) == layout
    assert layout[1]["ordinal"] == 1
    assert layout[1]["heading_path"] == ["Title"]
    assert layout[1]["page"] is None
    assert layout[1]["bbox"] is None


async def test_registry_propagates_cancellation() -> None:
    class CancelledParser:
        supported_mimes = frozenset({"text/plain"})

        async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
            raise asyncio.CancelledError

    registry = ParserRegistry()
    registry.register(CancelledParser())
    with pytest.raises(asyncio.CancelledError):
        await registry.parse(b"content", mime="text/plain", ctx=ParseContext())


def test_document_defaults_are_not_shared() -> None:
    first = ParsedDocument(markdown="one", blocks=[Block(kind="paragraph", text="one", ordinal=0)])
    second = ParsedDocument(markdown="two", blocks=[])
    first.metadata["key"] = "value"
    assert second.metadata == {}


@pytest.mark.parametrize("fields", [(), ("value",)])
async def test_overflowing_json_numbers_are_rejected(fields: tuple[str, ...]) -> None:
    with pytest.raises(ParseCorruptFile):
        await JsonParser().parse(b'{"value":1e9999}', ParseContext(json_fields=fields))


async def test_json_fence_cannot_be_closed_by_document_content() -> None:
    parsed = await JsonParser().parse(b'{"value":"```"}', ParseContext())
    assert parsed.markdown.startswith("````json\n")
    assert parsed.markdown.endswith("\n````")


@pytest.mark.parametrize("data", [b'<script>alert("private")</script>', b"<p> </p>"])
async def test_html_with_no_extractable_text(data: bytes) -> None:
    with pytest.raises(ParseEmptyContent):
        await HtmlParser().parse(data, ParseContext())


async def test_unicode_separator_does_not_corrupt_markdown_source_maps() -> None:
    parsed = await MarkdownParser().parse(
        "# Root\n\nfirst\u2028second\n\n## Next\n\nTail".encode(), ParseContext()
    )
    assert [block.text for block in parsed.blocks] == [
        "# Root",
        "first\u2028second",
        "## Next",
        "Tail",
    ]


async def test_initial_indentation_remains_an_atomic_code_block() -> None:
    parsed = await MarkdownParser().parse(
        b"    # code, not heading\n    print(1)\n\nTail", ParseContext()
    )
    assert [block.kind for block in parsed.blocks] == ["code", "paragraph"]
    assert parsed.blocks[-1].heading_path == ()


async def test_quoted_headings_do_not_leak_into_document_context() -> None:
    parsed = await MarkdownParser().parse(
        b"# Root\n\n> ## Quoted\n> Quote\n>\n> > # Inner\n> > Nested\n>\n> After\n\nOutside",
        ParseContext(),
    )
    assert parsed.blocks[-1].text == "Outside"
    assert parsed.blocks[-1].heading_path == ("Root",)
    assert parsed.blocks[-2].heading_path == ("Root", "Quoted")


@pytest.mark.parametrize("fields", [(), ("body",)])
async def test_lone_json_surrogates_are_rejected(fields: tuple[str, ...]) -> None:
    with pytest.raises(ParseCorruptFile):
        await JsonParser().parse(b'{"body":"\\ud800"}', ParseContext(json_fields=fields))


async def test_encoded_text_is_not_classified_as_empty_before_decoding() -> None:
    parsed = await get_parser_registry().parse(
        "†".encode("utf-16-le"), mime="text/plain", ctx=ParseContext(encoding="utf-16-le")
    )
    assert parsed.markdown == "†"
