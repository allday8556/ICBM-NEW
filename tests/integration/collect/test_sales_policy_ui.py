"""The supplier card's 「판매정책」 in the real UI (ADR-0039 §4).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. Proven here:
- each supplier card shows the two switches with the server's current values, who changed them and
  when, and the one line on what turning one off means;
- a save is one PUT through the policy's contract, carrying the client header and the revision it
  was read at, and the card then shows what the server holds — after a reload too;
- a save the server refuses (the policy moved elsewhere) shows the server's reason, writes nothing
  more, and the card reads the server's policy again.

No supplier or marketplace is reached: the policy is local configuration.
"""

import contextlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.main import create_app
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.product_support import raw

pytestmark = pytest.mark.integration

SUPPLIERS = f"{LOCAL}/#/collect?view=suppliers"
SUPPLIER = "kmretail"
FORWARDED = ("x-icbm-client", "content-type", "accept")
SECTION = f"[data-role='sales-policy'][data-supplier='{SUPPLIER}']"
NOTE = "끄면 공급사 정책을 경고로만 표시하고, 그에 따른 판매 위험은 판매자 책임입니다"


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            launched = launch_browser(playwright)
        except PlaywrightError as exc:
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({type(exc).__name__})")
        try:
            yield launched
        finally:
            launched.close()


@contextmanager
def _served(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@contextmanager
def _page(
    browser: Browser, client: TestClient, writes: list[tuple[str, str, dict[str, str], bytes]]
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        if request.method != "GET":
            writes.append((request.method, parts.path, headers, request.post_data_buffer or b""))
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(SUPPLIERS)
        page.wait_for_selector(f"{SECTION}[data-state='ready']", timeout=15_000)
        yield page
    finally:
        page.close()


def _revisions(config: AppConfig) -> list[tuple[int, int, int]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(
            "SELECT revision_no, apply_channel_restrictions, apply_minimum_price"
            " FROM supplier_sales_policy_revisions ORDER BY revision_no"
        ).fetchall()


def _switch(page: Page, key: str) -> str | None:
    return page.locator(f"{SECTION} [data-switch='{key}']").get_attribute("aria-checked")


def test_the_card_shows_the_switches_and_saves_them(browser: Browser, config: AppConfig) -> None:
    writes: list[tuple[str, str, dict[str, str], bytes]] = []
    with _served(config) as client, _page(browser, client, writes) as page:
        section = page.locator(SECTION)
        assert "판매정책" in section.inner_text()
        for key, label in (
            ("apply_channel_restrictions", "판매채널 제한 적용"),
            ("apply_minimum_price", "최저판매가 적용"),
        ):
            control = section.locator(f"[data-switch='{key}']")
            assert control.get_attribute("role") == "switch"
            assert control.get_attribute("aria-label") == label
            assert control.get_attribute("aria-checked") == "true"
        assert section.locator("[data-role='sales-policy-note']").inner_text() == NOTE
        assert "기본값" in section.locator("[data-role='sales-policy-changed']").inner_text()
        save = section.locator("[data-action='save-sales-policy']")
        assert save.is_disabled()
        # Every configured supplier's card has its own section.
        cards = page.locator(".supplier-card[data-supplier]")
        assert page.locator("[data-role='sales-policy']").count() == cards.count() >= 1

        section.locator("[data-switch='apply_channel_restrictions']").click()
        assert _switch(page, "apply_channel_restrictions") == "false"
        assert save.is_enabled()
        save.click()
        page.wait_for_selector(f"{SECTION}[data-revision='1']", timeout=10_000)
        assert _revisions(config) == [(1, 0, 1)]
        assert len(writes) == 1
        method, path, headers, body = writes[0]
        assert (method, path) == ("PUT", f"/api/v1/suppliers/{SUPPLIER}/sales-policy")
        assert headers.get("x-icbm-client")
        assert json.loads(body) == {
            "expected_revision_no": None,
            "apply_channel_restrictions": False,
            "apply_minimum_price": True,
        }
        changed = section.locator("[data-role='sales-policy-changed']").inner_text()
        assert changed.endswith("에 변경")
        assert section.get_attribute("data-changed-by") == config.operator_actor

        page.reload()
        page.wait_for_selector(f"{SECTION}[data-state='ready'][data-revision='1']")
        assert _switch(page, "apply_channel_restrictions") == "false"
        assert _switch(page, "apply_minimum_price") == "true"


def test_a_refused_save_shows_the_reason_and_reads_the_policy_again(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str, dict[str, str], bytes]] = []
    with _served(config) as client, _page(browser, client, writes) as page:
        # Another save lands after the card was read.
        moved = client.put(
            f"/api/v1/suppliers/{SUPPLIER}/sales-policy",
            json={
                "expected_revision_no": None,
                "apply_channel_restrictions": True,
                "apply_minimum_price": False,
            },
            headers={"X-ICBM-Client": "pytest"},
        )
        assert moved.status_code == 200, moved.text
        page.locator(f"{SECTION} [data-switch='apply_channel_restrictions']").click()
        page.locator(f"{SECTION} [data-action='save-sales-policy']").click()
        toast = page.locator(".toast", has_text="다른 곳에서 판매정책이 바뀌었습니다")
        toast.first.wait_for(timeout=10_000)
        page.wait_for_selector(f"{SECTION}[data-revision='1']", timeout=10_000)
        assert _revisions(config) == [(1, 1, 0)]
        # The card shows what the server holds, not what was refused.
        assert _switch(page, "apply_channel_restrictions") == "true"
        assert _switch(page, "apply_minimum_price") == "false"
