"""Route PDFs to Docling, except scans that already carry a trustworthy text layer.

The product parses PDFs with Docling. Docling re-runs OCR on every bitmap page even
when the producer already embedded an OCR text layer (archive scans, scanner output),
which costs about 6.5 seconds per page for text the pdfium baseline reads in
milliseconds. This router inspects a document with the baseline first and, only when
every content page is a full-page scan whose embedded text layer looks like real
text, returns the baseline parse. Everything else, including scans without text,
goes to Docling. The decision is recorded in ``metadata["pdf_routing"]`` so citations
and reparse decisions stay auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import JsonValue

from cairn.ingestion.base import ParseContext, ParsedDocument, Parser
from cairn.ingestion.pdf import PdfOcrRequired, PdfPageFacts, PdfParser

ROUTING_RULE = "scan-text-layer/1"


@dataclass(frozen=True, slots=True)
class ScanTextLayerPolicy:
    """Thresholds a document must clear before its embedded text layer is trusted."""

    min_largest_raster_coverage: float = 0.8
    min_characters_per_page: int = 200
    min_alphanumeric_ratio: float = 0.5
    min_scan_page_fraction: float = 0.9


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    engine: str
    reason: str
    content_pages: int
    scan_pages: int
    text_layer_pages: int

    def metadata(self) -> dict[str, JsonValue]:
        return {
            "rule": ROUTING_RULE,
            "engine": self.engine,
            "reason": self.reason,
            "content_pages": self.content_pages,
            "scan_pages": self.scan_pages,
            "text_layer_pages": self.text_layer_pages,
        }


def decide(pages: tuple[PdfPageFacts, ...], policy: ScanTextLayerPolicy) -> RoutingDecision:
    """Choose the engine for one document from its cheap per-page facts."""
    content = [page for page in pages if page.has_page_objects]
    scan = [
        page
        for page in content
        if page.largest_raster_coverage >= policy.min_largest_raster_coverage
    ]
    trusted = [page for page in scan if _text_layer_looks_real(page, policy)]
    counts = (len(content), len(scan), len(trusted))
    if not content:
        return RoutingDecision("docling", "no content pages", *counts)
    if len(scan) < policy.min_scan_page_fraction * len(content):
        return RoutingDecision("docling", "document is not a full-page scan", *counts)
    if len(trusted) != len(scan):
        return RoutingDecision("docling", "some scanned pages lack a usable text layer", *counts)
    return RoutingDecision("pdfium-text-layer", "every scanned page carries a text layer", *counts)


def _text_layer_looks_real(page: PdfPageFacts, policy: ScanTextLayerPolicy) -> bool:
    if page.extracted_characters < policy.min_characters_per_page:
        return False
    visible = [char for char in page.text if not char.isspace()]
    if not visible:
        return False
    alphanumeric = sum(1 for char in visible if char.isalnum())
    return alphanumeric / len(visible) >= policy.min_alphanumeric_ratio


class ScanTextLayerRoutedPdfParser:
    """Docling for everything, except scans whose text layer the baseline can read."""

    supported_mimes: ClassVar[frozenset[str]] = frozenset({"application/pdf"})

    def __init__(
        self,
        *,
        docling: Parser,
        baseline: PdfParser,
        policy: ScanTextLayerPolicy | None = None,
    ) -> None:
        self._docling = docling
        self._baseline = baseline
        self._policy = policy or ScanTextLayerPolicy()
        # The registry reads budget limits and model identity from the PDF parser.
        self._limits: Any = getattr(docling, "_limits", None) or getattr(baseline, "_limits", None)
        self.identity: str | None = getattr(docling, "identity", None)
        self.ocr_identity: Any = getattr(docling, "ocr_identity", None)
        self.last_decision: RoutingDecision | None = None

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        decision = decide(await self._baseline.inspect(data), self._policy)
        if decision.engine == "pdfium-text-layer":
            try:
                parsed = await self._baseline.parse(data, ctx)
            except PdfOcrRequired:
                decision = RoutingDecision(
                    "docling",
                    "baseline still required OCR",
                    decision.content_pages,
                    decision.scan_pages,
                    decision.text_layer_pages,
                )
            else:
                return self._record(parsed, decision)
        return self._record(await self._docling.parse(data, ctx), decision)

    def _record(self, parsed: ParsedDocument, decision: RoutingDecision) -> ParsedDocument:
        parsed.metadata["pdf_routing"] = decision.metadata()
        self.last_decision = decision
        return parsed

    async def aclose(self) -> None:
        closer = getattr(self._docling, "aclose", None)
        if closer is not None:
            await closer()
