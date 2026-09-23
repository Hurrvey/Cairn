"""Bounded PDF parser with all PDFium calls isolated in child processes."""

from __future__ import annotations

import asyncio
import json
import math
import sys
from dataclasses import dataclass
from typing import Any, ClassVar, cast

from pydantic import JsonValue

from cairn.ingestion.base import Block, BlockKind, ParseContext, ParsedDocument
from cairn.ingestion.errors import ParseEmptyContent, ParseError, ParseTooLarge
from cairn.ingestion.ocr import OcrEngine, OcrResult
from cairn.ingestion.process import ProcessOutputTooLarge, ProcessTimedOut, run_bounded


class PdfError(ParseError):
    code = "PDF_PARSE_FAILED"
    title = "The PDF could not be parsed."
    retryable = False


class PdfCorruptFile(PdfError):
    code = "PDF_CORRUPT_FILE"
    title = "The PDF is corrupt or uses unsupported structures."


class PdfEncrypted(PdfError):
    code = "PDF_ENCRYPTED"
    title = "Encrypted PDFs are not supported."


class PdfTooLarge(ParseTooLarge):
    code = "PDF_TOO_LARGE"
    title = "The PDF exceeds a configured resource limit."
    retryable = False


class PdfOcrRequired(PdfError):
    code = "PDF_OCR_REQUIRED"
    title = "A PDF page requires OCR, but no OCR engine is configured."


class PdfTimedOut(PdfError):
    code = "PDF_TIMED_OUT"
    title = "PDF parsing exceeded its time limit."
    retryable = True


class PdfEngineUnavailable(PdfError):
    code = "PDF_ENGINE_UNAVAILABLE"
    title = "The PDF parsing engine is unavailable."


@dataclass(frozen=True, slots=True)
class PdfLimits:
    max_input_bytes: int = 64 * 1024 * 1024
    max_pages: int = 500
    max_page_width_points: float = 20_000.0
    max_page_height_points: float = 20_000.0
    max_page_pixels: int = 40_000_000
    max_page_chars: int = 2_000_000
    max_text_chars: int = 16_000_000
    max_page_objects: int = 100_000
    max_layout_words: int = 100_000
    max_output_bytes: int = 64 * 1024 * 1024
    max_render_bytes: int = 32 * 1024 * 1024
    max_memory_bytes: int = 1024 * 1024 * 1024
    timeout_seconds: float = 120.0
    render_scale: float = 2.0
    ocr_text_threshold: int = 8

    def __post_init__(self) -> None:
        positive = (
            self.max_input_bytes,
            self.max_pages,
            self.max_page_width_points,
            self.max_page_height_points,
            self.max_page_pixels,
            self.max_page_chars,
            self.max_text_chars,
            self.max_page_objects,
            self.max_layout_words,
            self.max_output_bytes,
            self.max_render_bytes,
            self.max_memory_bytes,
            self.timeout_seconds,
            self.render_scale,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for value in positive
        ):
            raise ValueError("PDF limits must be finite and positive")
        integer_limits = (
            self.max_input_bytes,
            self.max_pages,
            self.max_page_pixels,
            self.max_page_chars,
            self.max_text_chars,
            self.max_page_objects,
            self.max_layout_words,
            self.max_output_bytes,
            self.max_render_bytes,
            self.max_memory_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_limits):
            raise ValueError("PDF count and byte limits must be integers")
        if isinstance(self.ocr_text_threshold, bool) or self.ocr_text_threshold < 0:
            raise ValueError("PDF OCR text threshold must be non-negative")
        if not isinstance(self.ocr_text_threshold, int):
            raise ValueError("PDF OCR text threshold must be an integer")


@dataclass(frozen=True, slots=True)
class _LayoutBlock:
    kind: BlockKind
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class _Page:
    number: int
    width: float
    height: float
    bbox: tuple[float, float, float, float]
    rotation: int
    char_count: int
    extracted_characters: int
    has_page_objects: bool
    has_raster_objects: bool
    layout: tuple[_LayoutBlock, ...]


@dataclass(frozen=True, slots=True)
class _Extraction:
    worker_pid: int
    pages: tuple[_Page, ...]


class PdfParser:
    supported_mimes: ClassVar[frozenset[str]] = frozenset({"application/pdf"})

    def __init__(self, ocr: OcrEngine | None = None, *, limits: PdfLimits | None = None) -> None:
        self._ocr = ocr
        self._limits = limits or PdfLimits()

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        if len(data) > self._limits.max_input_bytes:
            raise PdfTooLarge()
        if not data.startswith(b"%PDF-"):
            raise PdfCorruptFile()
        try:
            async with asyncio.timeout(self._limits.timeout_seconds):
                return await self._parse(data, ctx)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise PdfTimedOut() from exc
        except ProcessTimedOut as exc:
            raise PdfTimedOut() from exc
        except ProcessOutputTooLarge as exc:
            raise PdfTooLarge() from exc

    async def _parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        extraction = await self._extract(data)
        blocks: list[Block] = []
        ocr_pages: list[int] = []
        text_characters = 0
        for page in extraction.pages:
            if not page.has_page_objects:
                continue
            needs_ocr = page.extracted_characters == 0 or (
                page.extracted_characters < self._limits.ocr_text_threshold
                and page.has_raster_objects
            )
            if needs_ocr:
                if self._ocr is None:
                    raise PdfOcrRequired()
                image = await self._render(data, page.number - 1)
                result = await self._ocr.recognize(
                    image,
                    language=None if ctx.language == "und" else ctx.language,
                )
                page_blocks = [self._ocr_block(result, page, ctx, len(blocks))]
                ocr_pages.append(page.number)
            else:
                page_blocks = [
                    Block(
                        kind=block.kind,
                        text=block.text,
                        ordinal=len(blocks) + offset,
                        page=page.number,
                        bbox=block.bbox,
                        source_url=ctx.source_url,
                    )
                    for offset, block in enumerate(page.layout)
                ]
            text_characters += sum(len(block.text) for block in page_blocks)
            if text_characters > self._limits.max_text_chars:
                raise PdfTooLarge()
            blocks.extend(page_blocks)

        if not blocks:
            raise ParseEmptyContent()
        markdown = "\n\n".join(block.text for block in blocks)
        if len(markdown.encode("utf-8")) > self._limits.max_output_bytes:
            raise PdfTooLarge()
        ocr_page_values: list[JsonValue] = list(ocr_pages)
        metadata: dict[str, JsonValue] = {
            "format": "pdf",
            "pdf_engine": "pypdfium2",
            "pdf_worker_pid": extraction.worker_pid,
            "coordinate_space": "pdf_points_bottom_left",
            "ocr_pages": ocr_page_values,
        }
        return ParsedDocument(
            markdown=markdown,
            blocks=blocks,
            pages=len(extraction.pages),
            language=ctx.language,
            metadata=metadata,
        )

    async def _extract(self, data: bytes) -> _Extraction:
        config = self._worker_config()
        config["max_json_bytes"] = self._limits.max_output_bytes
        return_code, stdout, stderr = await run_bounded(
            self._command("extract", config),
            data,
            timeout_seconds=self._limits.timeout_seconds,
            max_output_bytes=self._limits.max_output_bytes,
        )
        if return_code != 0:
            self._raise_worker_error(stderr)
        try:
            raw: object = json.loads(stdout)
            return self._decode_extraction(raw)
        except (json.JSONDecodeError, UnicodeError, TypeError, ValueError, KeyError) as exc:
            raise PdfCorruptFile() from exc

    async def _render(self, data: bytes, page_index: int) -> bytes:
        config = self._worker_config()
        config.update(
            {
                "page_index": page_index,
                "scale": self._limits.render_scale,
                "max_page_pixels": self._limits.max_page_pixels,
                "max_png_bytes": self._limits.max_render_bytes,
            }
        )
        return_code, stdout, stderr = await run_bounded(
            self._command("render", config),
            data,
            timeout_seconds=self._limits.timeout_seconds,
            max_output_bytes=self._limits.max_render_bytes,
        )
        if return_code != 0:
            self._raise_worker_error(stderr)
        if not stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PdfCorruptFile()
        return stdout

    def _worker_config(self) -> dict[str, int | float]:
        return {
            "max_input_bytes": self._limits.max_input_bytes,
            "max_pages": self._limits.max_pages,
            "max_page_width_points": self._limits.max_page_width_points,
            "max_page_height_points": self._limits.max_page_height_points,
            "max_page_chars": self._limits.max_page_chars,
            "max_text_chars": self._limits.max_text_chars,
            "max_page_objects": self._limits.max_page_objects,
            "max_layout_words": self._limits.max_layout_words,
            "max_memory_bytes": self._limits.max_memory_bytes,
        }

    @staticmethod
    def _command(action: str, config: dict[str, int | float]) -> tuple[str, ...]:
        return (
            sys.executable,
            "-m",
            "cairn.ingestion.pdf_worker",
            action,
            json.dumps(config, separators=(",", ":"), allow_nan=False),
        )

    @staticmethod
    def _raise_worker_error(stderr: bytes) -> None:
        try:
            payload: object = json.loads(stderr)
            code = payload.get("code") if isinstance(payload, dict) else None
        except (json.JSONDecodeError, UnicodeError):
            code = None
        if code == "encrypted":
            raise PdfEncrypted()
        if code == "too_large":
            raise PdfTooLarge()
        if code == "unavailable":
            raise PdfEngineUnavailable()
        raise PdfCorruptFile()

    def _decode_extraction(self, raw: object) -> _Extraction:
        if not isinstance(raw, dict):
            raise ValueError("invalid extraction")
        worker_pid = _integer(raw.get("worker_pid"), minimum=1)
        raw_pages = raw.get("pages")
        if not isinstance(raw_pages, list) or not 0 < len(raw_pages) <= self._limits.max_pages:
            raise ValueError("invalid pages")
        pages: list[_Page] = []
        for expected_number, value in enumerate(raw_pages, start=1):
            if not isinstance(value, dict):
                raise ValueError("invalid page")
            number = _integer(value.get("number"), minimum=1)
            if number != expected_number:
                raise ValueError("invalid page order")
            width = _number(value.get("width"), maximum=self._limits.max_page_width_points)
            height = _number(value.get("height"), maximum=self._limits.max_page_height_points)
            raw_bbox = value.get("bbox")
            if not isinstance(raw_bbox, list):
                raise ValueError("invalid page box")
            page_bbox = tuple(_coordinate(item) for item in raw_bbox)
            if (
                len(page_bbox) != 4
                or page_bbox[2] - page_bbox[0] != width
                or page_bbox[3] - page_bbox[1] != height
            ):
                raise ValueError("invalid page box")
            rotation = _integer(value.get("rotation"), minimum=0, maximum=270)
            if rotation not in {0, 90, 180, 270}:
                raise ValueError("invalid page rotation")
            char_count = _integer(
                value.get("char_count"), minimum=0, maximum=self._limits.max_page_chars
            )
            extracted_characters = _integer(
                value.get("extracted_characters"),
                minimum=0,
                maximum=self._limits.max_page_chars,
            )
            has_page_objects = value.get("has_page_objects")
            if not isinstance(has_page_objects, bool):
                raise ValueError("invalid page object flag")
            has_raster_objects = value.get("has_raster_objects")
            if not isinstance(has_raster_objects, bool):
                raise ValueError("invalid raster object flag")
            raw_blocks = value.get("blocks")
            if not isinstance(raw_blocks, list):
                raise ValueError("invalid layout blocks")
            layout: list[_LayoutBlock] = []
            for raw_block in raw_blocks:
                if not isinstance(raw_block, dict):
                    raise ValueError("invalid layout block")
                kind = raw_block.get("kind")
                text = raw_block.get("text")
                bbox = raw_block.get("bbox")
                if kind not in {"paragraph", "table"} or not isinstance(text, str) or not text:
                    raise ValueError("invalid layout block")
                if not isinstance(bbox, list):
                    raise ValueError("invalid layout block")
                coordinates = tuple(_coordinate(item) for item in bbox)
                if len(coordinates) != 4:
                    raise ValueError("invalid layout block box")
                if coordinates[0] > coordinates[2] or coordinates[1] > coordinates[3]:
                    raise ValueError("inverted layout block box")
                layout.append(_LayoutBlock(cast("BlockKind", kind), text, coordinates))
            pages.append(
                _Page(
                    number,
                    width,
                    height,
                    page_bbox,
                    rotation,
                    char_count,
                    extracted_characters,
                    has_page_objects,
                    has_raster_objects,
                    tuple(layout),
                )
            )
        return _Extraction(worker_pid, tuple(pages))

    def _ocr_block(self, result: OcrResult, page: _Page, ctx: ParseContext, ordinal: int) -> Block:
        text = result.text.strip()
        if not text:
            raise ParseEmptyContent()
        if len(text) > self._limits.max_page_chars:
            raise PdfTooLarge()
        if result.width <= 0 or result.height <= 0:
            raise PdfCorruptFile()
        boxes: list[tuple[float, float, float, float]] = []
        for word in result.words:
            left, top, right, bottom = word.bbox
            if not all(math.isfinite(number) for number in word.bbox) or not (
                0 <= left <= right <= result.width and 0 <= top <= bottom <= result.height
            ):
                raise PdfCorruptFile()
            boxes.append(_image_bbox_to_pdf(word.bbox, result.width, result.height, page))
        bbox = _union_bbox(boxes) if boxes else page.bbox
        return Block(
            kind="paragraph",
            text=text,
            ordinal=ordinal,
            page=page.number,
            bbox=bbox,
            source_url=ctx.source_url,
        )


def _number(value: Any, *, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("number required")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or (maximum is not None and number > maximum):
        raise ValueError("invalid number")
    return number


def _coordinate(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("coordinate required")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid coordinate")
    return number


def _integer(value: Any, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("integer required")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError("invalid integer")
    return value


def _union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _image_bbox_to_pdf(
    bbox: tuple[float, float, float, float],
    image_width: int,
    image_height: int,
    page: _Page,
) -> tuple[float, float, float, float]:
    """Invert PDFium's page-rotation render transform into PDF point space."""
    left, top, right, bottom = bbox
    page_left, page_bottom, page_right, page_top = page.bbox
    if page.rotation == 0:
        return (
            page_left + left / image_width * page.width,
            page_top - bottom / image_height * page.height,
            page_left + right / image_width * page.width,
            page_top - top / image_height * page.height,
        )
    if page.rotation == 90:
        return (
            page_left + top / image_height * page.width,
            page_bottom + left / image_width * page.height,
            page_left + bottom / image_height * page.width,
            page_bottom + right / image_width * page.height,
        )
    if page.rotation == 180:
        return (
            page_right - right / image_width * page.width,
            page_bottom + top / image_height * page.height,
            page_right - left / image_width * page.width,
            page_bottom + bottom / image_height * page.height,
        )
    return (
        page_right - bottom / image_height * page.width,
        page_top - right / image_width * page.height,
        page_right - top / image_height * page.width,
        page_top - left / image_width * page.height,
    )
