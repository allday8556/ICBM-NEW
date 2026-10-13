"""The real Settings page saves Coupang credentials once and never displays them again."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.platform.core.egress import EGRESS
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser

pytestmark = pytest.mark.integration

API_TAB = f"{LOCAL}/#/settings?tab=coupang&sub=api"
FORWARDED = ("x-icbm-client", "content-type", "accept")
ENDPOINT = "/api/v1/connect/marketplaces/coupang/credentials"
VENDOR_ID = "A00UIFIXTURE"
ACCESS_KEY = "ui-fixture-access-key"
SECRET_KEY = "ui-fixture-secret-key"


@dataclass
class Traffic:
    requests: list[tuple[str, str, object, bool]] = field(default_factory=list)


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
def _page(browser: Browser, client: TestClient, traffic: Traffic) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {key: value for key, value in request.headers.items() if key.lower() in FORWARDED}
        body = request.post_data_buffer
        traffic.requests.append(
            (
                request.method,
                parts.path,
                json.loads(body) if body else None,
                "x-icbm-client" in {key.lower() for key in request.headers},
            )
        )
        response = client.request(request.method, path, content=body, headers=headers)
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(API_TAB)
        page.wait_for_selector(".coupang-credentials", timeout=15_000)
        yield page
    finally:
        page.close()


def test_coupang_settings_saves_once_clears_inputs_and_makes_no_egress(
    browser: Browser, client: TestClient
) -> None:
    traffic = Traffic()
    before = EGRESS.snapshot()
    with _page(browser, client, traffic) as page:
        assert page.locator(".coupang-credentials").inner_text().count("미설정") == 1
        page.fill("#coupang-vendor-id", VENDOR_ID)
        page.fill("#coupang-access-key", ACCESS_KEY)
        page.fill("#coupang-secret-key", SECRET_KEY)
        page.click("[data-action='save-coupang-credentials']")
        page.wait_for_selector(".coupang-credentials .chip.good", timeout=15_000)
        status = page.locator(".coupang-credentials .chip.good").inner_text()
        values = [
            page.input_value("#coupang-vendor-id"),
            page.input_value("#coupang-access-key"),
            page.input_value("#coupang-secret-key"),
        ]
        html = page.content()
        stored = page.evaluate(
            "() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])"
        )
    assert status == "저장됨 · 자격증명 세대 1"
    assert values == ["", "", ""]
    assert ACCESS_KEY not in html and SECRET_KEY not in html
    assert ACCESS_KEY not in stored and SECRET_KEY not in stored
    writes = [request for request in traffic.requests if request[0] != "GET"]
    assert writes == [
        (
            "PUT",
            ENDPOINT,
            {"vendor_id": VENDOR_ID, "access_key": ACCESS_KEY, "secret_key": SECRET_KEY},
            True,
        )
    ]
    after = EGRESS.snapshot()
    assert (after["external_attempts"], after["granted_events"]) == (
        before["external_attempts"],
        before["granted_events"],
    )
