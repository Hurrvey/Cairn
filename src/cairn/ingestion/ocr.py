"""Bounded CPU OCR adapter and image parser with source geometry."""

from __future__ import annotations

import asyncio
import csv
import io
import math
import re
import warnings
from dataclasses import dataclass, fields
from typing import ClassVar, Protocol

from PIL import Image, UnidentifiedImageError

from cairn.ingestion.base import Block, ParseContext, ParsedDocument
from cairn.ingestion.errors import ParseCorruptFile, ParseEmptyContent, ParseError, ParseTooLarge
from cairn.ingestion.process import ProcessOutputTooLarge, ProcessTimedOut, run_bounded


class OcrEngineUnavailable(ParseError):
    code = "OCR_ENGINE_UNAVAILABLE"
    title = "The configured OCR engine or language data is unavailable."


class OcrInputTooLarge(ParseTooLarge):
    code = "OCR_INPUT_TOO_LARGE"
    title = "The image exceeds the OCR size or page limit."


class OcrTimedOut(ParseError):
    code = "OCR_TIMED_OUT"
    title = "OCR exceeded its execution time limit."
    retryable = True


class OcrFailed(ParseError):
    code = "OCR_FAILED"
    title = "OCR could not extract a valid result."
    retryable = True


class OcrLanguageUnsupported(ParseError):
    code = "OCR_LANGUAGE_UNSUPPORTED"
    title = "The requested OCR language is not configured."


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    confidence: float
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class OcrResult:
    text: str
    words: tuple[OcrWord, ...]
    width: int
    height: int
    language: str
    engine: str
    engine_version: str


class OcrEngine(Protocol):
    async def recognize(self, data: bytes, *, language: str | None = None) -> OcrResult: ...


@dataclass(frozen=True, slots=True)
class OcrLimits:
    max_input_bytes: int = 50 * 1024 * 1024
    max_pixels: int = 25_000_000
    max_dimension: int = 16_000
    max_output_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        if any(
            type(getattr(self, item.name)) is not int or getattr(self, item.name) <= 0
            for item in fields(self)
        ):
            raise ValueError("OCR limits must be positive integers")


def image_dimensions(data: bytes, limits: OcrLimits) -> tuple[int, int]:
    """Inspect bounded image headers without decoding the raster in the API process."""
    if len(data) > limits.max_input_bytes:
        raise OcrInputTooLarge()
    if not data:
        raise ParseEmptyContent()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > limits.max_pixels
                    or max(width, height) > limits.max_dimension
                ):
                    raise OcrInputTooLarge()
                if image.format not in {"PNG", "JPEG", "TIFF"}:
                    raise ParseCorruptFile("OCR accepts PNG, JPEG, and single-page TIFF images.")
                # Only ask whether a second frame exists. Counting n_frames
                # traverses an attacker-controlled TIFF directory chain.
                try:
                    image.seek(1)
                except EOFError:
                    pass
                else:
                    raise OcrInputTooLarge()
                return width, height
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise OcrInputTooLarge() from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError) as exc:
        raise ParseCorruptFile("The image header could not be read.") from exc


_LANGUAGE_ALIASES = {
    "en": "eng",
    "zh": "chi_sim",
    "zh-cn": "chi_sim",
    "zh-hans": "chi_sim",
    "zh-tw": "chi_tra",
    "zh-hant": "chi_tra",
    "de": "deu",
    "fr": "fra",
    "ja": "jpn",
    "ko": "kor",
    "es": "spa",
}


class TesseractOcrEngine:
    def __init__(
        self,
        *,
        command: tuple[str, ...] = ("tesseract",),
        languages: tuple[str, ...] = ("eng", "chi_sim", "chi_tra"),
        timeout_seconds: float = 45,
        max_concurrency: int = 1,
        limits: OcrLimits | None = None,
    ) -> None:
        if not command or any(not part or "\x00" in part for part in command):
            raise ValueError("command must contain nonempty arguments")
        if (
            not languages
            or any(not re.fullmatch(r"[a-zA-Z0-9_]+", lang) for lang in languages)
            or len(set(languages)) != len(languages)
        ):
            raise ValueError("languages must be unique Tesseract language identifiers")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or max_concurrency <= 0:
            raise ValueError("OCR timeout and concurrency must be positive")
        self._command = command
        self._languages = languages
        self._timeout = timeout_seconds
        self._slots = asyncio.Semaphore(max_concurrency)
        self._limits = limits or OcrLimits()
        self._version: str | None = None

    def _language(self, language: str | None) -> str:
        if language in (None, "", "und"):
            return "+".join(self._languages)
        requested = _LANGUAGE_ALIASES.get(language.lower(), language.lower())
        if requested not in self._languages:
            raise OcrLanguageUnsupported()
        selected = [requested]
        if requested.startswith("chi_") and "eng" in self._languages:
            selected.append("eng")
        return "+".join(selected)

    async def recognize(self, data: bytes, *, language: str | None = None) -> OcrResult:
        width, height = image_dimensions(data, self._limits)
        selected = self._language(language)
        try:
            async with asyncio.timeout(self._timeout), self._slots:
                if self._version is None:
                    status, output, _ = await run_bounded(
                        (*self._command, "--version"),
                        b"",
                        timeout_seconds=self._timeout,
                        max_output_bytes=65536,
                    )
                    if status:
                        raise OcrEngineUnavailable()
                    first = output.decode("utf-8", errors="replace").splitlines()
                    self._version = (
                        first[0][:100] if first and first[0].startswith("tesseract ") else "unknown"
                    )
                status, output, stderr = await run_bounded(
                    (*self._command, "stdin", "stdout", "-l", selected, "--psm", "3", "tsv"),
                    data,
                    timeout_seconds=self._timeout,
                    max_output_bytes=self._limits.max_output_bytes,
                )
                if b"Failed loading language" in stderr or b"Error opening data file" in stderr:
                    raise OcrEngineUnavailable()
                if status:
                    raise OcrFailed()
        except (FileNotFoundError, PermissionError) as exc:
            raise OcrEngineUnavailable() from exc
        except (TimeoutError, ProcessTimedOut) as exc:
            raise OcrTimedOut() from exc
        except ProcessOutputTooLarge as exc:
            raise OcrInputTooLarge("OCR output exceeds its configured byte limit.") from exc
        text, words = _parse_tsv(output, width=width, height=height)
        return OcrResult(text, words, width, height, selected, "tesseract", self._version)


def _parse_tsv(data: bytes, *, width: int, height: int) -> tuple[str, tuple[OcrWord, ...]]:
    words: list[OcrWord] = []
    lines: dict[tuple[str, str, str, str], list[str]] = {}
    try:
        reader = csv.DictReader(
            io.StringIO(data.decode("utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE
        )
        required = {
            "level",
            "page_num",
            "block_num",
            "par_num",
            "line_num",
            "left",
            "top",
            "width",
            "height",
            "conf",
            "text",
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("invalid TSV header")
        for row in reader:
            if row["level"] != "5" or not row["text"] or not row["text"].strip():
                continue
            left, top, word_width, word_height = (
                float(row[key]) for key in ("left", "top", "width", "height")
            )
            confidence = float(row["conf"])
            if (
                not all(math.isfinite(n) for n in (left, top, word_width, word_height, confidence))
                or min(left, top, word_width, word_height) < 0
                or confidence < 0
                or confidence > 100
                or left + word_width > width
                or top + word_height > height
            ):
                raise ValueError("invalid OCR geometry or confidence")
            content = row["text"].strip()
            words.append(
                OcrWord(content, confidence, (left, top, left + word_width, top + word_height))
            )
            key = tuple(row[field] for field in ("page_num", "block_num", "par_num", "line_num"))
            lines.setdefault((key[0], key[1], key[2], key[3]), []).append(content)
    except (ValueError, TypeError, KeyError, UnicodeError, csv.Error) as exc:
        raise OcrFailed("OCR returned an invalid result.") from exc
    if not words:
        raise ParseEmptyContent()
    return "\n".join(" ".join(line) for line in lines.values()), tuple(words)


class ImageOcrParser:
    supported_mimes: ClassVar[frozenset[str]] = frozenset({"image/png", "image/jpeg", "image/tiff"})

    def __init__(self, engine: OcrEngine, *, limits: OcrLimits | None = None) -> None:
        self._engine = engine
        self._limits = limits or OcrLimits()

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        image_dimensions(data, self._limits)
        result = await self._engine.recognize(data, language=ctx.language)
        if not result.text.strip() or not result.words:
            raise ParseEmptyContent()
        boxes = [word.bbox for word in result.words]
        bbox = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        return ParsedDocument(
            markdown=result.text,
            blocks=[
                Block("paragraph", result.text, 0, page=1, bbox=bbox, source_url=ctx.source_url)
            ],
            language=ctx.language,
            metadata={
                "ocr_engine": result.engine,
                "ocr_engine_version": result.engine_version,
                "ocr_language": result.language,
                "coordinate_space": "image_pixels_top_left",
            },
        )
