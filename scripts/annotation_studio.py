"""Local annotation studio: a browser UI over the human gold annotation kit.

Run from the repository root:

    uv run python scripts/annotation_studio.py

It serves http://127.0.0.1:39310/ with corpus pages rendered on demand and
writes validated edits back to the same annotations file the frozen scorer
reads (default ``data/annotations/annotations.gold.json``). Localhost only.

Endpoints:

    GET  /                        the single-page UI
    GET  /api/state               per-document coverage summary
    GET  /api/doc/<id>            page templates + current annotation entry
    GET  /api/page/<id>/<n>.png   rendered page, cached under data/annotations/cache
    PUT  /api/doc/<id>            replace one document's annotation (validated)
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.pdf_annotation_helper import (  # noqa: E402
    DEFAULT_ANNOTATIONS,
    DEFAULT_CORPUS_ROOT,
    DEFAULT_MANIFEST,
    DEFAULT_TEMPLATES,
    load_documents,
    locate,
)
from scripts.pdf_gold_metrics import validate_annotations  # noqa: E402

__all__ = ["Studio", "main", "merge_document", "summarize"]

UI_FILE = Path(__file__).resolve().with_name("annotation_studio.html")
MAX_BODY_BYTES = 16 * 1024 * 1024


def summarize(entry: dict[str, Any] | None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "covered": 0,
        "tables": 0,
        "cells": 0,
        "order": 0,
        "geometry": 0,
        "ocr": 0,
        "method": "pending",
    }
    if entry is None:
        return summary
    for page in entry.get("pages") or []:
        summary["tables"] += len(page.get("tables") or [])
        for table in page.get("tables") or []:
            summary["cells"] += len(table.get("cells") or [])
        summary["order"] += len(page.get("order") or [])
        summary["geometry"] += len(page.get("geometry") or [])
        if isinstance(page.get("ocr_text"), str):
            summary["ocr"] += 1
    summary["covered"] = len(entry.get("covered_pages") or [])
    summary["method"] = str(entry.get("method") or "pending")
    return summary


def merge_document(
    payload: dict[str, Any], item: dict[str, Any], annotation: dict[str, Any]
) -> None:
    if annotation.get("source_sha256") != item["sha256"]:
        raise ValueError("annotation source_sha256 does not match the document")
    documents = payload.get("documents")
    if not isinstance(documents, list):
        raise ValueError("annotations file has no documents list")
    for index, entry in enumerate(documents):
        if entry.get("source_sha256") == item["sha256"]:
            documents[index] = annotation
            return
    documents.append(annotation)


class Studio:
    """Reads the kit, renders pages, and writes validated annotation edits."""

    def __init__(
        self,
        *,
        manifest: Path,
        corpus_root: Path,
        annotations: Path,
        templates: Path,
        cache_dir: Path,
    ) -> None:
        self.documents = load_documents(manifest)
        self.by_id = {item["id"]: item for item in self.documents}
        self.corpus_root = corpus_root
        self.annotations = annotations
        self.cache_dir = cache_dir
        template_payload = json.loads(templates.read_text(encoding="utf-8"))
        self.template_pages: dict[str, dict[int, dict[str, Any]]] = {
            doc["id"]: {page["page"]: page for page in doc["pages"]}
            for doc in template_payload["documents"]
        }
        self.render_lock = threading.Lock()
        self.write_lock = threading.Lock()

    # -- reads -----------------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.annotations.read_text(encoding="utf-8"))
        return data

    def state(self) -> dict[str, Any]:
        entries = {entry.get("source_sha256"): entry for entry in self.payload()["documents"]}
        documents = []
        totals: dict[str, int] = {
            "documents": len(self.documents),
            "pages": 0,
            "covered": 0,
            "tables": 0,
            "cells": 0,
            "order": 0,
            "geometry": 0,
            "ocr": 0,
            "gold": 0,
        }
        for item in self.documents:
            summary = summarize(entries.get(item["sha256"]))
            documents.append(
                {
                    "id": item["id"],
                    "category": item.get("primary_category") or item.get("category"),
                    "pages": item["pages"],
                    **summary,
                }
            )
            totals["pages"] += int(item["pages"])
            for key in ("covered", "tables", "cells", "order", "geometry", "ocr"):
                totals[key] += int(summary[key])
            if summary["method"] == "independent_human":
                totals["gold"] += 1
        return {"documents": documents, "totals": totals, "annotations": str(self.annotations)}

    def doc(self, doc_id: str) -> dict[str, Any]:
        item = self.by_id.get(doc_id)
        if item is None:
            raise ValueError(f"unknown document {doc_id!r}")
        entry = next(
            (
                entry
                for entry in self.payload()["documents"]
                if entry.get("source_sha256") == item["sha256"]
            ),
            None,
        )
        templates = {
            page: {
                "width": value["width"],
                "height": value["height"],
                "rotation": value.get("rotation", 0),
            }
            for page, value in self.template_pages.get(doc_id, {}).items()
        }
        return {
            "id": doc_id,
            "category": item.get("primary_category") or item.get("category"),
            "pages": int(item["pages"]),
            "sha256": item["sha256"],
            "templates": templates,
            "annotation": entry,
        }

    def page_png(self, doc_id: str, page: int, scale: int) -> bytes:
        item = self.by_id.get(doc_id)
        if item is None:
            raise ValueError(f"unknown document {doc_id!r}")
        total = int(item["pages"])
        if not 1 <= page <= total:
            raise ValueError(f"page must be within 1..{total}")
        if scale not in (1, 2, 3):
            raise ValueError("scale must be 1, 2 or 3")
        pdf = locate(item, self.corpus_root)
        if not pdf.is_file():
            raise ValueError(f"missing source PDF: {pdf}")
        target = self.cache_dir / f"{item['sha256'][:16]}-p{page:04d}-s{scale}.png"
        if target.is_file():
            return target.read_bytes()
        with self.render_lock:
            if target.is_file():
                return target.read_bytes()
            import pypdfium2 as pdfium  # type: ignore[import-untyped]

            document = pdfium.PdfDocument(str(pdf))
            try:
                source = document[page - 1]
                source.set_rotation(0)  # the stored, unrotated annotation space
                image = source.render(scale=scale).to_pil().convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
            finally:
                document.close()
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            pending = target.with_suffix(".png.pending")
            pending.write_bytes(buffer.getvalue())
            os.replace(pending, target)
        return target.read_bytes()

    # -- writes ----------------------------------------------------------------

    def save(self, doc_id: str, annotation: Any) -> dict[str, Any]:
        item = self.by_id.get(doc_id)
        if item is None:
            raise ValueError(f"unknown document {doc_id!r}")
        if not isinstance(annotation, dict):
            raise ValueError("annotation must be an object")
        with self.write_lock:
            payload = self.payload()
            merge_document(payload, item, annotation)
            validate_annotations(payload, self.documents)
            backup = self.annotations.with_name(self.annotations.name + ".bak")
            if self.annotations.is_file():
                shutil.copyfile(self.annotations, backup)
            pending = self.annotations.with_name(self.annotations.name + ".pending")
            pending.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            os.replace(pending, self.annotations)
        print(f"saved {doc_id}: {len(annotation.get('covered_pages') or [])} page(s) covered")
        return self.state()


class StudioHandler(BaseHTTPRequestHandler):
    server_version = "AnnotationStudio/1.0"
    studio: Studio
    ui_path: Path

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str, *, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(
            code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._send(200, self.ui_path.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, self.studio.state())
            elif path.startswith("/api/doc/"):
                self._json(200, self.studio.doc(unquote(path[len("/api/doc/") :])))
            elif path.startswith("/api/page/"):
                rest = path[len("/api/page/") :]
                doc_id, _, name = rest.rpartition("/")
                page = int(name.removesuffix(".png"))
                scale = int(parse_qs(parsed.query).get("scale", ["2"])[0])
                body = self.studio.page_png(unquote(doc_id), page, scale)
                self._send(200, body, "image/png", cache=True)
            else:
                self._json(404, {"error": "not found"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/doc/"):
            self._json(404, {"error": "not found"})
            return
        doc_id = unquote(parsed.path[len("/api/doc/") :])
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(400, {"error": "request body missing or too large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            state = self.studio.save(doc_id, body.get("annotation"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception as exc:
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._json(200, {"ok": True, "state": state})


def main() -> None:
    cli = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    cli.add_argument("--host", default="127.0.0.1", help="bind address (localhost only by default)")
    cli.add_argument("--port", type=int, default=39310)
    cli.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    cli.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    cli.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    cli.add_argument("--templates", type=Path, default=DEFAULT_TEMPLATES)
    cli.add_argument("--cache", type=Path, default=Path("data/annotations/cache"))
    cli.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = cli.parse_args()

    if not args.annotations.is_file():
        raise SystemExit(
            f"missing {args.annotations}; create it first (see "
            f"scripts/pdf_annotation_helper.py status)"
        )
    if not UI_FILE.is_file():
        raise SystemExit(f"missing UI file: {UI_FILE}")

    studio = Studio(
        manifest=args.manifest,
        corpus_root=args.corpus_root,
        annotations=args.annotations,
        templates=args.templates,
        cache_dir=args.cache,
    )
    StudioHandler.studio = studio
    StudioHandler.ui_path = UI_FILE

    try:
        server = ThreadingHTTPServer((args.host, args.port), StudioHandler)
    except OSError as exc:
        raise SystemExit(f"cannot bind {args.host}:{args.port}: {exc}") from exc
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f"annotation studio: {url}")
    print(f"annotations file: {args.annotations}")
    print("Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
