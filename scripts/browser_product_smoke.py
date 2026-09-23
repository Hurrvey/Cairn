"""Exercise the supported product pages against an isolated real deployment."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright


def verify_documents(page: Page, output: Path) -> None:
    expect(page.locator('[data-test="document-list"] [data-state="indexed"]').first).to_be_visible(
        timeout=90000
    )
    page.screenshot(path=str(output / "documents.png"), full_page=True)
    page.locator('[data-test="tab-search"]').click()
    page.locator('[data-test="retrieval-query"]').fill("Which planet has rings?")
    page.locator('[data-test="run-search"]').click()
    expect(page.locator('[data-test="search-results"]')).to_contain_text("Saturn", timeout=30000)
    page.screenshot(path=str(output / "retrieval.png"), full_page=True)
    page.locator('[data-test="tab-documents"]').click()
    page.locator('[data-test="document-list"] button').first.click()
    expect(page.locator('[data-test="chunk-list"] li')).not_to_have_count(0, timeout=15000)
    page.screenshot(path=str(output / "chunks.png"), full_page=True)
    print(f"Browser document/retrieval flow passed at {page.url}")


def run(
    base_url: str, output: Path, login_only: bool = False, existing_kb: str | None = None
) -> None:
    password = os.environ["CAIRN_SMOKE_PASSWORD"]
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.add_init_script("localStorage.setItem('cairn.locale', 'en-US')")
        page.goto(base_url)
        page.locator('[data-test="username"]').fill(os.environ.get("CAIRN_SMOKE_USERNAME", "admin"))
        page.locator('[data-test="password"]').fill(password)
        page.locator('[data-test="submit"]').click()
        page.wait_for_selector(
            '[data-test="forced-credential-dialog"], [data-test="nav-knowledge"]'
        )
        if page.locator('[data-test="forced-credential-dialog"]').is_visible():
            changed = os.environ["CAIRN_SMOKE_NEW_PASSWORD"]
            page.locator('[data-test="current-password"]').fill(password)
            page.locator('[data-test="new-password"]').fill(changed)
            page.locator('[data-test="confirm-password"]').fill(changed)
            page.locator('[data-test="forced-credential-dialog"] [data-test="submit"]').click()
        expect(page.locator('[data-test="nav-knowledge"]')).to_be_visible(timeout=15000)
        if login_only:
            page.screenshot(path=str(output / "login-complete.png"), full_page=True)
            print("Browser login and required credential flow passed")
            browser.close()
            return
        if existing_kb:
            page.goto(f"{base_url}/knowledge/{existing_kb}")
            verify_documents(page, output)
            browser.close()
            return
        page.locator('[data-test="nav-models"]').click()
        expect(page.locator('[data-test="page-title"]')).to_have_text("Models")
        page.screenshot(path=str(output / "models.png"), full_page=True)
        page.locator('[data-test="add-provider"]').click()
        sheet = page.locator('[data-test="provider-sheet"]')
        sheet.locator('[data-test="provider-name"]').fill("Local MiniLM")
        sheet.locator('[data-test="provider-url"]').fill("http://embedding:80")
        sheet.locator('[data-test="provider-submit"]').click()
        expect(sheet).not_to_be_visible(timeout=15000)
        page.locator('[data-test="add-model"]').click()
        model_sheet = page.locator('[data-test="model-sheet"]')
        provider_select = model_sheet.locator('select[data-test="model-provider"]')
        provider_select.select_option(label="Local MiniLM")
        model_sheet.locator('[data-test="model-submit"]').click()
        expect(model_sheet).not_to_be_visible(timeout=15000)
        page.locator('[data-test="test-model"]').first.click()
        test_result = page.locator('[data-test="model-test-result"]')
        expect(test_result).to_contain_text("Healthy", timeout=30000)
        page.locator('[data-test="nav-storage"]').click()
        for kind_index, name in ((0, "Persistent documents"), (1, "Knowledge vectors")):
            page.locator('[data-test="add-storage"]').click()
            storage_sheet = page.locator('[data-test="storage-sheet"]')
            storage_sheet.locator('[data-test="storage-name"]').fill(name)
            kinds = storage_sheet.locator('[data-test="storage-kind"] [role="radio"]')
            kinds.nth(kind_index).click()
            storage_sheet.locator('[data-test="storage-submit"]').click()
            expect(storage_sheet).not_to_be_visible(timeout=15000)
        page.screenshot(path=str(output / "storage.png"), full_page=True)
        page.locator('[data-test="nav-knowledge"]').click()
        page.locator('[data-test="create-kb"]').click()
        kb_sheet = page.locator('[data-test="create-kb-sheet"]')
        kb_sheet.locator('[data-test="kb-name"]').fill("Product acceptance")
        for selector, label in (
            ("kb-model", "MiniLM · 384d"),
            ("kb-objects", "Persistent documents"),
            ("kb-vectors", "Knowledge vectors"),
        ):
            kb_sheet.locator(f'select[data-test="{selector}"]').select_option(label=label)
        kb_sheet.locator('[data-test="kb-submit"]').click()
        expect(page.locator('[data-test="document-upload"]')).to_be_attached(timeout=15000)
        page.locator('[data-test="document-upload"]').set_input_files(
            {
                "name": "astronomy.md",
                "mimeType": "text/markdown",
                "buffer": b"# Astronomy\n\nSaturn has prominent rings of ice and rock.",
            }
        )
        verify_documents(page, output)
        print(f"Browser setup/upload/retrieval flow passed at {page.url}")
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:38880")
    parser.add_argument("--output", type=Path, default=Path("data/browser-product"))
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--existing-kb")
    args = parser.parse_args()
    run(args.base_url, args.output, args.login_only, args.existing_kb)
