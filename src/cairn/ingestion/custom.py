"""Version-pinned custom chunk executor contract and untrusted-output validation."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol, cast
from uuid import UUID, uuid5

from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import ChunkSpec
from cairn.embedding.tokenizers import Tokenizer
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.errors import ChunkError

CUSTOM_MAX_CHUNKS = 10_000
CUSTOM_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
CUSTOM_MAX_CHUNK_BYTES = 1024 * 1024
CUSTOM_MAX_CITATIONS_PER_CHUNK = 4096

ChunkRole = Literal["parent", "child", "standalone"]


class CustomChunkOutputInvalid(ChunkError):
    code = "CHUNK_CUSTOM_OUTPUT_INVALID"
    title = "The custom chunk function returned invalid output."


@dataclass(frozen=True, slots=True)
class FunctionVersionRef:
    function_id: str
    version: int

    def __post_init__(self) -> None:
        if not self.function_id or self.version <= 0:
            raise ValueError("a positive pinned function version is required")


@dataclass(frozen=True, slots=True)
class CustomChunkLimits:
    max_chunks: int = CUSTOM_MAX_CHUNKS
    max_output_bytes: int = CUSTOM_MAX_OUTPUT_BYTES
    max_chunk_bytes: int = CUSTOM_MAX_CHUNK_BYTES
    max_citations_per_chunk: int = CUSTOM_MAX_CITATIONS_PER_CHUNK


@dataclass(frozen=True, slots=True)
class CustomChunkScope:
    document_id: UUID
    index_version: int
    tokenizer_fingerprint: str
    config_key: str


class CustomChunkExecutor(Protocol):
    identity: str

    async def execute(
        self,
        ref: FunctionVersionRef,
        doc: ParsedDocument,
        cfg: ChunkConfig,
        scope: CustomChunkScope,
        limits: CustomChunkLimits,
    ) -> list[ChunkSpec]: ...


def content_hash(content: str) -> str:
    return sha256(content.encode()).hexdigest()


def chunk_id(
    scope: CustomChunkScope,
    *,
    ordinal: int,
    role: ChunkRole,
    content_hash: str,
    citations: Sequence[dict[str, object]],
) -> UUID:
    identity = json.dumps(
        [scope.config_key, ordinal, role, content_hash, citations],
        sort_keys=True,
        ensure_ascii=False,
    )
    return uuid5(scope.document_id, identity)


def validate_custom_chunks(
    chunks: object,
    *,
    doc: ParsedDocument,
    cfg: ChunkConfig,
    tokenizer: Tokenizer,
    scope: CustomChunkScope,
    limits: CustomChunkLimits,
) -> list[ChunkSpec]:
    """Validate materialized executor output against an unexposed source baseline."""
    try:
        return _validate_custom_chunks(chunks, doc, cfg, tokenizer, scope, limits)
    except CustomChunkOutputInvalid:
        raise
    except (
        AttributeError,
        KeyError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        raise CustomChunkOutputInvalid() from exc


@dataclass(frozen=True, slots=True)
class _Citation:
    block: Block
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _ValidatedChunk:
    chunk: ChunkSpec
    role: ChunkRole
    citations: tuple[_Citation, ...]


_CITATION_FIELDS = frozenset(
    {
        "block_ordinal",
        "kind",
        "start",
        "end",
        "heading_path",
        "page",
        "bbox",
        "source_url",
    }
)
_METADATA_FIELDS = frozenset(
    {
        "role",
        "embed",
        "language",
        "heading_path",
        "page",
        "source_url",
        "ordinal",
        "citations",
        "oversized",
        "oversized_reason",
    }
)


def _invalid_if(condition: bool) -> None:
    if condition:
        raise CustomChunkOutputInvalid()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_json(value: object, *, depth: int = 0) -> None:
    _invalid_if(depth > 64)
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        _invalid_if(not math.isfinite(value))
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        _invalid_if(any(not isinstance(key, str) for key in value))
        for item in value.values():
            _validate_json(item, depth=depth + 1)
        return
    raise CustomChunkOutputInvalid()


def _validate_citations(
    raw_citations: object,
    *,
    block_by_ordinal: dict[int, tuple[int, Block]],
    max_citations: int,
    keep_tables_intact: bool,
) -> tuple[_Citation, ...]:
    _invalid_if(not isinstance(raw_citations, list))
    citations_list = cast("list[object]", raw_citations)
    _invalid_if(not citations_list or len(citations_list) > max_citations)
    result: list[_Citation] = []
    previous_block_index = -1
    heading_path: tuple[str, ...] | None = None
    for raw in citations_list:
        _invalid_if(not isinstance(raw, dict))
        item = cast("dict[object, object]", raw)
        _invalid_if(set(item) != _CITATION_FIELDS)
        block_ordinal = item["block_ordinal"]
        start = item["start"]
        end = item["end"]
        _invalid_if(not _is_int(block_ordinal) or not _is_int(start) or not _is_int(end))
        ordinal = cast("int", block_ordinal)
        _invalid_if(ordinal not in block_by_ordinal)
        block_index, block = block_by_ordinal[ordinal]
        source_start = cast("int", start)
        source_end = cast("int", end)
        _invalid_if(
            block_index <= previous_block_index
            or source_start < 0
            or source_start >= source_end
            or source_end > len(block.text)
        )
        canonical_bbox = list(block.bbox) if block.bbox is not None else None
        _invalid_if(
            item["kind"] != block.kind
            or item["heading_path"] != list(block.heading_path)
            or item["page"] != block.page
            or item["bbox"] != canonical_bbox
            or item["source_url"] != block.source_url
        )
        _invalid_if(
            not isinstance(item["heading_path"], list)
            or any(not isinstance(part, str) for part in item["heading_path"])
            or not (item["page"] is None or _is_int(item["page"]))
            or not (item["source_url"] is None or isinstance(item["source_url"], str))
        )
        bbox = item["bbox"]
        _invalid_if(
            bbox is not None
            and (
                not isinstance(bbox, list)
                or len(bbox) != 4
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in bbox
                )
            )
        )
        if heading_path is None:
            heading_path = block.heading_path
        _invalid_if(block.heading_path != heading_path)
        if keep_tables_intact and block.kind == "table":
            _invalid_if(
                source_start != 0 or source_end != len(block.text) or len(citations_list) != 1
            )
        result.append(_Citation(block, source_start, source_end))
        previous_block_index = block_index
    return tuple(result)


def _reconstruct_content(citations: tuple[_Citation, ...], cfg: ChunkConfig) -> str:
    heading_path = citations[0].block.heading_path
    prefix = ""
    if cfg.prepend_heading_path and heading_path:
        prefix = " > ".join(heading_path) + "\n\n"
    body = "\n\n".join(item.block.text[item.start : item.end] for item in citations)
    return prefix + body


def _is_protected_table(citations: tuple[_Citation, ...], cfg: ChunkConfig) -> bool:
    return (
        cfg.keep_tables_intact
        and len(citations) == 1
        and citations[0].block.kind == "table"
        and citations[0].start == 0
        and citations[0].end == len(citations[0].block.text)
    )


def _validate_metadata(
    chunk: ChunkSpec,
    role: ChunkRole,
    citations: tuple[_Citation, ...],
    doc: ParsedDocument,
    *,
    oversized: bool,
) -> None:
    metadata = chunk.metadata
    _invalid_if(not isinstance(metadata, dict) or not _METADATA_FIELDS.issubset(metadata))
    _validate_json(metadata)
    expected = {
        "role": role,
        "embed": role != "parent",
        "language": doc.language,
        "heading_path": list(citations[0].block.heading_path),
        "page": citations[0].block.page,
        "source_url": citations[0].block.source_url,
        "ordinal": chunk.ordinal,
        "oversized": oversized,
        "oversized_reason": "table" if oversized else None,
    }
    _invalid_if(any(metadata[key] != value for key, value in expected.items()))
    _invalid_if(not isinstance(metadata["embed"], bool))
    _invalid_if(not _is_int(metadata["ordinal"]))
    _invalid_if(not isinstance(metadata["oversized"], bool))


def _serialized_chunk(chunk: ChunkSpec) -> bytes:
    return json.dumps(
        {
            "id": str(chunk.id),
            "document_id": str(chunk.document_id),
            "ordinal": chunk.ordinal,
            "content": chunk.content,
            "content_hash": chunk.content_hash,
            "token_count": chunk.token_count,
            "parent_id": str(chunk.parent_id) if chunk.parent_id is not None else None,
            "metadata": chunk.metadata,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_links(validated: list[_ValidatedChunk]) -> None:
    by_id = {item.chunk.id: item for item in validated}
    child_counts: dict[UUID, int] = {}
    for item in validated:
        parent_id = item.chunk.parent_id
        if item.role != "child":
            _invalid_if(parent_id is not None)
            continue
        _invalid_if(not isinstance(parent_id, UUID) or parent_id not in by_id)
        parent_uuid = cast("UUID", parent_id)
        parent = by_id[parent_uuid]
        _invalid_if(parent.role != "parent")
        parent_ranges = {citation.block.ordinal: citation for citation in parent.citations}
        for child_range in item.citations:
            parent_range = parent_ranges.get(child_range.block.ordinal)
            _invalid_if(
                parent_range is None
                or child_range.start < parent_range.start
                or child_range.end > parent_range.end
            )
        child_counts[parent_uuid] = child_counts.get(parent_uuid, 0) + 1
    for item in validated:
        if item.role == "parent":
            _invalid_if(item.chunk.id not in child_counts)


def _validate_source_coverage(
    validated: list[_ValidatedChunk],
    blocks: Sequence[Block],
) -> None:
    ranges: dict[int, list[tuple[int, int]]] = {block.ordinal: [] for block in blocks}
    for item in validated:
        if item.role == "parent":
            continue
        for citation in item.citations:
            ranges[citation.block.ordinal].append((citation.start, citation.end))
    for block in blocks:
        if not block.text.strip():
            continue
        position = 0
        for start, end in sorted(ranges[block.ordinal]):
            _invalid_if(start > position)
            position = max(position, end)
        _invalid_if(position != len(block.text))


def _validate_custom_chunks(
    chunks: object,
    doc: ParsedDocument,
    cfg: ChunkConfig,
    tokenizer: Tokenizer,
    scope: CustomChunkScope,
    limits: CustomChunkLimits,
) -> list[ChunkSpec]:
    _invalid_if(
        not isinstance(chunks, list)
        or len(chunks) > limits.max_chunks
        or cfg.strategy != "custom"
        or scope.index_version <= 0
        or not scope.config_key
        or scope.tokenizer_fingerprint != tokenizer.fingerprint
    )
    chunk_list = cast("list[object]", chunks)
    block_by_ordinal: dict[int, tuple[int, Block]] = {}
    for index, block in enumerate(doc.blocks):
        _invalid_if(block.ordinal in block_by_ordinal)
        block_by_ordinal[block.ordinal] = (index, block)

    total_bytes = 2  # JSON list brackets are part of the executor-output limit.
    ids: set[UUID] = set()
    validated: list[_ValidatedChunk] = []
    for ordinal, raw_chunk in enumerate(chunk_list):
        _invalid_if(not isinstance(raw_chunk, ChunkSpec))
        chunk = cast("ChunkSpec", raw_chunk)
        _invalid_if(
            not isinstance(chunk.id, UUID)
            or chunk.document_id != scope.document_id
            or not _is_int(chunk.ordinal)
            or chunk.ordinal != ordinal
            or chunk.id in ids
            or not isinstance(chunk.content, str)
            or not isinstance(chunk.content_hash, str)
            or not _is_int(chunk.token_count)
            or chunk.token_count < 0
            or not isinstance(chunk.metadata, dict)
            or not (chunk.parent_id is None or isinstance(chunk.parent_id, UUID))
        )
        _invalid_if(content_hash(chunk.content) != chunk.content_hash)
        _invalid_if(tokenizer.count(chunk.content) != chunk.token_count)
        role_value = chunk.metadata.get("role")
        _invalid_if(role_value not in {"parent", "child", "standalone"})
        role = cast("ChunkRole", role_value)
        citations_value = chunk.metadata.get("citations")
        citations = _validate_citations(
            citations_value,
            block_by_ordinal=block_by_ordinal,
            max_citations=limits.max_citations_per_chunk,
            keep_tables_intact=cfg.keep_tables_intact,
        )
        raw_citations = cast("list[dict[str, object]]", citations_value)
        _invalid_if(chunk.content != _reconstruct_content(citations, cfg))
        protected_table = _is_protected_table(citations, cfg)
        budget = cfg.parent_tokens if role == "parent" else cfg.child_tokens
        oversized = chunk.token_count > budget
        _invalid_if(oversized and not protected_table)
        _validate_metadata(chunk, role, citations, doc, oversized=oversized)
        _invalid_if(
            chunk.id
            != chunk_id(
                scope,
                ordinal=ordinal,
                role=role,
                content_hash=chunk.content_hash,
                citations=raw_citations,
            )
        )
        content_bytes = len(chunk.content.encode("utf-8"))
        _invalid_if(content_bytes > limits.max_chunk_bytes)
        total_bytes += len(_serialized_chunk(chunk)) + (1 if ordinal else 0)
        _invalid_if(total_bytes > limits.max_output_bytes)
        ids.add(chunk.id)
        validated.append(_ValidatedChunk(chunk, role, citations))
    _validate_links(validated)
    _validate_source_coverage(validated, doc.blocks)
    return cast("list[ChunkSpec]", chunks)
