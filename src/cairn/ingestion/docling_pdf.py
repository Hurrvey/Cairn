"""Isolated standard Docling adapter; Docling is never imported in this process."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from cairn.core.document_ir import Element, Formula, Location, Provenance, Table, TableCell
from cairn.core.native_process import kill_tree, spawn_supervised
from cairn.ingestion.base import Asset, ParseContext, ParsedDocument
from cairn.ingestion.pdf import (
    PdfCorruptFile,
    PdfEngineUnavailable,
    PdfLimits,
    PdfTimedOut,
    PdfTooLarge,
)
from cairn.ingestion.process import (
    ProcessOutputTooLarge,
    ProcessSupervisionUnavailable,
    ProcessTimedOut,
    run_bounded,
)


class PdfUnsafeProjection(PdfCorruptFile):
    code = "PDF_UNSAFE_PROJECTION"
    title = "Extracted page provenance cannot be projected safely; the document was not indexed."


def _dedupe_cell_text(text: str) -> str:
    """Drop overlapping text runs that a source PDF draws more than once.

    Some PDFs repeat a value at nearly the same spot; Docling folds every
    overlapping run into one cell, so a cell can repeat a token. Collapse
    repeated tokens keeping first-seen order; leave unique cells untouched.
    """
    tokens = text.split()
    if len(tokens) < 2 or len(set(tokens)) == len(tokens):
        return text
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return " ".join(unique)


def _cell_locations(cell: dict[str, Any], table_locations: list[Location]) -> tuple[Location, ...]:
    box = cell.get("bbox")
    if box is None or len(table_locations) != 1:
        return ()
    location = table_locations[0]
    if location.rotation or location.crop not in (None, (0, 0, location.width, location.height)):
        return ()
    coords = (box["l"], box["b"], box["r"], box["t"])
    if box.get("coord_origin") == "TOPLEFT":
        coords = (box["l"], location.height - box["b"], box["r"], location.height - box["t"])
    return (
        Location(page=location.page, width=location.width, height=location.height, bbox=coords),
    )


def normalize_docling(payload: dict[str, Any]) -> ParsedDocument:
    try:
        pages = payload["pages"]
        if (
            len(pages) > 500
            or len(payload["items"]) > 100000
            or sum(len(item.get("data", {}).get("table_cells", [])) for item in payload["items"])
            > 100000
        ):
            raise PdfTooLarge()
        elements = []
        local_ids = {item["self_ref"] for item in payload["items"]}
        for order, item in enumerate(payload["items"]):
            label = item["label"]
            kind = {
                "section_header": "heading",
                "title": "heading",
                "list_item": "list",
                "picture": "image",
                "formula": "formula",
                "table": "table",
                "code": "code",
            }.get(label, "text")
            text, orig = item.get("text", ""), item.get("orig")
            locations = []
            for prov in item.get("prov", []):
                page = prov["page_no"]
                size = pages[str(page)]["size"]
                box = prov["bbox"]
                coords = (box["l"], box["b"], box["r"], box["t"])
                if box.get("coord_origin") == "TOPLEFT":
                    coords = (
                        box["l"],
                        size["height"] - box["b"],
                        box["r"],
                        size["height"] - box["t"],
                    )
                geometry = (
                    payload.get("pdf_pages", [])[page - 1] if payload.get("pdf_pages") else None
                )
                crop = None
                rotation = 0
                width, height = size["width"], size["height"]
                if geometry is not None:
                    crop, rotation = geometry["crop"], geometry["rotation"]
                    width, height = geometry["width"], geometry["height"]
                    x0, y0, x1, y1 = coords
                    if rotation == 0:
                        coords = (crop[0] + x0, crop[1] + y0, crop[0] + x1, crop[1] + y1)
                    elif rotation == 90:
                        coords = (
                            crop[0] + width - y1,
                            crop[1] + x0,
                            crop[0] + width - y0,
                            crop[1] + x1,
                        )
                    elif rotation == 180:
                        coords = (
                            crop[0] + width - x1,
                            crop[1] + height - y1,
                            crop[0] + width - x0,
                            crop[1] + height - y0,
                        )
                    else:
                        coords = (
                            crop[0] + y0,
                            crop[1] + height - x1,
                            crop[0] + y1,
                            crop[1] + height - x0,
                        )
                # Docling charspans are page-local; they cannot prove one global
                # span across pages, so keep the geometry and drop the span.
                locations.append(
                    Location(
                        page=page,
                        width=width,
                        height=height,
                        bbox=coords,
                        crop=crop,
                        rotation=rotation,
                    )
                )
            if not locations:
                raise ValueError("PDF element lacks provenance")
            table = None
            formula = None
            if kind == "table":
                grid = item["data"]
                table = Table(
                    rows=grid["num_rows"],
                    columns=grid["num_cols"],
                    cells=[
                        TableCell(
                            text=_dedupe_cell_text(cell["text"]),
                            row_start=cell["start_row_offset_idx"],
                            row_end=cell["end_row_offset_idx"],
                            col_start=cell["start_col_offset_idx"],
                            col_end=cell["end_col_offset_idx"],
                            row_span=cell.get("row_span"),
                            col_span=cell.get("col_span"),
                            locations=_cell_locations(cell, locations),
                        )
                        for cell in grid["table_cells"]
                    ],
                )
            elif kind == "formula":
                formula = Formula(
                    native=orig or text,
                    recognized=None,
                    status="unrecognized" if orig or text else "missing",
                )
            elements.append(
                Element(
                    local_id=item["self_ref"],
                    caption_ids=tuple(
                        ref["$ref"]
                        for ref in item.get("captions", [])
                        if ref.get("$ref") in local_ids
                    ),
                    order=order,
                    type=kind,
                    text=text,
                    orig=orig,
                    locations=locations,
                    table=table,
                    formula=formula,
                    asset_id=item.get("asset_id"),
                    asset_status=("available" if item.get("asset_id") else "unavailable")
                    if kind == "image"
                    else None,
                    provenance=[Provenance(method="model-derived", producer="docling/2.135.0")],
                )
            )
        return ParsedDocument(
            markdown="\n\n".join(item.search_text for item in elements),
            blocks=[],
            pages=len(pages),
            elements=elements,
            metadata={
                "format": "pdf",
                "pdf_engine": "docling",
                "resume_granularity": "document",
                "warnings": ["Extraction quality is unscored."],
                "pdf_pages": payload.get("pdf_pages", []),
            },
            assets=[
                Asset(
                    name=item["name"],
                    mime_type=item["mime_type"],
                    data=base64.b64decode(item["data"], validate=True),
                    page=item.get("page"),
                )
                for item in payload.get("assets", [])
            ],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise PdfUnsafeProjection() from exc


class _PersistentWorker:
    """One long-lived Docling child: models load once, requests are serialized."""

    def __init__(
        self, command: tuple[str, ...], *, max_memory_bytes: int, max_output_bytes: int
    ) -> None:
        self._command = command
        self._max_memory_bytes = max_memory_bytes
        self._max_output_bytes = max_output_bytes
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self, *, timeout_seconds: float) -> None:
        process = await spawn_supervised(self._command, max_memory_bytes=self._max_memory_bytes)
        self._process = process
        assert process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain(process.stderr))
        try:
            async with asyncio.timeout(timeout_seconds):
                ready = json.loads(await self._read_frame(65536))
        except TimeoutError as exc:
            await self.kill()
            raise ProcessTimedOut from exc
        except (asyncio.IncompleteReadError, ValueError, OSError, ProcessOutputTooLarge) as exc:
            await self.kill()
            raise ProcessSupervisionUnavailable() from exc
        if not isinstance(ready, dict) or not ready.get("ready"):
            await self.kill()
            raise ProcessSupervisionUnavailable()

    async def request(self, data: bytes, request_id: int, *, timeout_seconds: float) -> bytes:
        process = self._process
        assert process is not None and process.stdin is not None
        meta = json.dumps({"request_id": request_id}).encode()
        try:
            async with asyncio.timeout(timeout_seconds):
                process.stdin.write(struct.pack(">I", len(meta)) + meta)
                process.stdin.write(struct.pack(">I", len(data)))
                for offset in range(0, len(data), 65536):
                    process.stdin.write(data[offset : offset + 65536])
                    await process.stdin.drain()
                await process.stdin.drain()
                return await self._read_frame(self._max_output_bytes)
        except TimeoutError as exc:
            await self.kill()
            raise ProcessTimedOut from exc
        except ProcessOutputTooLarge:
            await self.kill()
            raise
        except (asyncio.IncompleteReadError, OSError) as exc:
            await self.kill()
            raise ProcessSupervisionUnavailable() from exc

    async def _read_frame(self, maximum: int) -> bytes:
        process = self._process
        assert process is not None and process.stdout is not None
        (length,) = struct.unpack(">I", await process.stdout.readexactly(4))
        if length > maximum:
            raise ProcessOutputTooLarge
        return await process.stdout.readexactly(length)

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            self._stderr_tail.extend(chunk)
            del self._stderr_tail[:-4096]

    async def kill(self) -> None:
        process, self._process = self._process, None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        if process is not None:
            await kill_tree(process)

    async def aclose(self) -> None:
        """Ask the child to exit by closing stdin, then reap or kill it."""
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            with suppress(BrokenPipeError, ConnectionResetError, OSError):
                process.stdin.close()
        with suppress(TimeoutError):
            async with asyncio.timeout(0.5):
                await process.wait()
        await self.kill()


class DoclingPdfParser:
    supported_mimes: ClassVar[frozenset[str]] = frozenset({"application/pdf"})

    def __init__(
        self,
        *,
        python: str = sys.executable,
        assets_path: str,
        manifest_path: str,
        limits: PdfLimits | None = None,
        ocr: bool = True,
        languages: tuple[str, ...] = ("eng", "chi_sim", "chi_tra"),
        psm: int = 3,
        persistent: bool = False,
    ) -> None:
        self._python = python
        self._persistent = persistent
        self._worker_script = str(Path(__file__).with_name("docling_worker.py"))
        self._child: _PersistentWorker | None = None
        self._child_lock = asyncio.Lock()
        self._request_id = 0
        self._assets = assets_path
        self._manifest = manifest_path
        self._limits = limits or PdfLimits(max_memory_bytes=4 * 1024**3, timeout_seconds=600)
        self._ocr = ocr
        self._languages, self._psm = languages, psm
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            if manifest["docling"] != "2.135.0" or not manifest.get("identity"):
                raise ValueError("invalid worker manifest")
            canonical = {key: value for key, value in manifest.items() if key != "identity"}
            if (
                hashlib.sha256(
                    json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                != manifest["identity"]
            ):
                raise ValueError("worker manifest identity mismatch")
            root = Path(assets_path).resolve()
            for entry in manifest["files"]:
                asset = (root / entry["path"]).resolve()
                if not asset.is_relative_to(root):
                    raise ValueError("asset path escapes model root")
                with asset.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                        raise ValueError("model asset checksum mismatch")
            self.identity = manifest["identity"]
            self.ocr_identity = manifest.get("ocr_environment")
            if ocr and (
                not isinstance(self.ocr_identity, dict)
                or any(
                    language not in self.ocr_identity.get("traineddata", {})
                    for language in languages
                )
            ):
                raise ValueError("selected worker OCR assets are not prepared")
            if ocr:
                helper = Path(__file__).parents[1] / "core" / "ocr_environment.py"
                probe = subprocess.run(  # noqa: S603 -- operator-owned isolated Python
                    (self._python, str(helper), json.dumps(self.ocr_identity["languages"])),
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                if json.loads(probe.stdout) != self.ocr_identity:
                    raise ValueError("selected worker OCR environment changed")
        except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
            raise PdfEngineUnavailable() from exc

    async def _request_once(self, data: bytes, config: dict[str, Any]) -> bytes:
        """One child per document: pays the model load every time."""
        try:
            code, output, _stderr = await run_bounded(
                (self._python, self._worker_script, json.dumps(config)),
                data,
                timeout_seconds=self._limits.timeout_seconds,
                max_output_bytes=self._limits.max_output_bytes,
                max_memory_bytes=self._limits.max_memory_bytes,
            )
        except ProcessTimedOut as exc:
            raise PdfTimedOut() from exc
        except ProcessOutputTooLarge as exc:
            raise PdfTooLarge() from exc
        except (OSError, ProcessSupervisionUnavailable) as exc:
            raise PdfEngineUnavailable() from exc
        if code == 3:
            raise PdfEngineUnavailable()
        if code in (4, -9, 137):
            raise PdfTooLarge()
        if code:
            raise PdfCorruptFile()
        return output

    async def _request_persistent(self, data: bytes, config: dict[str, Any]) -> tuple[bytes, int]:
        """Reuse one warm child; respawn it after a timeout, crash or oversized reply."""
        from cairn.ingestion.ocr import TesseractOcrEngine

        # The converter is built once, so OCR runs with the full configured language
        # set rather than a per-document narrowing.
        config = dict(config, persistent=True)
        config["ocr_languages"] = (
            TesseractOcrEngine(languages=self._languages, psm=self._psm)._language(None).split("+")
            if self._ocr
            else []
        )
        async with self._child_lock:
            if self._child is None or not self._child.alive:
                child = _PersistentWorker(
                    (self._python, self._worker_script, json.dumps(config)),
                    max_memory_bytes=self._limits.max_memory_bytes,
                    max_output_bytes=self._limits.max_output_bytes,
                )
                try:
                    await child.start(timeout_seconds=self._limits.timeout_seconds)
                except (ProcessTimedOut, OSError, ProcessSupervisionUnavailable) as exc:
                    raise PdfEngineUnavailable() from exc
                self._child = child
            self._request_id += 1
            request_id = self._request_id
            try:
                output = await self._child.request(
                    data, request_id, timeout_seconds=self._limits.timeout_seconds
                )
            except ProcessTimedOut as exc:
                raise PdfTimedOut() from exc
            except ProcessOutputTooLarge as exc:
                raise PdfTooLarge() from exc
            except (OSError, ProcessSupervisionUnavailable) as exc:
                raise PdfEngineUnavailable() from exc
        return output, request_id

    async def aclose(self) -> None:
        async with self._child_lock:
            if self._child is not None:
                await self._child.aclose()
                self._child = None

    async def parse(self, data: bytes, ctx: ParseContext) -> ParsedDocument:
        from cairn.ingestion.ocr_cache import current_hooks

        hooks = current_hooks.get()
        if hooks:
            await hooks.progress(None, None, "converting")
        if len(data) > self._limits.max_input_bytes:
            raise PdfTooLarge()
        if not data.startswith(b"%PDF-"):
            raise PdfCorruptFile()
        config = {
            "assets": self._assets,
            "manifest": self._manifest,
            "identity": self.identity,
            "ocr": self._ocr,
            "max_pages": self._limits.max_pages,
            "max_memory_bytes": self._limits.max_memory_bytes,
            "max_output_bytes": self._limits.max_output_bytes,
        }
        from cairn.ingestion.ocr import TesseractOcrEngine
        from cairn.ingestion.pdf_budget import validate_pdf_budget, worker_limits

        config.update(worker_limits(self._limits))
        config["ocr_languages"] = (
            TesseractOcrEngine(languages=self._languages, psm=self._psm)
            ._language(None if ctx.language == "und" else ctx.language)
            .split("+")
            if self._ocr
            else []
        )
        config["ocr_psm"] = self._psm
        request_id: int | None = None
        if self._persistent:
            output, request_id = await self._request_persistent(data, config)
        else:
            output = await self._request_once(data, config)
        try:
            payload = json.loads(output)
        except (ValueError, TypeError) as exc:
            raise PdfCorruptFile() from exc
        if not isinstance(payload, dict):
            raise PdfCorruptFile()
        error = payload.get("error")
        if error is not None:
            if error == 3:
                raise PdfEngineUnavailable()
            if error == 4:
                raise PdfTooLarge()
            raise PdfCorruptFile()
        if request_id is not None and payload.get("request_id") != request_id:
            raise PdfEngineUnavailable()
        if payload.get("identity") != self.identity:
            raise PdfEngineUnavailable()
        try:
            parsed = normalize_docling(payload)
        except PdfUnsafeProjection:
            if hooks:
                await hooks.diagnostic(payload)
            raise
        from cairn.ingestion.ocr_cache import current_hooks

        hooks = current_hooks.get()
        if hooks:
            await hooks.progress(parsed.pages, parsed.pages, "normalized")
        parsed.language = ctx.language
        parsed.metadata["pdf_budget_facts"] = payload.get("pdf_budget_facts")
        parsed.metadata["ocr_transcripts"] = payload.get("ocr_transcripts", {})
        parsed.metadata["ocr_transcription_status"] = payload.get(
            "ocr_transcription_status", "unsupported"
        )
        parsed.metadata["worker_output_bytes"] = len(output)
        parsed.metadata["pdf_worker_pid"] = payload.get("worker_pid")
        parsed.elements = [
            item.model_copy(update={"source_url": ctx.source_url}) for item in parsed.elements
        ]
        if len(parsed.markdown) > self._limits.max_text_chars:
            raise PdfTooLarge()
        validate_pdf_budget(parsed, self._limits)
        return parsed
