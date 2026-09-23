"""The Registration Management screen authors with unowned revisions (decision 5800619183).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test, on a durable G1-A policy whose two authoring revisions are ``null`` and a
reviewed G1-B category. The screen loads the reviewed fields, saves the preparation with the
server's ``null`` revisions exactly, shows the server's ``AUTHORING_REVISIONS_UNOWNED`` reason, and
leaves FREEZE disabled with the server's reason. Nothing is frozen and no provider is reached.
"""

import contextlib
import json
import sys
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.register.preparation import AUTHORING_REVISIONS_UNOWNED
from tests.conftest import LOCAL
from tests.gate1_support import CATEGORY
from tests.integration.test_authoring_unowned_revisions import (  # noqa: F401 - fixture
    FROZEN_ROWS,
    counts,
    draft,
)
from tests.product_support import raw

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
REGISTER = f"{LOCAL}/#/register"
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


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


@contextlib.contextmanager
def _page(
    browser: Browser, client: TestClient, calls: list[tuple[str, str, int]]
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        if parts.path.startswith("/api/"):
            calls.append((request.method, parts.path, response.status_code))
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(REGISTER)
        page.wait_for_selector(".register-canary", timeout=15_000)
        yield page
    finally:
        page.close()


def test_the_screen_authors_with_null_revisions_and_leaves_freeze_disabled(
    browser: Browser,
    api: TestClient,
    config: AppConfig,
    draft: tuple[str, str],  # noqa: F811 - the imported fixture
) -> None:
    draft_id, _ = draft
    calls: list[tuple[str, str, int]] = []
    metadata_path = f"/api/v1/register/drafts/{draft_id}/authoring-metadata/{CATEGORY}"
    with _page(browser, api, calls) as page:
        unit = page.locator(f".register-unit[data-draft='{draft_id}']")
        assert unit.count() == 1
        # Choosing the category loads the reviewed fields from the server: no refusal, no 500.
        unit.locator("input[name='category_id']").fill(CATEGORY)
        unit.locator("input[name='category_id']").press("Tab")
        unit.locator("input[name='attribute.brand']").wait_for(timeout=10_000)
        assert ("GET", metadata_path, 200) in calls
        unit.locator("input[name='name']").fill("합성 상품")
        unit.locator("input[name='attribute.brand']").fill("합성 브랜드")
        unit.locator("input[name='notice.manufacturer']").fill("합성 제조사")
        unit.locator("input[data-detail-reference='notice'][data-field-key='origin']").check()
        unit.locator("textarea[name='detail_body']").fill("상세 본문")
        unit.locator("button[data-action='SAVE_PREPARATION']").click()
        page.wait_for_function(
            "() => document.querySelector('.register-authoring')?.dataset.preparation !== ''"
        )
        assert ("POST", "/api/v1/register/preparations", 200) in calls
        # The server's nulls were sent and stored exactly: no stand-in revision.
        with contextlib.closing(raw(config)) as connection:
            category_json, detail_json = connection.execute(
                "SELECT category_json, detail_json FROM registration_preparation_revisions"
            ).fetchone()
        assert json.loads(category_json)["mapping_revision"] is None
        assert json.loads(detail_json)["composition_revision"] is None
        # The server's reason is shown and FREEZE stays disabled with the server's own reason.
        unit = page.locator(f".register-unit[data-draft='{draft_id}']")
        preflight = unit.locator(".register-preflight")
        assert preflight.get_attribute("data-preflight") != "READY"
        assert preflight.locator(f"[data-reason='{AUTHORING_REVISIONS_UNOWNED}']").count() == 1
        freeze = unit.locator("button[data-action='FREEZE']")
        assert freeze.is_disabled()
        cell = unit.locator(".register-action", has=page.locator("button[data-action='FREEZE']"))
        assert "Preflight가 READY가 아니어서" in cell.inner_text()
        # A reload rebuilds the same state from durable rows.
        page.reload()
        page.wait_for_selector(".register-canary", timeout=15_000)
        unit = page.locator(f".register-unit[data-draft='{draft_id}']")
        assert unit.locator(f"[data-reason='{AUTHORING_REVISIONS_UNOWNED}']").count() == 1
        assert unit.locator("button[data-action='FREEZE']").is_disabled()
    assert not any(path.endswith("/freeze") for _, path, _ in calls)
    assert not any(status >= 500 for _, _, status in calls)
    assert counts(config) == dict.fromkeys(FROZEN_ROWS, 0)
