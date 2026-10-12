"""The persistent Docling child keeps its models loaded and survives one bad document."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from cairn.ingestion.base import ParseContext
from cairn.ingestion.docling_pdf import DoclingPdfParser
from cairn.ingestion.ir import digest
from cairn.ingestion.pdf import PdfLimits, PdfTimedOut, PdfTooLarge

FAKE_CHILD = textwrap.dedent(
    """
    import json, os, struct, sys, time

    config = json.loads(sys.argv[1])
    assert config.get("persistent") is True
    inp, out = sys.stdin.buffer, sys.stdout.buffer

    def read_frame():
        header = inp.read(4)
        if len(header) < 4:
            return None
        (length,) = struct.unpack(">I", header)
        data = b""
        while len(data) < length:
            chunk = inp.read(length - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def write_frame(data):
        out.write(struct.pack(">I", len(data)) + data)
        out.flush()

    sys.stderr.write("loading weights once\\n")
    write_frame(json.dumps({"ready": True, "identity": config["identity"]}).encode())
    while True:
        meta = read_frame()
        if meta is None:
            break
        source = read_frame()
        if source is None:
            break
        request_id = json.loads(meta)["request_id"]
        if b"SLOW" in source:
            time.sleep(5)
        if b"BIG" in source:
            write_frame(b"x" * 100000)
            continue
        payload = {
            "request_id": request_id,
            "identity": config["identity"],
            "worker_pid": os.getpid(),
            "pages": {"1": {"size": {"width": 100, "height": 100}}},
            "items": [
                {
                    "self_ref": "#/texts/0",
                    "label": "text",
                    "text": source.decode("latin-1"),
                    "prov": [{"page_no": 1, "bbox": {"l": 0, "b": 0, "r": 80, "t": 20}}],
                }
            ],
            "pdf_budget_facts": {
                "schema": 1,
                "output_bytes": 500,
                "pages": [
                    {
                        "width": 100,
                        "height": 100,
                        "characters": 7,
                        "words": 1,
                        "objects": 1,
                        "rasters": 0,
                        "layout_blocks": 1,
                        "render_pixels": 0,
                        "render_bytes": 0,
                    }
                ],
            },
        }
        write_frame(json.dumps(payload).encode())
    """
)


def _parser(tmp_path: Path, *, timeout_seconds: float = 3.0) -> DoclingPdfParser:
    manifest = {"docling": "2.135.0", "files": [], "packages": {}}
    manifest["identity"] = digest(manifest)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    fake = tmp_path / "fake_child.py"
    fake.write_text(FAKE_CHILD, encoding="utf-8")
    parser = DoclingPdfParser(
        python=sys.executable,
        assets_path=str(tmp_path),
        manifest_path=str(manifest_path),
        ocr=False,
        languages=("eng",),
        persistent=True,
        limits=PdfLimits(
            timeout_seconds=timeout_seconds,
            max_output_bytes=50_000,
            max_memory_bytes=4 * 1024**3,
        ),
    )
    parser._worker_script = str(fake)
    return parser


async def test_one_child_serves_consecutive_documents(tmp_path: Path) -> None:
    parser = _parser(tmp_path)
    try:
        first = await parser.parse(b"%PDF-one", ParseContext())
        second = await parser.parse(b"%PDF-two", ParseContext())
    finally:
        await parser.aclose()
    assert first.markdown == "%PDF-one" and second.markdown == "%PDF-two"
    assert first.metadata["pdf_worker_pid"] == second.metadata["pdf_worker_pid"]
    assert parser._child is None


async def test_timeout_kills_the_child_and_the_next_document_respawns(tmp_path: Path) -> None:
    parser = _parser(tmp_path, timeout_seconds=1.0)
    try:
        before = await parser.parse(b"%PDF-ok", ParseContext())
        with pytest.raises(PdfTimedOut):
            await parser.parse(b"%PDF-SLOW", ParseContext())
        assert parser._child is None or not parser._child.alive
        after = await parser.parse(b"%PDF-again", ParseContext())
    finally:
        await parser.aclose()
    assert after.markdown == "%PDF-again"
    assert before.metadata["pdf_worker_pid"] != after.metadata["pdf_worker_pid"]


async def test_oversized_reply_is_too_large_and_the_child_is_replaced(tmp_path: Path) -> None:
    parser = _parser(tmp_path)
    try:
        with pytest.raises(PdfTooLarge):
            await parser.parse(b"%PDF-BIG", ParseContext())
        recovered = await parser.parse(b"%PDF-small", ParseContext())
    finally:
        await parser.aclose()
    assert recovered.markdown == "%PDF-small"
