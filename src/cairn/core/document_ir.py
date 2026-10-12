"""Engine-independent, validated document evidence and immutable parse identity."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Location(EvidenceModel):
    page: int = Field(ge=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    bbox: tuple[float, float, float, float]
    coordinate_space: Literal["pdf_points_bottom_left"] = "pdf_points_bottom_left"
    crop: tuple[float, float, float, float] | None = None
    rotation: Literal[0, 90, 180, 270] = 0
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    span_source: Literal["text", "orig"] | None = None

    @property
    def display_bbox(self) -> tuple[float, float, float, float] | None:
        bounds = self.crop or (0.0, 0.0, self.width, self.height)
        box = (
            max(bounds[0], self.bbox[0]),
            max(bounds[1], self.bbox[1]),
            min(bounds[2], self.bbox[2]),
            min(bounds[3], self.bbox[3]),
        )
        return box if box[0] < box[2] and box[1] < box[3] else None

    @property
    def clipped(self) -> bool:
        return self.display_bbox != self.bbox

    @model_validator(mode="after")
    def validate_geometry(self) -> Self:
        x0, y0, x1, y1 = self.bbox
        bounds = self.crop or (0.0, 0.0, self.width, self.height)
        if x0 > x1 or y0 > y1:
            raise ValueError("source geometry is inverted")
        if (
            bounds[0] >= bounds[2]
            or bounds[1] >= bounds[3]
            or abs(bounds[2] - bounds[0] - self.width) > 1e-5
            or abs(bounds[3] - bounds[1] - self.height) > 1e-5
        ):
            raise ValueError("invalid visible crop geometry")
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("both character span endpoints are required")
        if self.char_start is not None and (
            self.char_end is None or self.char_end <= self.char_start or self.span_source is None
        ):
            raise ValueError("character spans require an explicit source and positive range")
        return self


class Provenance(EvidenceModel):
    method: Literal["native", "ocr", "model-derived", "legacy"]
    producer: str
    confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_meaning: str | None = None

    @model_validator(mode="after")
    def confidence_has_meaning(self) -> Self:
        if self.confidence is not None and not self.confidence_meaning:
            raise ValueError("confidence requires its meaning")
        return self


class TableCell(EvidenceModel):
    text: str
    row_start: int = Field(ge=0)
    row_end: int = Field(gt=0)
    col_start: int = Field(ge=0)
    col_end: int = Field(gt=0)
    row_span: int | None = Field(default=None, gt=0)
    col_span: int | None = Field(default=None, gt=0)
    locations: tuple[Location, ...] = ()
    value: str | None = None
    value_type: Literal["decimal", "integer", "text", "unknown"] = "unknown"
    unit: str | None = None

    @model_validator(mode="after")
    def valid_span(self) -> Self:
        if self.row_end <= self.row_start or self.col_end <= self.col_start:
            raise ValueError("table spans must be positive")
        if self.row_span not in (None, self.row_end - self.row_start) or self.col_span not in (
            None,
            self.col_end - self.col_start,
        ):
            raise ValueError("table span evidence is inconsistent")
        object.__setattr__(self, "row_span", self.row_end - self.row_start)
        object.__setattr__(self, "col_span", self.col_end - self.col_start)
        if self.value is not None and self.value_type in ("decimal", "integer"):
            try:
                number = Decimal(self.value)
                if not number.is_finite() or (
                    self.value_type == "integer" and number != number.to_integral()
                ):
                    raise ValueError("invalid exact numeric value")
            except InvalidOperation as exc:
                raise ValueError("invalid exact numeric value") from exc
        return self


class Table(EvidenceModel):
    rows: int = Field(ge=1, le=10000)
    columns: int = Field(ge=1, le=1000)
    cells: tuple[TableCell, ...] = Field(max_length=100000)

    @model_validator(mode="after")
    def valid_grid(self) -> Self:
        if self.rows * self.columns > 100000:
            raise ValueError("table grid exceeds the cell budget")
        occupied: set[tuple[int, int]] = set()
        for cell in self.cells:
            if cell.row_end > self.rows or cell.col_end > self.columns:
                raise ValueError("table cell exceeds its grid")
            for row in range(cell.row_start, cell.row_end):
                for col in range(cell.col_start, cell.col_end):
                    if (row, col) in occupied:
                        raise ValueError("table cells overlap")
                    occupied.add((row, col))
                    if len(occupied) > 100000:
                        raise ValueError("table grid exceeds the cell budget")
        return self

    def markdown(self) -> str:
        anchors = {(cell.row_start, cell.col_start): cell.text for cell in self.cells}
        lines = []
        for row in range(self.rows):
            values = [
                anchors.get((row, col), "").replace("|", "\\|").replace("\n", "<br>")
                for col in range(self.columns)
            ]
            lines.append("| " + " | ".join(values) + " |")
            if row == 0:
                lines.append("| " + " | ".join("---" for _ in range(self.columns)) + " |")
        return "\n".join(lines)


class Formula(EvidenceModel):
    native: str = ""
    recognized: str | None = None
    format: Literal["latex", "mathml", "native"] | None = None
    status: Literal["recognized", "unrecognized", "missing"]


class AssetRef(EvidenceModel):
    asset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    mime_type: Literal["image/png", "image/jpeg"]
    size_bytes: int = Field(ge=1, le=32 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page: int | None = Field(default=None, ge=1)


class Element(EvidenceModel):
    local_id: str = Field(min_length=1, max_length=256)
    element_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    parent_id: str | None = None
    caption_ids: tuple[str, ...] = ()
    order: int = Field(ge=0)
    type: Literal["text", "heading", "list", "code", "table", "image", "formula"]
    text: str = ""
    source_url: str | None = None
    legacy_page: int | None = Field(default=None, ge=1)
    legacy_bbox: tuple[float, float, float, float] | None = None
    orig: str | None = None
    locations: tuple[Location, ...] = ()
    provenance: tuple[Provenance, ...] = ()
    table: Table | None = None
    formula: Formula | None = None
    asset_id: str | None = None
    asset_status: Literal["available", "unavailable"] | None = None
    heading_level: int | None = Field(default=None, ge=1, le=6)
    heading_path: tuple[str, ...] = ()

    @property
    def search_text(self) -> str:
        if self.table is not None:
            return self.table.markdown()
        if self.formula is not None:
            return self.formula.recognized or self.formula.native
        return self.text

    @model_validator(mode="after")
    def valid_content(self) -> Self:
        if self.table is not None and self.type != "table":
            raise ValueError("table content belongs to a table element")
        if self.formula is not None and self.type != "formula":
            raise ValueError("formula content belongs to a formula element")
        for location in self.locations:
            if location.char_end is not None:
                source = self.orig if location.span_source == "orig" else self.text
                if source is None or location.char_end > len(source):
                    raise ValueError("character span exceeds its declared source")
        if len({location.page for location in self.locations}) > 1 and self.type not in (
            "table",
            "image",
        ):
            spanned = [loc for loc in self.locations if loc.char_end is not None]
            if spanned:
                if len(spanned) != len(self.locations):
                    raise ValueError("multipage text requires exact nonoverlapping spans")
                spans = sorted(self.locations, key=lambda item: item.char_start or 0)
                end = 0
                span_source = spans[0].span_source
                for location in spans:
                    if location.char_start != end or location.span_source != span_source:
                        raise ValueError("multipage text requires exact nonoverlapping spans")
                    end = location.char_end or 0
                source = self.orig if span_source == "orig" else self.text
                if end != len(source or ""):
                    raise ValueError("multipage spans must cover their source")
            # Unspanned multipage text is kept whole by the projection, with no page.
        return self


class DocumentIR(EvidenceModel):
    schema_version: Literal[2] = 2
    identity_version: Literal[1, 2] = 1
    workspace_id: UUID
    kb_id: UUID
    document_id: UUID
    revision: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parse_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_fingerprint: str | None = None
    context_fingerprint: str | None = None
    producer: str
    pages: int = Field(ge=1, le=500)
    language: str = "und"
    elements: tuple[Element, ...] = Field(max_length=100000)
    assets: tuple[AssetRef, ...] = Field(default=(), max_length=500)
    warnings: tuple[str, ...] = ()
    quality_status: Literal["unscored", "warnings"] = "unscored"
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_relations(self) -> Self:
        if len({item.local_id for item in self.elements}) != len(self.elements):
            raise ValueError("local element identities must be unique")
        ids = {item.element_id for item in self.elements}
        if None in ids or len(ids) != len(self.elements):
            raise ValueError("element identities must be unique and bound")
        if len({item.order for item in self.elements}) != len(self.elements):
            raise ValueError("element order must be unique")
        parents = {item.element_id: item.parent_id for item in self.elements}
        assets = {asset.asset_id for asset in self.assets}
        for item in self.elements:
            if any(caption not in ids for caption in item.caption_ids):
                raise ValueError("caption references a missing element")
            if any(location.page > self.pages for location in item.locations):
                raise ValueError("element references a missing page")
            if item.table is not None and any(
                location.page > self.pages
                for cell in item.table.cells
                for location in cell.locations
            ):
                raise ValueError("table cell references a missing page")
            if item.asset_id is not None and item.asset_id not in assets:
                raise ValueError("element references a missing asset")
            visited = {item.element_id}
            parent = item.parent_id
            while parent is not None:
                if parent not in ids or parent in visited:
                    raise ValueError("invalid or cyclic element hierarchy")
                visited.add(parent)
                parent = parents[parent]
        local_ids = {item.element_id: item.local_id for item in self.elements}
        local = []
        for item in self.elements:
            value = item.model_dump(mode="json", exclude={"element_id"})
            value["parent_id"] = local_ids[item.parent_id] if item.parent_id else None
            value["caption_ids"] = [local_ids[caption] for caption in item.caption_ids]
            local.append(value)
        if self.identity_version == 1:
            for local_item in local:
                local_item.pop("display_bbox", None)
        owner = {
            "workspace_id": str(self.workspace_id),
            "kb_id": str(self.kb_id),
            "document_id": str(self.document_id),
            "revision": self.revision,
            "source_sha256": self.source_sha256,
            "parser_fingerprint": self.parser_fingerprint,
            "context_fingerprint": self.context_fingerprint,
        }
        value = dict(
            owner,
            elements=local,
            pages=self.pages,
            language=self.language,
            assets=[asset.model_dump(mode="json") for asset in self.assets],
        )
        if self.identity_version == 2:
            value.update(
                identity_version=2,
                schema_version=self.schema_version,
                producer=self.producer,
                warnings=list(self.warnings),
                quality_status=self.quality_status,
                metadata=semantic_metadata(self.metadata),
            )
        expected = hashlib.sha256(
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
        ).hexdigest()
        if expected != self.parse_version:
            raise ValueError("parse version does not match immutable evidence")
        if self.identity_version == 2:
            for item in self.elements:
                derived = hashlib.sha256(
                    json.dumps(
                        [self.parse_version, item.local_id],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                if item.element_id != derived:
                    raise ValueError("element identity does not match its parse/local identity")
        return self


def semantic_metadata(metadata: dict[str, JsonValue]) -> dict[str, JsonValue]:
    noise = {"pdf_worker_pid", "elapsed_seconds", "temporary_path", "cache_hits", "cache_misses"}
    return {key: value for key, value in metadata.items() if key not in noise}


def parse_content_digest(
    *, owner: dict[str, object], elements: list[dict[str, object]], pages: int, language: str
) -> str:
    value = {**owner, "elements": elements, "pages": pages, "language": language}
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
