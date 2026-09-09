"""Explicit MIME dispatch; unsupported formats never degrade to plain text."""

from __future__ import annotations

from cairn.ingestion.base import ParseContext, ParsedDocument, Parser
from cairn.ingestion.errors import ParseEmptyContent, ParseTooLarge, ParseUnsupportedMime
from cairn.ingestion.language import resolve_language
from cairn.ingestion.office import CsvParser, DocxParser, PptxParser, XlsxParser
from cairn.ingestion.parsers import HtmlParser, JsonParser, MarkdownParser, TextParser


def _normalize_mime(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower()


class ParserRegistry:
    def __init__(self, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._parsers: dict[str, Parser] = {}

    @property
    def supported_mimes(self) -> frozenset[str]:
        return frozenset(self._parsers)

    def register(self, parser: Parser) -> None:
        mimes = {_normalize_mime(mime) for mime in parser.supported_mimes}
        if not mimes or any("/" not in mime for mime in mimes):
            raise ValueError("A parser must declare valid MIME types")
        if mimes & self._parsers.keys():
            raise ValueError("A parser is already registered for this MIME type")
        self._parsers.update(dict.fromkeys(mimes, parser))

    async def parse(self, data: bytes, *, mime: str, ctx: ParseContext) -> ParsedDocument:
        if len(data) > self._max_bytes:
            raise ParseTooLarge()
        parser = self._parsers.get(_normalize_mime(mime))
        if parser is None:
            raise ParseUnsupportedMime()
        if not data:
            raise ParseEmptyContent()
        parsed = await parser.parse(data, ctx)
        if not parsed.markdown.strip() or not parsed.blocks:
            raise ParseEmptyContent()
        language = await resolve_language(
            parsed.markdown,
            explicit=ctx.language if ctx.language != "und" else parsed.language,
        )
        parsed.language = language.language
        return parsed


def get_parser_registry() -> ParserRegistry:
    registry = ParserRegistry()
    for parser in (
        TextParser(),
        MarkdownParser(),
        JsonParser(),
        HtmlParser(),
        DocxParser(),
        PptxParser(),
        XlsxParser(),
        CsvParser(),
    ):
        registry.register(parser)
    return registry
