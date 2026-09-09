"""Parser extension contract and source-preserving intermediate representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal, Protocol

from pydantic import JsonValue

BlockKind = Literal["heading", "paragraph", "table", "list", "code", "image"]


@dataclass(frozen=True, slots=True)
class ParseContext:
    source_url: str | None = None
    language: str = "und"
    encoding: str = "utf-8"
    json_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Block:
    kind: BlockKind
    text: str
    ordinal: int
    heading_path: tuple[str, ...] = ()
    heading_level: int | None = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class Asset:
    name: str
    mime_type: str
    data: bytes = field(repr=False)
    page: int | None = None


@dataclass(slots=True)
class ParsedDocument:
    markdown: str
    blocks: list[Block]
    pages: int = 1
    language: str = "und"
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    assets: list[Asset] = field(default_factory=list)

    def layout(self) -> list[dict[str, JsonValue]]:
        """JSON-ready citations; absent page geometry remains explicitly unknown."""
        return [
            {
                "kind": block.kind,
                "text": block.text,
                "ordinal": block.ordinal,
                "heading_path": list(block.heading_path),
                "heading_level": block.heading_level,
                "page": block.page,
                "bbox": list(block.bbox) if block.bbox is not None else None,
                "source_url": block.source_url,
            }
            for block in self.blocks
        ]


class Parser(Protocol):
    supported_mimes: ClassVar[frozenset[str]]

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument: ...
