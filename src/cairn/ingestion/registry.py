"""Explicit MIME dispatch; unsupported formats never degrade to plain text."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from types import MappingProxyType

from pydantic import JsonValue

from cairn.ingestion.base import ParseContext, ParsedDocument, Parser
from cairn.ingestion.errors import ParseEmptyContent, ParseTooLarge, ParseUnsupportedMime
from cairn.ingestion.identity import ocr_identity, package_version
from cairn.ingestion.ir import digest
from cairn.ingestion.language import resolve_language
from cairn.ingestion.ocr import ImageOcrParser, TesseractOcrEngine
from cairn.ingestion.office import CsvParser, DocxParser, PptxParser, XlsxParser
from cairn.ingestion.parsers import HtmlParser, JsonParser, MarkdownParser, TextParser
from cairn.ingestion.runtime_config import get_ingestion_runtime_settings


@dataclass(frozen=True, slots=True)
class ParserDescriptor:
    implementation: str
    options: Mapping[str, JsonValue] = field(default_factory=dict)
    _fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(deepcopy(dict(self.options))))
        object.__setattr__(
            self,
            "_fingerprint",
            digest({"implementation": self.implementation, "options": dict(self.options)}),
        )

    @property
    def fingerprint(self) -> str:
        return self._fingerprint


def context_fingerprint(ctx: ParseContext) -> str:
    return digest(asdict(ctx))


def _normalize_mime(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower()


class ParserRegistry:
    def __init__(self, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._parsers: dict[str, Parser] = {}
        self._descriptors: dict[str, ParserDescriptor] = {}

    @property
    def supported_mimes(self) -> frozenset[str]:
        return frozenset(self._parsers)

    def register(self, parser: Parser, *, descriptor: ParserDescriptor | None = None) -> None:
        mimes = {_normalize_mime(mime) for mime in parser.supported_mimes}
        if not mimes or any("/" not in mime for mime in mimes):
            raise ValueError("A parser must declare valid MIME types")
        if mimes & self._parsers.keys():
            raise ValueError("A parser is already registered for this MIME type")
        self._parsers.update(dict.fromkeys(mimes, parser))
        if descriptor is not None:
            self._descriptors.update(dict.fromkeys(mimes, descriptor))

    def describe(self, mime: str) -> ParserDescriptor | None:
        return self._descriptors.get(_normalize_mime(mime))

    def validate_bound(self, parsed: ParsedDocument, *, mime: str) -> None:
        parser = self._parsers.get(_normalize_mime(mime))
        limits = getattr(parser, "_limits", None)
        if limits is not None and parsed.metadata.get("format") == "pdf":
            from cairn.ingestion.pdf_budget import validate_pdf_budget

            validate_pdf_budget(parsed, limits)

    def accepts_cached(self, parsed: ParsedDocument, *, mime: str, source_bytes: int) -> bool:
        if source_bytes > self._max_bytes or parsed.ir is None or parsed.ir.identity_version != 2:
            return False
        parser = self._parsers.get(_normalize_mime(mime))
        limits = getattr(parser, "_limits", None)
        if limits is None:
            return len(parsed.markdown) <= 16_000_000
        from cairn.ingestion.pdf_budget import validate_pdf_budget

        try:
            validate_pdf_budget(parsed, limits)
        except (ValueError, ParseTooLarge, TypeError, KeyError):
            return False
        if source_bytes > getattr(limits, "max_input_bytes", self._max_bytes):
            return False
        if parsed.pages > getattr(limits, "max_pages", 500):
            return False
        if len(parsed.markdown) > getattr(limits, "max_text_chars", 16_000_000):
            return False
        if len(parsed.markdown.encode()) > getattr(limits, "max_output_bytes", 64 * 1024**2):
            return False
        for element in parsed.ir.elements:
            for location in element.locations:
                if location.width > getattr(
                    limits, "max_page_width_points", 20000
                ) or location.height > getattr(limits, "max_page_height_points", 20000):
                    return False
        return True

    async def aclose(self) -> None:
        """Release long-lived parser resources such as a warm model child."""
        closed: set[int] = set()
        for parser in self._parsers.values():
            if id(parser) in closed:
                continue
            closed.add(id(parser))
            closer = getattr(parser, "aclose", None)
            if closer is not None:
                await closer()

    async def parse(self, data: bytes, *, mime: str, ctx: ParseContext) -> ParsedDocument:
        if len(data) > self._max_bytes:
            raise ParseTooLarge()
        parser = self._parsers.get(_normalize_mime(mime))
        if parser is None:
            raise ParseUnsupportedMime()
        if not data:
            raise ParseEmptyContent()
        parsed = await parser.parse(data, ctx)
        if (not parsed.markdown.strip() or not parsed.blocks) and not parsed.elements:
            raise ParseEmptyContent()
        language = await resolve_language(
            parsed.markdown,
            explicit=ctx.language if ctx.language != "und" else parsed.language,
        )
        parsed.language = language.language
        return parsed


def get_parser_registry() -> ParserRegistry:
    from cairn.ingestion.docling_pdf import (
        DoclingDocxParser,
        DoclingOfficeParser,
        DoclingPdfParser,
        DoclingPptxParser,
    )
    from cairn.ingestion.pdf import PdfLimits, PdfParser

    registry = ParserRegistry()
    settings = get_ingestion_runtime_settings()
    ocr_fp = ocr_identity(tuple(settings.ocr_languages.split("+")))
    ocr = (
        TesseractOcrEngine(
            languages=tuple(settings.ocr_languages.split("+")),
            psm=settings.ocr_psm,
            timeout_seconds=settings.ocr_timeout_s,
            max_concurrency=settings.ocr_max_concurrency,
        )
        if settings.ocr_enabled
        else None
    )
    docling: DoclingPdfParser | None = None
    pdf: Parser
    if settings.pdf_engine == "docling":
        from cairn.ingestion.pdf_routing import ScanTextLayerRoutedPdfParser

        docling = DoclingPdfParser(
            python=settings.docling_python,
            assets_path=settings.docling_assets_path,
            manifest_path=settings.docling_manifest_path,
            ocr=settings.ocr_enabled,
            languages=tuple(settings.ocr_languages.split("+")),
            psm=settings.ocr_psm,
            persistent=settings.docling_persistent,
            limits=PdfLimits(
                timeout_seconds=settings.docling_timeout_s,
                max_memory_bytes=settings.docling_memory_bytes,
            ),
        )
        # Docling is the product engine; scans that already carry a text layer are
        # read by the baseline instead of being OCR'd a second time.
        pdf = (
            ScanTextLayerRoutedPdfParser(docling=docling, baseline=PdfParser(ocr=None))
            if settings.docling_scan_text_layer
            else docling
        )
    else:
        pdf = PdfParser(ocr=ocr)
    docx_parser: Parser = DocxParser()
    pptx_parser: Parser = PptxParser()
    if docling is not None and settings.docling_office:
        # Word and PowerPoint go through the same warm Docling child as PDFs, which
        # also extracts their pictures; XLSX stays on openpyxl to keep formula source.
        docx_parser = DoclingDocxParser(engine=docling, fallback=docx_parser)
        pptx_parser = DoclingPptxParser(engine=docling, fallback=pptx_parser)
    pdf_routing_rule = (
        "docling+scan-text-layer/1"
        if settings.pdf_engine == "docling" and settings.docling_scan_text_layer
        else "cairn-image-dominated/1"
    )
    for parser in (
        TextParser(),
        MarkdownParser(),
        JsonParser(),
        HtmlParser(),
        docx_parser,
        pptx_parser,
        XlsxParser(),
        CsvParser(),
        pdf,
    ):
        registry.register(
            parser,
            descriptor=ParserDescriptor(
                implementation=f"cairn/{type(parser).__name__}/3",
                options={
                    "ocr_languages": settings.ocr_languages,
                    "ocr_enabled": settings.ocr_enabled,
                    "ocr_psm": settings.ocr_psm,
                    "engine": settings.pdf_engine,
                    "model_manifest": getattr(parser, "identity", None),
                    "engine_package": package_version("pypdfium2")
                    if settings.pdf_engine == "pdfium"
                    else "2.135.0",
                    "layout": "cairn-pdf-layout/4",
                    "routing": pdf_routing_rule,
                    "baseline_package": package_version("pypdfium2"),
                    "render_scale": 2.0,
                    "ocr_text_threshold": 8,
                    "ocr_identity": None
                    if not settings.ocr_enabled
                    else digest(getattr(pdf, "ocr_identity", None))
                    if settings.pdf_engine == "docling" and settings.ocr_enabled
                    else ocr_fp,
                }
                if parser is pdf
                else {
                    "engine": "docling",
                    "model_manifest": parser.identity,
                    "fallback": parser.fallback_implementation,
                }
                if isinstance(parser, DoclingOfficeParser)
                else {},
            ),
        )
    if ocr is not None:
        registry.register(
            ImageOcrParser(ocr),
            descriptor=ParserDescriptor(
                implementation="cairn/ImageOcrParser/2", options={"ocr_identity": ocr_fp}
            ),
        )
    return registry
