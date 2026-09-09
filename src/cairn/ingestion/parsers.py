"""Offline text parsers; CPU-bound extraction runs outside the event loop."""

from __future__ import annotations

import json
import math
import re
from typing import ClassVar

from anyio import to_thread
from markdown_it import MarkdownIt
from trafilatura import extract

from cairn.ingestion.base import Block, BlockKind, ParseContext, ParsedDocument
from cairn.ingestion.errors import ParseCorruptFile, ParseEmptyContent


def _decode(data: bytes, ctx: ParseContext) -> str:
    try:
        text = data.decode(ctx.encoding).removeprefix("\ufeff")
    except (UnicodeError, LookupError) as exc:
        raise ParseCorruptFile() from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        raise ParseEmptyContent()
    if "\x00" in text:
        raise ParseCorruptFile()
    return text


def _plain_document(text: str, ctx: ParseContext) -> ParsedDocument:
    blocks = [
        Block(kind="paragraph", text=part, ordinal=ordinal, source_url=ctx.source_url)
        for ordinal, part in enumerate(re.split(r"\n\s*\n", text))
    ]
    return ParsedDocument(markdown=text, blocks=blocks, language=ctx.language)


def _markdown_document(text: str, ctx: ParseContext) -> ParsedDocument:
    tokens = MarkdownIt("commonmark").enable("table").parse(text)
    lines = text.split("\n")
    blocks: list[Block] = []
    headings: list[tuple[int, str]] = []
    quote_headings: list[list[tuple[int, str]]] = []
    consumed_until = 0
    kinds: dict[str, BlockKind] = {
        "heading_open": "heading",
        "paragraph_open": "paragraph",
        "table_open": "table",
        "bullet_list_open": "list",
        "ordered_list_open": "list",
        "fence": "code",
        "code_block": "code",
        "html_block": "code",
    }
    for index, token in enumerate(tokens):
        if token.type == "blockquote_open":
            quote_headings.append(headings.copy())
        elif token.type == "blockquote_close":
            headings = quote_headings.pop()
        kind = kinds.get(token.type)
        if kind is None or token.map is None or token.map[0] < consumed_until:
            continue
        start, end = token.map
        consumed_until = end
        level = None
        if kind == "heading":
            level = int(token.tag[1:])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, tokens[index + 1].content))
        blocks.append(
            Block(
                kind=kind,
                text="\n".join(lines[start:end]).rstrip(),
                ordinal=len(blocks),
                heading_path=tuple(title for _, title in headings),
                heading_level=level,
                source_url=ctx.source_url,
            )
        )
    if not blocks:
        raise ParseEmptyContent()
    return ParsedDocument(markdown=text, blocks=blocks, language=ctx.language)


class TextParser:
    supported_mimes: ClassVar[frozenset[str]] = frozenset({"text/plain"})

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        return await to_thread.run_sync(self._parse, data, ctx)

    def _parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        return _plain_document(_decode(data, ctx).strip(), ctx)


class MarkdownParser:
    supported_mimes: ClassVar[frozenset[str]] = frozenset({"text/markdown", "text/x-markdown"})

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        return await to_thread.run_sync(self._parse, data, ctx)

    def _parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        return _markdown_document(_decode(data, ctx), ctx)


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON numbers are not supported")


def _parse_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON numbers are not supported")
    return number


class JsonParser:
    supported_mimes: ClassVar[frozenset[str]] = frozenset({"application/json"})

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        return await to_thread.run_sync(self._parse, data, ctx)

    def _parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        text = _decode(data, ctx)
        try:
            value = json.loads(text, parse_constant=_reject_constant, parse_float=_parse_float)
            rendered = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
            rendered.encode("utf-8")
            if ctx.json_fields:
                if not isinstance(value, dict):
                    raise ParseCorruptFile("Field extraction requires a JSON object.")
                parts: list[str] = []
                for name in ctx.json_fields:
                    field = value.get(name)
                    if field is None:
                        continue
                    part = (
                        field if isinstance(field, str) else json.dumps(field, ensure_ascii=False)
                    )
                    if part.strip():
                        parts.append(part.strip())
                if not parts:
                    raise ParseEmptyContent()
                return _plain_document("\n\n".join(parts), ctx)
        except (ValueError, RecursionError) as exc:
            raise ParseCorruptFile() from exc
        fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", rendered)), default=0))
        markdown = f"{fence}json\n{rendered}\n{fence}"
        block = Block(kind="code", text=markdown, ordinal=0, source_url=ctx.source_url)
        return ParsedDocument(markdown=markdown, blocks=[block], language=ctx.language)


class HtmlParser:
    supported_mimes: ClassVar[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        return await to_thread.run_sync(self._parse, data, ctx)

    def _parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        text = _decode(data, ctx)
        markdown = extract(
            text,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=False,
            include_formatting=True,
            favor_precision=True,
        )
        if not markdown or not markdown.strip():
            raise ParseEmptyContent()
        return _markdown_document(markdown.strip(), ctx)
