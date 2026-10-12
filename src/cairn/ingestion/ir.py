"""Bind parser-local evidence to immutable owner-scoped IDs and project it once."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import cast
from uuid import UUID

from cairn.core.document_ir import (
    AssetRef,
    DocumentIR,
    Element,
    Location,
    Provenance,
    Table,
    TableCell,
    semantic_metadata,
)
from cairn.ingestion.base import Block, BlockKind, ParsedDocument


def digest(value: object) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()


def bind_document(
    parsed: ParsedDocument,
    *,
    workspace_id: UUID,
    kb_id: UUID,
    document_id: UUID,
    revision: int,
    source_sha256: str,
    parser_fingerprint: str | None,
    context_fingerprint: str | None,
) -> DocumentIR:
    if parsed.ir is not None:
        if (
            parsed.ir.workspace_id,
            parsed.ir.kb_id,
            parsed.ir.document_id,
            parsed.ir.revision,
            parsed.ir.source_sha256,
            parsed.ir.parser_fingerprint,
            parsed.ir.context_fingerprint,
        ) != (
            workspace_id,
            kb_id,
            document_id,
            revision,
            source_sha256,
            parser_fingerprint,
            context_fingerprint,
        ):
            raise ValueError("parser supplied an IR for a different ingestion identity")
        return parsed.ir
    elements = list(parsed.elements)
    ocr_pages_value = parsed.metadata.get("ocr_pages")
    ocr_pages = ocr_pages_value if isinstance(ocr_pages_value, list) else []
    if not elements:
        for block in parsed.blocks:
            locations: tuple[Location, ...] = ()
            # Legacy parsers do not provide page dimensions; never infer them from a bbox.
            page_info = parsed.metadata.get("pdf_pages")
            if block.page is not None and block.bbox is not None and isinstance(page_info, list):
                info = page_info[block.page - 1]
                if isinstance(info, dict):
                    locations = (
                        Location(
                            page=block.page,
                            width=info["width"],
                            height=info["height"],
                            bbox=block.bbox,
                            crop=info.get("crop"),
                            rotation=info.get("rotation", 0),
                        ),
                    )
            elements.append(
                Element(
                    local_id=str(block.ordinal),
                    order=block.ordinal,
                    type="text" if block.kind == "paragraph" else block.kind,
                    text=block.text,
                    source_url=block.source_url,
                    legacy_page=block.page if not locations else None,
                    legacy_bbox=block.bbox if not locations else None,
                    locations=locations,
                    heading_level=block.heading_level,
                    heading_path=block.heading_path,
                    table=_baseline_table(block.text) if block.kind == "table" else None,
                    provenance=(
                        Provenance(
                            method=("ocr" if block.page in ocr_pages else "native")
                            if parsed.metadata.get("format") == "pdf"
                            else "legacy",
                            producer=str(parsed.metadata.get("pdf_engine") or "cairn"),
                        ),
                    ),
                )
            )
    local = [item.model_dump(mode="json", exclude={"element_id"}) for item in elements]
    assets = []
    total_bytes = 0
    for asset in parsed.assets:
        total_bytes += len(asset.data)
        if len(parsed.assets) > 500 or len(asset.data) > 8 * 1024**2 or total_bytes > 32 * 1024**2:
            raise ValueError("asset extraction exceeds its resource budget")
        asset_sha = sha256(asset.data).hexdigest()
        if asset.mime_type not in ("image/png", "image/jpeg"):
            continue
        assets.append(
            AssetRef(
                asset_id=asset_sha,
                sha256=asset_sha,
                mime_type=asset.mime_type,
                size_bytes=len(asset.data),
                page=asset.page,
            )
        )
    owner = {
        "workspace_id": str(workspace_id),
        "kb_id": str(kb_id),
        "document_id": str(document_id),
        "revision": revision,
        "source_sha256": source_sha256,
        "parser_fingerprint": parser_fingerprint,
        "context_fingerprint": context_fingerprint,
    }
    metadata = semantic_metadata(parsed.metadata)
    warning_values = metadata.get("warnings", [])
    warnings = (
        tuple(str(value) for value in warning_values) if isinstance(warning_values, list) else ()
    )
    if metadata.get("ocr_layout_unavailable_pages"):
        warnings += ("OCR word layout unavailable; authoritative page text was preserved.",)
    quality_status = "warnings" if warnings else "unscored"
    version = digest(
        dict(
            owner,
            elements=local,
            pages=parsed.pages,
            language=parsed.language,
            assets=[asset.model_dump(mode="json") for asset in assets],
            identity_version=2,
            schema_version=2,
            producer="cairn-ir/2",
            warnings=list(warnings),
            quality_status=quality_status,
            metadata=metadata,
        )
    )
    identities = {item.local_id: digest([version, item.local_id]) for item in elements}
    if len(identities) != len(elements):
        raise ValueError("duplicate parser-local element identity")
    bound = [
        item.model_copy(
            update={
                "element_id": identities[item.local_id],
                "parent_id": identities[item.parent_id] if item.parent_id else None,
                "caption_ids": tuple(identities[caption] for caption in item.caption_ids),
            }
        )
        for item in elements
    ]
    return DocumentIR(
        **owner,
        identity_version=2,
        parse_version=version,
        elements=bound,
        assets=assets,
        producer="cairn-ir/2",
        pages=parsed.pages,
        language=parsed.language,
        metadata=metadata,
        warnings=warnings,
        quality_status="warnings" if warnings else "unscored",
    )


def project_document(ir: DocumentIR) -> ParsedDocument:
    ir = DocumentIR.model_validate(ir.model_dump(mode="json"))
    from cairn.core.document_projection import project_ir

    blocks: list[Block] = []
    for projected in project_ir(ir, strict=False):
        element, location = projected.element, projected.location
        blocks.append(
            Block(
                kind=cast(
                    "BlockKind",
                    "paragraph" if element.type in ("text", "formula") else element.type,
                ),
                text=projected.text,
                ordinal=len(blocks),
                heading_path=element.heading_path,
                heading_level=element.heading_level,
                page=location.page if location else element.legacy_page,
                bbox=location.bbox if location else element.legacy_bbox,
                element_id=element.element_id,
                parse_version=ir.parse_version,
                source_url=element.source_url,
                locations=projected.locations,
            )
        )
    return ParsedDocument(
        markdown="\n\n".join(block.text for block in blocks),
        blocks=blocks,
        pages=ir.pages,
        language=ir.language,
        metadata=dict(ir.metadata),
        ir=ir,
    )


def _baseline_table(markdown: str) -> Table | None:
    if "\\\\" in markdown or re.search(r"<br\s*/?>|&(?:#\w+|\w+);", markdown, re.I):
        return None  # Producer-specific decoding cannot be proven from rendered Markdown alone.
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    if len(lines) < 2 or not all(line.startswith("|") and line.endswith("|") for line in lines):
        return None
    rows = [
        [_decode_cell(cell.strip()) for cell in _split_cells(line[1:-1])]
        for index, line in enumerate(lines)
        if index != 1
    ]
    columns = len(rows[0])
    if any(len(row) != columns for row in rows):
        return None
    table = Table(
        rows=len(rows),
        columns=columns,
        cells=[
            TableCell(
                text=value, row_start=row, row_end=row + 1, col_start=column, col_end=column + 1
            )
            for row, values in enumerate(rows)
            for column, value in enumerate(values)
        ],
    )

    return table if table.markdown() == markdown else None


def _split_cells(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if character == "|" and not escaped:
            cells.append("".join(current))
            current = []
        else:
            current.append(character)
        escaped = (not escaped) if character == "\\" else False
    cells.append("".join(current))
    return cells


def _decode_cell(cell: str) -> str:
    # Decode only the producer's pipe escape. Literal HTML and backslashes remain evidence.
    return cell.replace("\\|", "|")
