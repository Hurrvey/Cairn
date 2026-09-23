"""Drive the redesigned Cairn web UI end to end against a live local stack.

Covers sign-in, forced credential change, model/storage setup, knowledge base
creation, upload -> indexed, document detail with chunks and in-place edit,
search with citations, API keys, users, audit, settings, MCP page, theme,
keyboard (command palette), mobile layout and offline handling. Screenshots
land in the output directory for visual review.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ADMIN_INITIAL = "Initial-Admin-Password-2026"
ADMIN_NEW = "Correct-Horse-Battery-Staple-9"
TEI_URL = "http://127.0.0.1:39223"


def shot(page: Page, output: Path, name: str, full: bool = False) -> None:
    page.screenshot(path=str(output / f"{name}.png"), full_page=full)
    print(f"  shot {name}.png", flush=True)


def check(label: str) -> None:
    print(f"PASS {label}", flush=True)


def login(page: Page, base: str, password: str) -> None:
    page.goto(f"{base}/login")
    page.locator('[data-test="username"]').fill("admin")
    page.locator('[data-test="password"]').fill(password)
    page.locator('[data-test="submit"]').click()


def run(base: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        # Prefer the bundled Chromium; fall back to the installed Google Chrome
        # so the check runs on a machine without Playwright browser downloads.
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True, channel="chrome")
        context = browser.new_context(viewport={"width": 1440, "height": 960}, locale="en-US")
        context.add_init_script(
            "localStorage.setItem('cairn.locale','en-US');"
            "localStorage.setItem('cairn.theme','light')"
        )
        page = context.new_page()
        failed_responses: list[str] = []
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text)
                if message.type == "error" and "Failed to load resource" not in message.text
                else None
            ),
        )
        page.on("pageerror", lambda error: console_errors.append(str(error)))
        page.on(
            "response",
            lambda response: (
                failed_responses.append(f"{response.status} {response.url}")
                if response.status >= 400
                else None
            ),
        )

        # --- sign in and forced credential change -----------------------------
        login(page, base, ADMIN_INITIAL)
        shot(page, output, "01-login-forced")
        dialog = page.locator('[data-test="forced-credential-dialog"]')
        expect(dialog).to_be_visible(timeout=15000)
        expect(page.locator('[data-test="dialog-close"]')).to_have_count(0)
        page.keyboard.press("Escape")
        expect(dialog).to_be_visible()
        dialog.locator('[data-test="current-password"]').fill(ADMIN_INITIAL)
        dialog.locator('[data-test="new-password"]').fill(ADMIN_NEW)
        dialog.locator('[data-test="confirm-password"]').fill(ADMIN_NEW)
        expect(dialog.locator('[data-rule="length"]')).to_have_class(re.compile(r"(^|\s)ok(\s|$)"))
        dialog.locator('[data-test="submit"]').click()
        expect(page.locator('[data-test="nav-knowledge"]')).to_be_visible(timeout=15000)
        check("forced credential change completes and lands on the overview")
        shot(page, output, "02-overview-setup")
        expect(page.locator('[data-test="setup-checklist"]')).to_be_visible()

        # --- models ----------------------------------------------------------
        page.locator('[data-test="nav-models"]').click()
        expect(page.locator('[data-test="page-title"]')).to_have_text("Models")
        page.locator('[data-test="add-provider"]').click()
        sheet = page.locator('[data-test="provider-sheet"]')
        expect(sheet).to_be_visible()
        sheet.locator('[data-test="provider-name"]').fill("Local TEI")
        sheet.locator('[data-test="provider-url"]').fill(TEI_URL)
        shot(page, output, "03-provider-sheet")
        sheet.locator('[data-test="provider-submit"]').click()
        expect(page.locator('[data-test="provider-list"]')).to_contain_text(
            "Local TEI", timeout=15000
        )
        page.locator('[data-test="add-model"]').click()
        model_sheet = page.locator('[data-test="model-sheet"]')
        expect(model_sheet).to_be_visible()
        model_sheet.locator('[data-test="model-submit"]').click()
        expect(page.locator('[data-test="model-list"]')).to_contain_text("MiniLM", timeout=15000)
        page.locator('[data-test="test-model"]').first.click()
        expect(page.locator('[data-test="model-test-result"]')).to_contain_text(
            "Healthy", timeout=30000
        )
        check("provider and model registered; live model test healthy")
        shot(page, output, "04-models")

        # --- storage -----------------------------------------------------------
        page.locator('[data-test="nav-storage"]').click()
        for name, kind_index in (("Documents", 0), ("Vectors", 1)):
            page.locator('[data-test="add-storage"]').click()
            storage_sheet = page.locator('[data-test="storage-sheet"]')
            expect(storage_sheet).to_be_visible()
            storage_sheet.locator('[data-test="storage-name"]').fill(name)
            storage_sheet.locator('[data-test="storage-kind"] [role="radio"]').nth(
                kind_index
            ).click()
            storage_sheet.locator('[data-test="storage-submit"]').click()
            expect(page.locator('[data-test="binding-list"]')).to_contain_text(name, timeout=15000)
        expect(page.locator('[data-test="binding-test-result"]').first).to_contain_text(
            "healthy", timeout=20000
        )
        check("object and vector bindings created and tested")
        shot(page, output, "05-storage")

        # --- knowledge base -------------------------------------------------------
        page.locator('[data-test="nav-knowledge"]').click()
        expect(page.locator('[data-test="empty-state"]')).to_be_visible()
        shot(page, output, "06-knowledge-empty")
        page.locator('[data-test="create-kb"]').click()
        kb_sheet = page.locator('[data-test="create-kb-sheet"]')
        expect(kb_sheet).to_be_visible()
        kb_sheet.locator('[data-test="kb-name"]').fill("Planet handbook")
        shot(page, output, "07-create-kb")
        kb_sheet.locator('[data-test="kb-submit"]').click()
        expect(page.locator('[data-test="page-title"]')).to_have_text(
            "Planet handbook", timeout=15000
        )
        kb_url = page.url
        check("knowledge base created and opened")
        shot(page, output, "08-kb-empty")

        # --- upload and index -----------------------------------------------------
        page.locator('[data-test="document-upload"]').set_input_files(
            [
                {
                    "name": "saturn.md",
                    "mimeType": "text/markdown",
                    "buffer": (
                        b"# Saturn\n\nSaturn is the sixth planet and has prominent rings "
                        b"made of ice and rock.\n\n## Moons\n\nTitan is its largest moon "
                        b"and has a thick atmosphere.\n"
                    ),
                },
                {
                    "name": "mars.txt",
                    "mimeType": "text/plain",
                    "buffer": (
                        b"Mars is the fourth planet. It is called the red planet because of "
                        b"iron oxide on its surface. Olympus Mons is the tallest volcano."
                    ),
                },
            ]
        )
        expect(page.locator('[data-test="upload-queue"]')).to_contain_text("Queued", timeout=15000)
        row = page.locator('[data-test="document-list"] li').first
        expect(row).to_be_visible(timeout=15000)
        shot(page, output, "09-documents-processing")
        expect(page.locator('[data-test="document-list"] [data-state="indexed"]')).to_have_count(
            2, timeout=120000
        )
        check("two documents uploaded and indexed through the real pipeline")
        shot(page, output, "10-documents-indexed")

        # --- document detail + chunk edit ---------------------------------------
        page.locator('[data-test="document-list"] button', has_text="saturn.md").click()
        detail = page.locator('[data-test="document-detail"]')
        expect(detail).to_be_visible()
        expect(detail.locator('[data-test="chunk-list"]')).to_be_visible(timeout=15000)
        assert "doc=" in page.url
        shot(page, output, "11-document-detail")
        detail.locator('[data-test="edit-chunk"]').first.click()
        editor = detail.locator('[data-test="chunk-editor"]')
        editor.fill(
            editor.input_value() + "\nSaturn's rings are visible from Earth with a small telescope."
        )
        detail.locator('[data-test="save-chunk"]').click()
        expect(page.locator('[data-test="toast-success"]')).to_be_visible(timeout=15000)
        expect(detail.locator('[data-test="chunk-list"]')).to_contain_text("Edited")
        expect(page.locator('[data-test="toast-success"]')).to_have_count(0, timeout=8000)
        check("chunk edited in place; toast dismissed itself")
        shot(page, output, "12-chunk-edited")

        # --- search -------------------------------------------------------------------
        page.locator('[data-test="tab-search"]').click()
        query = page.locator('[data-test="retrieval-query"]')
        query.fill("Which planet has rings?")
        page.locator('[data-test="run-search"]').click()
        expect(page.locator('[data-test="search-results"]')).to_contain_text(
            "Saturn", timeout=30000
        )
        shot(page, output, "13-search-results")
        page.locator('[data-test="toggle-advanced"]').click()
        expect(page.locator('[data-test="advanced-options"]')).to_be_visible()
        page.locator('[data-test="expand-parent"] [role="switch"]').click()
        page.locator('[data-test="run-search"]').click()
        expect(page.locator('[data-test="search-results"]')).to_contain_text(
            "Saturn", timeout=30000
        )
        check("search returns cited passages; parent expansion toggle works")
        shot(page, output, "14-search-advanced")
        page.locator('[data-test="search-results"] button').first.click()
        expect(page.locator('[data-test="document-detail"]')).to_be_visible()
        check("search hit links back to its document")

        # --- versions + settings ---------------------------------------------------
        page.locator('[data-test="tab-versions"]').click()
        expect(page.locator('[data-test="version-list"]')).to_contain_text("v1", timeout=15000)
        shot(page, output, "15-versions")
        page.locator('[data-test="tab-settings"]').click()
        expect(page.locator('[data-test="kb-settings"]')).to_be_visible()
        page.locator('[data-test="setting-expand-parent"] [role="switch"]').click()
        page.locator('[data-test="save-retrieval"]').click()
        expect(page.locator('[data-test="toast-success"]')).to_be_visible(timeout=15000)
        check("knowledge base retrieval defaults saved")
        shot(page, output, "16-kb-settings", full=True)

        # --- back navigation and list ----------------------------------------------
        page.locator('[data-test="back-link"]').click()
        expect(page.locator('[data-test="kb-table"]')).to_contain_text("Planet handbook")
        expect(page.locator('[data-test="kb-table"]')).to_contain_text("2")
        shot(page, output, "17-knowledge-list")
        page.locator('[data-test="kb-filter"]').fill("zzz")
        expect(page.locator('[data-test="empty-state"]')).to_be_visible()
        page.locator('[data-test="kb-filter"]').fill("")

        # --- overview after setup ----------------------------------------------------
        page.locator('[data-test="nav-dashboard"]').click()
        expect(page.locator('[data-test="setup-checklist"]')).to_have_count(0)
        expect(page.locator('[data-test="kb-list"]')).to_contain_text("Planet handbook")
        expect(page.locator('[data-test="all-clear"]')).to_be_visible(timeout=15000)
        check("overview shows the knowledge base and no attention items")
        shot(page, output, "18-overview-ready")

        # --- API keys ----------------------------------------------------------------
        page.locator('[data-test="nav-api-keys"]').click()
        page.locator('[data-test="new-key"]').click()
        key_dialog = page.locator('[data-test="create-key-dialog"]')
        key_dialog.locator('[data-test="key-name"]').fill("support-bot")
        key_dialog.locator('[data-test="key-kbs"] [role="checkbox"]').first.click()
        shot(page, output, "19-create-key")
        key_dialog.locator('[data-test="submit"]').click()
        secret = key_dialog.locator('[data-test="secret-value"]')
        expect(secret).to_be_visible(timeout=15000)
        api_key = secret.inner_text().strip()
        expect(key_dialog.locator('[data-test="done"]')).to_be_disabled()
        key_dialog.locator('[data-test="secret-ack"] [role="checkbox"]').click()
        key_dialog.locator('[data-test="done"]').click()
        expect(page.locator('[data-test="keys-table"]')).to_contain_text("support-bot")
        check("API key created behind an acknowledgement")
        shot(page, output, "20-api-keys")

        # Use the key exactly as an agent would.
        response = page.request.post(
            f"{base}/v1/retrieval/query",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "targets": [{"knowledge_base_id": kb_url.rsplit("/", 1)[-1].split("?")[0]}],
                    "query": "red planet",
                    "search_mode": "fulltext",
                    "top_k": 3,
                }
            ),
        )
        assert response.ok, response.text()
        assert any("Mars" in hit["content"] for hit in response.json()["results"]), response.text()
        check("agent-style bearer query through the public retrieval API")

        # --- users + grants -------------------------------------------------------------
        page.locator('[data-test="nav-users"]').click()
        page.locator('[data-test="new-user"]').click()
        user_dialog = page.locator('[data-test="create-user-dialog"]')
        user_dialog.locator('[data-test="username"]').fill("wei.chen")
        user_dialog.locator('[data-test="submit"]').click()
        expect(user_dialog.locator('[data-test="secret-value"]')).to_be_visible(timeout=15000)
        user_dialog.locator('[data-test="secret-ack"] [role="checkbox"]').click()
        user_dialog.locator('[data-test="done"]').click()
        expect(page.locator('[data-test="users-table"]')).to_contain_text("wei.chen")
        shot(page, output, "21-users")
        page.locator('[data-test="users-table"] li', has_text="wei.chen").locator(
            '[data-test="user-menu"]'
        ).click()
        page.locator('[data-test="edit-grants"]').click()
        grants = page.locator('[data-test="grants-drawer"]')
        expect(grants).to_be_visible()
        grants.locator('select[data-test="resource-id"]').select_option(index=1)
        grants.locator('[data-test="permissions"] [role="checkbox"]').first.click()
        grants.locator('[data-test="add-grant"]').click()
        expect(grants.locator('[data-test="grants-table"]')).to_contain_text(
            "Planet handbook", timeout=15000
        )
        check("grant added for a user on the knowledge base")
        shot(page, output, "22-grants")
        grants.locator('[data-test="sheet-close"]').click()

        # --- audit, settings, MCP ---------------------------------------------------------
        page.locator('[data-test="nav-audit"]').click()
        expect(page.locator('[data-test="audit-table"]')).to_be_visible(timeout=15000)
        page.locator('[data-test="toggle-detail"]').first.click()
        expect(page.locator('[data-test="audit-detail"]')).to_be_visible()
        shot(page, output, "23-audit")
        page.locator('[data-test="nav-settings"]').click()
        expect(page.locator('[data-test="access-mode"]')).to_be_visible(timeout=15000)
        expect(page.locator('[data-test="queues-table"]')).to_be_visible()
        shot(page, output, "24-settings", full=True)
        page.locator('[data-test="nav-mcp-service"]').click()
        expect(page.locator('[data-test="unmanaged-notice"]')).to_be_visible(timeout=15000)
        expect(page.locator('[data-test="action-start"]')).to_be_disabled()
        shot(page, output, "25-mcp")
        check("audit, settings and MCP pages render live data")

        # --- command palette and keyboard --------------------------------------------------
        page.keyboard.press("Control+k")
        palette = page.locator('[data-test="command-palette"]')
        expect(palette).to_be_visible()
        page.locator('[data-test="palette-input"]').fill("planet")
        expect(page.locator('[data-test^="palette-kb:"]')).to_be_visible(timeout=10000)
        shot(page, output, "26-command-palette")
        page.keyboard.press("Enter")
        expect(page.locator('[data-test="page-title"]')).to_have_text(
            "Planet handbook", timeout=15000
        )
        check("command palette jumps to a knowledge base from the keyboard")

        # --- dark theme and Chinese ---------------------------------------------------------
        page.locator('[data-test="toggle-theme"]').click()
        expect(page.locator("html")).to_have_class(re.compile(r"(^|\s)dark(\s|$)"))
        shot(page, output, "27-dark-kb")
        page.locator('[data-test="nav-dashboard"]').click()
        shot(page, output, "28-dark-overview")
        page.locator('[data-test="toggle-locale"]').click()
        expect(page.locator('[data-test="topbar-title"]')).to_have_text("总览")
        shot(page, output, "29-zh-overview")
        page.locator('[data-test="toggle-locale"]').click()
        page.locator('[data-test="toggle-theme"]').click()
        check("dark theme and zh-CN switch in place")

        # --- offline handling ---------------------------------------------------------------
        context.set_offline(True)
        page.locator('[data-test="nav-knowledge"]').click()
        expect(page.locator('[data-test="offline-banner"]')).to_be_visible(timeout=15000)
        expect(page.locator('[data-test="error-state"]')).to_be_visible(timeout=15000)
        shot(page, output, "30-offline")
        context.set_offline(False)
        page.locator('[data-test="retry"]').first.click()
        expect(page.locator('[data-test="kb-table"]')).to_contain_text(
            "Planet handbook", timeout=15000
        )
        expect(page.locator('[data-test="offline-banner"]')).to_have_count(0)
        check("offline banner and retry recover")

        # --- mobile layout -------------------------------------------------------------------
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(kb_url)
        expect(page.locator('[data-test="sidebar"]')).to_be_hidden()
        page.locator('[data-test="open-nav"]').click()
        expect(page.locator('[data-test="mobile-nav"]')).to_be_visible()
        shot(page, output, "31-mobile-nav")
        page.keyboard.press("Escape")
        page.locator('[data-test="document-list"] button').first.click()
        expect(page.locator('[data-test="document-detail"]')).to_be_visible()
        shot(page, output, "32-mobile-document", full=True)
        check("mobile layout: drawer navigation and stacked master-detail")

        # --- reduced motion --------------------------------------------------------------------
        page.set_viewport_size({"width": 1440, "height": 960})
        page.emulate_media(reduced_motion="reduce")
        page.goto(f"{base}/")
        expect(page.locator('[data-test="kb-list"]')).to_be_visible(timeout=15000)
        check("renders under prefers-reduced-motion")

        # --- sign out --------------------------------------------------------------
        page.locator('[data-test="user-menu"]').click()
        page.locator('[data-test="sign-out"]').click()
        expect(page.locator('[data-test="login-panel"]')).to_be_visible(timeout=15000)
        shot(page, output, "33-login")
        check("sign out returns to login")
        page.locator('[data-test="username"]').fill("admin")
        page.locator('[data-test="password"]').fill(ADMIN_NEW)
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement?.dataset.test") == "submit"
        page.keyboard.press("Enter")
        expect(page.locator('[data-test="nav-knowledge"]')).to_be_visible(timeout=15000)
        check("keyboard-only sign in")

        browser.close()

    # The only failed responses a clean run produces: the unauthenticated
    # session probe on the login page (401 on /v1/me).
    unexpected = [
        entry
        for entry in failed_responses
        if not entry.startswith("401 ") or not entry.endswith("/v1/me")
    ]
    if console_errors or unexpected:
        print("Console errors / unexpected responses:", file=sys.stderr)
        for error in [*console_errors, *unexpected]:
            print("  " + error, file=sys.stderr)
        raise SystemExit(1)
    print("ALL BROWSER CHECKS PASSED", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--output", default="data/ui-20260922/shots")
    args = parser.parse_args()
    started = time.monotonic()
    run(args.base_url, Path(args.output))
    print(f"done in {time.monotonic() - started:.0f}s")
