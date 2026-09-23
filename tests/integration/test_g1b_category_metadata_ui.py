"""The category-metadata review surface in the real UI (Gate 1 G1-B, ADR-0015 §3).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. Proven here:
- the operator records an AI-suggested revision as unreviewed, then reviews it by recording an
  operator-confirmed revision from evidence; the screen shows the server's current revision, review
  provenance and history, and the same state comes back after a reload and after a restart;
- a save the server refuses (an AI suggestion marked reviewed) shows the server's reason and
  writes nothing.

No provider is reached: category metadata is operator-recorded local truth.
"""

import contextlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from tests.conftest import LOCAL
from tests.product_support import raw

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
MARKETPLACE = "smartstore"
TAB = f"{LOCAL}/#/settings?tab=smartstore&sub=product"
FORWARDED = ("x-icbm-client", "content-type", "accept")
TABLES = (
    "registration_category_metadata",
    "registration_category_metadata_revisions",
    "registration_category_metadata_current",
)
PANEL = f".category-metadata[data-marketplace='{MARKETPLACE}']"
KEY = "taxonomy-ui-1/50000803"


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
def _served(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


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
        page.goto(TAB)
        page.wait_for_selector(
            f"{PANEL} button[data-action='new-category-metadata']", timeout=15_000
        )
        yield page
    finally:
        page.close()


def _counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }


def _field(page: Page, name: str) -> str:
    return f"{PANEL} [data-meta-field='{name}']"


def _fill_new(page: Page, *, reviewed: bool, provenance: str) -> None:
    page.locator(f"{PANEL} button[data-action='new-category-metadata']").click()
    page.locator(_field(page, "taxonomy_revision")).fill("taxonomy-ui-1")
    page.locator(_field(page, "category_id")).fill("50000803")
    page.locator(_field(page, "leaf")).check()
    page.locator(_field(page, "registrable")).check()
    page.locator(_field(page, "name_max_length")).fill("100")
    page.locator(_field(page, "attributes")).fill(
        "brand | y | n | REVIEW_REQUIRED | -\ncolor | n | n | REVIEW_REQUIRED | 20"
    )
    page.locator(_field(page, "notice_type")).fill("notice-ui-1")
    page.locator(_field(page, "notice_fields")).fill(
        "manufacturer | y | n | BLOCKED | -\norigin | y | y | REVIEW_REQUIRED | -"
    )
    page.locator(_field(page, "options_supported")).check()
    page.locator(_field(page, "max_options")).fill("5")
    page.locator(_field(page, "max_dimensions")).fill("1")
    page.locator(_field(page, "required_templates")).fill("shipping, returns")
    page.locator(_field(page, "evidence_reference")).fill("seller-center/50000803/2026-09-23")
    page.locator(_field(page, "content_provenance")).select_option(provenance)
    if reviewed:
        page.locator(_field(page, "reviewed")).check()


def _entry(page: Page) -> tuple[str | None, str | None, str]:
    row = page.locator(f"{PANEL} [data-metadata-key='{KEY}']")
    return (
        row.get_attribute("data-meta-reviewed"),
        row.get_attribute("data-meta-history"),
        row.inner_text(),
    )


def _save(page: Page) -> None:
    page.locator(f"{PANEL} button[data-action='save-category-metadata']").click()


def test_the_operator_records_then_reviews_metadata_and_it_survives_reload_and_restart(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    path = f"/api/v1/settings/category-metadata/{MARKETPLACE}/{KEY}/revisions"
    with _served(config) as client, _page(browser, client, writes) as page:
        assert "CATEGORY_METADATA_MISSING" in page.locator(PANEL).inner_text()
        assert writes == []
        _fill_new(page, reviewed=False, provenance="AI_SUGGESTION")
        _save(page)
        page.wait_for_selector(f"{PANEL} [data-metadata-key='{KEY}'][data-meta-reviewed='false']")
        assert "CATEGORY_METADATA_UNREVIEWED" in _entry(page)[2]

        # Review: load the recorded revision, confirm it from evidence, record it reviewed.
        page.locator(
            f"{PANEL} [data-metadata-key='{KEY}'] button[data-action='edit-category-metadata']"
        ).click()
        taxonomy = page.locator(_field(page, "taxonomy_revision"))
        assert (
            taxonomy.input_value() == "taxonomy-ui-1"
            and taxonomy.get_attribute("readonly") is not None
        )
        assert (
            "color | n | n | REVIEW_REQUIRED | 20"
            in page.locator(_field(page, "attributes")).input_value()
        )
        page.locator(_field(page, "content_provenance")).select_option("OPERATOR_CONFIRMED")
        page.locator(_field(page, "reviewed")).check()
        _save(page)
        page.wait_for_selector(f"{PANEL} [data-metadata-key='{KEY}'][data-meta-reviewed='true']")
        reviewed = _entry(page)
        assert reviewed[:2] == ("true", "2")
        assert "검토 완료 · operator" in reviewed[2] and "현재 리비전 #2" in reviewed[2]
        assert writes == [("POST", path), ("POST", path)]
        page.reload()
        page.wait_for_selector(f"{PANEL} [data-metadata-key='{KEY}'][data-meta-reviewed='true']")
        assert _entry(page) == reviewed
        served: Container = client.app.state.container  # type: ignore[attr-defined]
        materialized = served.registration_preflight.category_metadata(
            MARKETPLACE, "taxonomy-ui-1", "50000803"
        )
        assert materialized is not None and materialized.reviewed is True
        assert materialized.notice is not None
        assert [rule.missing_status.value for rule in materialized.notice.fields] == [
            "BLOCKED",
            "REVIEW_REQUIRED",
        ]
    with _served(config) as client, _page(browser, client, []) as page:
        page.wait_for_selector(f"{PANEL} [data-metadata-key='{KEY}'][data-meta-reviewed='true']")
        assert _entry(page) == reviewed
    assert _counts(config) == {TABLES[0]: 1, TABLES[1]: 2, TABLES[2]: 1}


def test_a_refused_save_shows_the_server_reason_and_writes_nothing(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client, _page(browser, client, writes) as page:
        _fill_new(page, reviewed=True, provenance="AI_SUGGESTION")
        _save(page)
        page.locator(".toast", has_text="CATEGORY_METADATA_AI_NOT_REVIEWABLE").wait_for(
            timeout=10_000
        )
        assert page.locator(f"{PANEL} [data-metadata-key='{KEY}']").count() == 0
        # The typed values stay for a fix; nothing was promoted locally.
        assert page.locator(_field(page, "category_id")).input_value() == "50000803"
    assert len(writes) == 1
    assert _counts(config) == dict.fromkeys(TABLES, 0)
