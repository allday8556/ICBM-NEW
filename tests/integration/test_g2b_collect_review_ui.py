"""The COLLECT screen's review path in the real UI (Gate 2 G2-B, ADR-0016 §5, §7, §8).

The page runs in the installed browser, and every request is answered in-process by the
application under test, collecting from a scripted shop whose stock is unclear. The run's focus
shows the server's ReviewItems for its source product and the server's coverage verdict. A
resolution sends the scope and generation it was shown; while the source still states the
condition, the server keeps the item open and the page says so. The page counts nothing.
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from tests.collect_submit_support import served
from tests.conftest import LOCAL
from tests.integration.test_g2b_collect_review import UnclearShop, collect_unclear

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
FORWARDED = ("x-icbm-client", "content-type", "accept")


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({type(exc).__name__})")
        try:
            yield launched
        finally:
            launched.close()


@contextmanager
def _page(browser: Browser, client: TestClient, writes: list[tuple[str, str]]) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        if request.method != "GET":
            writes.append((request.method, parts.path))
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        yield page
    finally:
        page.close()


def test_the_run_focus_shows_its_review_items_and_resolves_one(
    browser: Browser, config: AppConfig
) -> None:
    with served(config, UnclearShop()) as api:
        run = collect_unclear(api, "4242")
        writes: list[tuple[str, str]] = []
        with _page(browser, api, writes) as page:
            page.goto(f"{LOCAL}/#/collect?view=jobs&run={run['collection_run_id']}")
            block = page.locator("[data-role='review-items']")
            block.wait_for(timeout=15_000)
            # The server's coverage verdict, never a count: the startup pass made it current.
            assert block.get_attribute("data-coverage") == "CURRENT"
            item = block.locator("[data-review-item][data-state='OPEN']", has_text="field:stock")
            assert item.count() == 1
            item_id = item.get_attribute("data-review-item")
            assert "SOURCE_STOCK_REVIEW_REQUIRED" in item.inner_text()
            assert writes == []  # rendering reads only
            item.locator("[data-role='review-disposition']").select_option("FOLLOW_UP_REQUIRED")
            item.locator("[data-role='review-note']").fill("재수집 예정")
            item.locator("[data-action='resolve-review']").click()
            # The block is redrawn from the server with its answer: the source still states the
            # condition, so the item is still open, at the same generation.
            page.wait_for_selector(
                "[data-role='review-items'] [data-role='review-outcome']"
                "[data-outcome='CONDITION_PERSISTS']",
                timeout=15_000,
            )
            assert writes == [("POST", f"/api/v1/review/items/{item_id}/resolve")]
            again = page.locator(f"[data-review-item='{item_id}']")
            assert again.get_attribute("data-state") == "OPEN"
            assert again.get_attribute("data-generation") == "1"
            # A reload rebuilds the same state from durable rows.
            page.reload()
            reloaded = page.locator(f"[data-review-item='{item_id}']")
            reloaded.wait_for(timeout=15_000)
            assert reloaded.get_attribute("data-state") == "OPEN"
