"""Exercise managed MCP controls in an explicitly configured disposable deployment."""

import argparse
import os
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright


def verify(base: str, output: Path, first_port: int) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        page.add_init_script("localStorage.setItem('cairn.locale','en-US')")
        page.goto(base)
        page.locator('[data-test="username"]').fill("admin")
        page.locator('[data-test="password"]').fill(os.environ["CAIRN_SMOKE_PASSWORD"])
        page.locator('[data-test="submit"]').click()
        page.wait_for_selector(
            '[data-test="forced-credential-dialog"], [data-test="nav-knowledge"]'
        )
        if page.locator('[data-test="forced-credential-dialog"]').is_visible():
            for field, value in [
                ("current-password", os.environ["CAIRN_SMOKE_PASSWORD"]),
                ("new-password", os.environ["CAIRN_SMOKE_NEW_PASSWORD"]),
                ("confirm-password", os.environ["CAIRN_SMOKE_NEW_PASSWORD"]),
            ]:
                page.locator(f'[data-test="{field}"]').fill(value)
            page.locator('[data-test="forced-credential-dialog"] [data-test="submit"]').click()
        expect(page.locator('[data-test="nav-knowledge"]')).to_be_visible(timeout=15000)
        page.goto(base + "/mcp-service/nav")
        expect(page.locator('[data-test="actual-state"]')).to_contain_text("Running", timeout=20000)
        page.screenshot(path=str(output / "running.png"), full_page=True)
        page.locator('[data-test="action-stop"]').click()
        expect(page.locator('[data-test="actual-state"]')).to_contain_text("Stopped", timeout=20000)
        assert httpx.post(base + "/mcp", json={}).status_code == 503
        try:
            httpx.post(f"http://127.0.0.1:{first_port}/mcp", json={}, timeout=2)
        except httpx.HTTPError:
            pass
        else:
            raise AssertionError("Stopped direct listener still accepts requests")
        page.locator('select[data-test="config-port"]').select_option(str(first_port + 1))
        page.locator('[data-test="save-config"]').click()
        expect(page.locator('[data-test="action-start"]')).to_be_enabled(timeout=15000)
        page.locator('[data-test="action-start"]').click()
        expect(page.locator('[data-test="actual-state"]')).to_contain_text("Running", timeout=20000)
        expect(page.locator('[data-test="direct-endpoint"]')).to_contain_text(str(first_port + 1))
        assert httpx.post(f"http://127.0.0.1:{first_port + 1}/mcp", json={}).status_code == 401
        page.locator('[data-test="action-restart"]').click()
        expect(page.locator('[data-test="action-restart"]')).to_be_enabled(timeout=20000)
        expect(page.locator('[data-test="actual-state"]')).to_contain_text("Running")
        expect(page.locator('[data-test="log-detail"]').first).to_be_visible(timeout=10000)
        page.screenshot(path=str(output / "restarted.png"), full_page=True)
        with page.expect_download() as download:
            page.locator('[data-test="download-logs"]').click()
        download.value.save_as(output / "mcp-logs.jsonl")
        browser.close()
        print("PASS browser MCP stop, port change, start, restart, logs and download")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:38882")
    parser.add_argument("--first-port", type=int, default=38081)
    parser.add_argument(
        "--output", type=Path, default=Path("data/acceptance-mcpui-20260917/browser")
    )
    args = parser.parse_args()
    verify(args.base_url, args.output, args.first_port)
