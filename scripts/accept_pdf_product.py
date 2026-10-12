"""Disposable real-ingress PDF lifecycle and browser evidence; never truncates databases."""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zlib
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont


def scanned_pdf() -> bytes:
    image = Image.new("L", (1000, 1400), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 35)
    draw.text((80, 100), "Cairn PDF stage scanned evidence", font=font, fill="black")
    draw.text((80, 180), "The checkpoint constellation is Orion.", font=font, fill="black")
    pixels = zlib.compress(image.tobytes())
    content = b"q 612 0 0 792 0 0 cm /Im1 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 1000 /Height 1400 "
        b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length "
        + str(len(pixels)).encode()
        + b" >>\nstream\n"
        + pixels
        + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def main() -> None:
    import pypdfium2 as pdfium
    from playwright.sync_api import expect, sync_playwright
    from tests.unit.ingestion.test_pdf import _text_pdf

    evidence = Path(
        os.environ.get("CAIRN_PDF_ACCEPTANCE_OUTPUT", "data/acceptance-pdf-stage-20261009/product")
    )
    evidence.mkdir(parents=True, exist_ok=True)
    password = os.environ["CAIRN_PDF_ACCEPTANCE_PASSWORD"]
    records = []
    with httpx.Client(base_url="http://127.0.0.1:38080", timeout=60) as admin:
        response = admin.post("/v1/auth/login", json={"username": "admin", "password": password})
        response.raise_for_status()
        headers = {"X-CSRF-Token": admin.cookies["cairn_csrf"]}
        options = admin.get("/v1/knowledge-base-options").json()
        model = options["models"][0]
        vector = next(item for item in options["bindings"] if item["kind"] == "vector")
        objects = next(item for item in options["bindings"] if item["kind"] == "object")
        created = admin.post(
            "/v1/knowledge-bases",
            headers=headers,
            json={
                "name": f"PDF stage acceptance {int(time.time())}",
                "embedding_model_id": model["id"],
                "vector_binding_id": vector["id"],
                "object_binding_id": objects["id"],
            },
        )
        created.raise_for_status()
        kb = created.json()["id"]
        (evidence / "created.json").write_text(json.dumps({"kb_id": kb}), encoding="utf-8")
        try:
            native = _text_pdf("The Cairn PDF acceptance planet is Saturn.")
            doc = pdfium.PdfDocument(native)
            page = doc[0]
            page.set_rotation(90)
            page.close()
            rotated = io.BytesIO()
            doc.save(rotated)
            doc.close()
            financial = Path(
                "data/acceptance-pdf-20260921/corpus/documents/financial_table_heavy/financial_table_heavy-05.pdf"
            ).read_bytes()
            sources = [
                ("native", native),
                ("rotated", rotated.getvalue()),
                ("scan", scanned_pdf()),
                ("table", financial),
            ]
            if os.environ.get("CAIRN_PDF_ACCEPTANCE_MODEL") == "1":
                sources.append(
                    (
                        "formula",
                        Path(
                            "data/acceptance-pdf-20260921/corpus/documents/multicolumn/multicolumn-01.pdf"
                        ).read_bytes(),
                    )
                )
            for name, source in sources:
                uploaded = admin.post(
                    f"/v1/knowledge-bases/{kb}/documents/upload",
                    headers=headers,
                    files={"file": (f"{name}.pdf", source, "application/pdf")},
                )
                uploaded.raise_for_status()
                identifier = uploaded.json()["document"]["id"]
                for _attempt in range(120):
                    status = admin.get(f"/v1/knowledge-bases/{kb}/documents/{identifier}").json()
                    if status["state"] in ("indexed", "failed"):
                        break
                    time.sleep(1)
                if (
                    status["state"] == "failed"
                    and os.environ.get("CAIRN_PDF_ACCEPTANCE_MODEL") == "1"
                ):
                    records.append(
                        {
                            "name": name,
                            "document_id": identifier,
                            "sha256": hashlib.sha256(source).hexdigest(),
                            "status": "failed",
                            "code": status.get("error_code"),
                        }
                    )
                    continue
                assert status["state"] == "indexed", status.get("error_code")
                summary = admin.get(f"/v1/documents/{identifier}/parse")
                summary.raise_for_status()
                version = summary.json()["parse_version"]
                params = {"view": "active", "parse_version": version}
                elements = admin.get(f"/v1/documents/{identifier}/parse/elements", params=params)
                elements.raise_for_status()
                for element in elements.json()["items"]:
                    if element.get("asset_id"):
                        asset = admin.get(
                            f"/v1/documents/{identifier}/parse/assets/{element['asset_id']}",
                            params=params,
                        )
                        asset.raise_for_status()
                        assert hashlib.sha256(asset.content).hexdigest() == element["asset_id"]
                        assert all(
                            value.strip().lower() == "nosniff"
                            for value in asset.headers.get("x-content-type-options", "").split(",")
                        )
                        (evidence / f"{name}-{element['asset_id']}.png").write_bytes(asset.content)
                preview = admin.get(
                    f"/v1/documents/{identifier}/parse/pages/1/preview", params=params
                )
                preview.raise_for_status()
                (evidence / f"{name}-preview.png").write_bytes(preview.content)
                record = {
                    "name": name,
                    "document_id": identifier,
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "summary": summary.json(),
                    "elements": elements.json(),
                }
                records.append(record)
            query = admin.post(
                "/v1/retrieval/query",
                headers=headers,
                json={
                    "query": "Cairn PDF acceptance planet Saturn",
                    "targets": [{"knowledge_base_id": kb}],
                    "rerank": {"enabled": False},
                },
            )
            query_attempts = [{"status": query.status_code}]
            for _retry in range(2):
                if not query.is_error:
                    break
                time.sleep(2)
                query = admin.post(
                    "/v1/retrieval/query",
                    headers=headers,
                    json={
                        "query": "Cairn PDF acceptance planet Saturn",
                        "targets": [{"knowledge_base_id": kb}],
                        "rerank": {"enabled": False},
                    },
                )
                query_attempts.append({"status": query.status_code})
            (evidence / "query-attempts.json").write_text(json.dumps(query_attempts))
            query.raise_for_status()
            records.append({"query": query.json()})
            (evidence / "results.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, channel="chrome")
                context = browser.new_context(viewport={"width": 1440, "height": 1100})
                context.add_init_script("localStorage.setItem('cairn.locale','en-US')")
                page = context.new_page()
                page.goto("http://127.0.0.1:38080/login")
                page.locator('[data-test="username"]').fill("admin")
                page.locator('[data-test="password"]').fill(password)
                page.locator('[data-test="submit"]').click()
                page.wait_for_url("http://127.0.0.1:38080/", timeout=30000)
                for record in records[:-1]:
                    if record.get("status") == "failed":
                        continue
                    page.goto(f"http://127.0.0.1:38080/knowledge/{kb}")
                    page.locator(f'[data-test="doc-row-{record["document_id"]}"]').click()
                    panel = page.locator('[data-test="parse-preview"]')
                    expect(panel.locator("img")).to_be_visible(timeout=30000)
                    page.wait_for_function(
                        "document.querySelector('[data-test=parse-preview] img')?.naturalWidth > 0"
                    )
                    panel.screenshot(path=str(evidence / f"{record['name']}-browser.png"))
                browser.close()
            (evidence / "results.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
            indexed_count = sum(item.get("status") != "failed" for item in records[:-1])
            failed_count = sum(item.get("status") == "failed" for item in records[:-1])
            print(
                f"Tested {len(sources)} PDFs; indexed={indexed_count}, "
                f"failed={failed_count}; KB {kb}"
            )
        finally:
            deletion = admin.delete(f"/v1/knowledge-bases/{kb}", headers=headers)
            deletion.raise_for_status()
            (evidence / "cleanup-request.json").write_text(
                json.dumps({"kb_id": kb, "http_status": deletion.status_code})
            )


if __name__ == "__main__":
    main()
