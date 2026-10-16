"""End-to-end smoke for the annotation studio.

Drives the studio UI with Playwright against a scratch copy of the working
gold file, then asserts the file the server wrote passes the frozen validator.
The real working file is never written.

Usage (repository root):

    uv run python scripts/annotation_studio_smoke.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from playwright.sync_api import expect, sync_playwright  # noqa: E402
from scripts.pdf_annotation_helper import (  # noqa: E402
    DEFAULT_ANNOTATIONS,
    DEFAULT_MANIFEST,
    DEFAULT_SKELETON,
    load_documents,
)
from scripts.pdf_gold_metrics import validate_annotations  # noqa: E402

SCRATCH = Path("data/annotations/_studio_smoke.json")
SCREENSHOT = Path("data/annotations/studio-smoke.png")
PORT = 39317
DOC = "cjk-06"
OCR_TEXT = "Line one. Line two."


def wait_for_server(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    raise SystemExit(f"studio did not answer at {url}")


def wait_for_save(path: Path, timeout: float = 25.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            last = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(0.3)
            continue
        for entry in last.get("documents", []):
            if entry.get("method") != "independent_human":
                continue
            for page in entry.get("pages", []):
                for table in page.get("tables", []):
                    if len(table.get("cells", [])) == 6:
                        return last
        time.sleep(0.3)
    raise AssertionError(f"expected save never appeared: {last}")


def drag(page: Any, x1: float, y1: float, x2: float, y2: float) -> None:
    box = page.locator("#overlay").bounding_box()
    assert box is not None
    page.mouse.move(box["x"] + x1, box["y"] + y1)
    page.mouse.down()
    page.mouse.move(box["x"] + x2, box["y"] + y2, steps=6)
    page.mouse.up()


def set_number(page: Any, test: str, value: str) -> None:
    page.locator(f'[data-test="{test}"]').evaluate(
        "(el, v) => { el.value = v; el.dispatchEvent(new Event('change', { bubbles: true })); }",
        value,
    )


def run_ui(playwright: Any) -> None:
    browser = playwright.chromium.launch()
    try:
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda err: print("PAGEERROR:", err))
        page.on(
            "console",
            lambda msg: print("CONSOLE ERROR:", msg.text) if msg.type == "error" else None,
        )
        page.goto(f"http://127.0.0.1:{PORT}/")
        page.wait_for_selector('[data-test="doc-item"]')
        expect(page.locator('[data-test="doc-item"]')).to_have_count(30)
        page.click(f'[data-test="doc-item"][data-id="{DOC}"]')
        page.wait_for_function(
            "() => { const img = document.getElementById('pageImg');"
            " return !!img && img.naturalWidth > 0; }"
        )

        # a table with a 2x3 grid and one typed cell
        page.click('[data-test="tool-table"]')
        drag(page, 100, 80, 500, 260)
        page.wait_for_selector('[data-test="cell-grid"]')
        set_number(page, "rows", "2")
        set_number(page, "cols", "3")
        page.click('[data-test="gen-cells"]')
        expect(page.locator('[data-test="cell-grid"] .cell.has')).to_have_count(6)
        page.locator('[data-test="cell-grid"] .cell').first.click()
        cell_text = page.locator('[data-test="cell-text"]')
        cell_text.fill("Metric")
        cell_text.press("Enter")

        # order box, undo, redo
        page.click('[data-test="tool-order"]')
        drag(page, 120, 320, 520, 420)
        expect(page.locator("#orderPanel .item")).to_have_count(1)
        page.keyboard.press("Control+z")
        expect(page.locator("#orderPanel .item")).to_have_count(0)
        page.keyboard.press("Control+Shift+z")
        expect(page.locator("#orderPanel .item")).to_have_count(1)

        # geometry box
        page.click('[data-test="tool-geometry"]')
        drag(page, 140, 440, 540, 520)
        expect(page.locator("#geometryPanel .item")).to_have_count(1)

        # drawing a box covers the page automatically
        expect(page.locator('[data-test="covered-toggle"]')).to_be_checked()

        # OCR transcription
        page.click('[data-test="ocr-toggle"]')
        page.locator('[data-test="ocr-text"]').fill(OCR_TEXT)
        page.locator("#pageInfo h3").click()
        page.wait_for_timeout(1500)
        page.screenshot(path=str(REPO_ROOT / SCREENSHOT))
    finally:
        browser.close()


def verify(payload: dict[str, Any]) -> None:
    documents = load_documents(DEFAULT_MANIFEST)
    accepted = validate_annotations(payload, documents)
    item = next(d for d in documents if d["id"] == DOC)
    entry = accepted[item["sha256"]]
    assert entry["method"] == "independent_human"
    assert entry["annotation_version"] == "r1"
    assert entry["covered_pages"] == [1]
    page = entry["pages"][0]
    assert page["page"] == 1
    assert len(page["tables"]) == 1
    table = page["tables"][0]
    assert table["rows"] == 2 and table["cols"] == 3
    assert len(table["cells"]) == 6
    first = next(c for c in table["cells"] if c["row_start"] == 0 and c["col_start"] == 0)
    assert first["text"] == "Metric"
    x0, y0, x1, y1 = table["bbox"]
    assert 0 <= x0 < x1 <= page["width"]
    assert 0 <= y0 < y1 <= page["height"]
    assert len(page["order"]) == 1
    assert len(page["geometry"]) == 1
    assert page["ocr_text"] == OCR_TEXT
    print(
        f"verified: table bbox {table['bbox']} {table['rows']}x{table['cols']}, "
        f"{len(table['cells'])} cells, order {len(page['order'])}, "
        f"geometry {len(page['geometry'])}"
    )


def main() -> None:
    source = DEFAULT_ANNOTATIONS if DEFAULT_ANNOTATIONS.is_file() else DEFAULT_SKELETON
    before = source.read_bytes()
    SCRATCH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, SCRATCH)

    server = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "scripts/annotation_studio.py",
            "--no-browser",
            "--port",
            str(PORT),
            "--annotations",
            str(SCRATCH),
        ],
        cwd=str(REPO_ROOT),
    )
    try:
        wait_for_server(f"http://127.0.0.1:{PORT}/api/state")
        with sync_playwright() as playwright:
            run_ui(playwright)
        payload = wait_for_save(SCRATCH)
        verify(payload)
        assert source.read_bytes() == before, "the working file must not be touched"
        print("studio smoke OK: draw, cells, undo/redo, ocr, autosave, validator")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        for leftover in (SCRATCH, SCRATCH.with_name(SCRATCH.name + ".bak")):
            leftover.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
