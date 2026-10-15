"""ADR-0036 O4 in the real UI: 수집관리 › 공급처 관리 › 「수집 규칙」.

The page runs in the installed browser; every request it makes is answered in-process by the
application under test, whose collection gateway reads a scripted storefront (U-PICK's real
Cafe24 product page moved onto the test host). Proven here:

- 「수집 규칙」 is live on an onboarded supplier's card while it is 분석 중 and opens the words
  editor: every slot with the template's own words and an empty field for the supplier's words;
- 「미리보기」 sends the typed words and shows the unread row they now read, from the stored
  capture: the storefront is not read again and nothing is saved;
- 「저장」 sends the words with the revision they were edited from, the editor shows the saved
  revision, and 「다시 분석」 re-reads the stored capture: still no supplier read;
- neither the screen nor the open editor scrolls horizontally at 1920×1080 or at 390×844.
"""

import json
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
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.fake_suppliers import KMRETAIL_PAGES, FakeGateway
from tests.support.onboarding_support import wait_until
from tests.support.onboarding_trial_support import (
    CAFE24_HOST,
    CAFE24_ORIGIN,
    OPERATOR,
    StubSessions,
    TrialShop,
    cafe24_url,
    recon_site,
    upick_page,
)

pytestmark = pytest.mark.integration

SUPPLIERS = f"{LOCAL}/#/collect?view=suppliers"
FORWARDED = ("x-icbm-client", "content-type", "accept")
MODAL = ".modal"


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


@pytest.fixture
def shop() -> TrialShop:
    return TrialShop()


@pytest.fixture
def client(config: AppConfig, shop: TrialShop) -> Iterator[TestClient]:
    app = create_app(
        config,
        supplier_gateway=FakeGateway(KMRETAIL_PAGES),
        collection_gateway=shop.gateway(),
        collection_sessions=StubSessions(),
    )
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client


@contextmanager
def _page(
    browser: Browser,
    client: TestClient,
    writes: list[tuple[str, str, str]],
    width: int,
    height: int,
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        body = request.post_data_buffer
        if request.method != "GET":
            writes.append((request.method, parts.path, (body or b"").decode("utf-8")))
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = client.request(request.method, path, content=body, headers=headers)
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page(viewport={"width": width, "height": height})
    page.route("**/*", answer)
    try:
        page.goto(SUPPLIERS)
        page.wait_for_selector("[data-action='add-supplier']", timeout=15_000)
        yield page
    finally:
        page.close()


def _no_horizontal_scroll(page: Page) -> None:
    overflow = page.evaluate(
        """() => {
          const doc = document.documentElement;
          const card = document.querySelector('.modal .modal-card');
          const body = document.querySelector('.modal .modal-body');
          return {
            page: doc.scrollWidth - doc.clientWidth,
            card: card ? card.scrollWidth - card.clientWidth : 0,
            body: body ? body.scrollWidth - body.clientWidth : 0,
          };
        }"""
    )
    assert overflow == {"page": 0, "card": 0, "body": 0}, overflow


def _analysed(container: Container, shop: TrialShop) -> str:
    """A RECON site with one sample read (U-PICK 4954), through the running job worker."""
    key = recon_site(container, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(4954)
    shop.serve(url, upick_page("4954"))
    submitted = container.onboarding_analysis.submit(key, url, actor=OPERATOR)
    wait_until(lambda: container.jobs.get(submitted.job_id).state in ("SUCCEEDED", "DEAD"))
    assert container.jobs.get(submitted.job_id).state == "SUCCEEDED"
    return key


def _open_words(page: Page, key: str) -> None:
    card = page.locator(f"[data-supplier='{key}']")
    card.locator("[data-onboarding-state='RECON']").wait_for(timeout=15_000)
    button = card.locator("[data-action='collection-words']")
    assert button.get_attribute("data-inert") is None
    button.click()
    page.wait_for_selector("[data-role='collection-words'] [data-slot='price']", timeout=15_000)


def test_words_are_previewed_saved_and_reanalysed_without_a_supplier_read(
    browser: Browser, client: TestClient, shop: TrialShop
) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    key = _analysed(container, shop)
    writes: list[tuple[str, str, str]] = []
    with _page(browser, client, writes, 1920, 1080) as page:
        _open_words(page, key)
        modal = page.locator(MODAL)
        price = modal.locator("[data-slot='price']")
        assert "판매가" in price.locator("[data-role='defaults']").inner_text()
        assert price.locator("[data-role='slot-words']").input_value() == ""
        _no_horizontal_scroll(page)

        modal.locator("[data-slot='purchase_price'] [data-role='slot-words']").fill("회원가")
        # A word may hold a comma (ADR-0035's phrases): one word per line, never split at a comma.
        modal.locator("[data-slot='channel_all_allowed'] [data-role='slot-words']").fill(
            "쿠팡, 스마트스토어 판매 가능"
        )
        modal.locator("[data-action='preview-words']").click()
        sample = modal.locator("[data-preview-sample]")
        sample.wait_for(timeout=15_000)
        assert sample.locator("[data-role='newly-read'] [data-label='회원가']").is_visible()
        assert (
            "아직 저장하지 않았습니다" in modal.locator("[data-role='words-status']").inner_text()
        )
        _no_horizontal_scroll(page)

        modal.locator("[data-action='save-words']").click()
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='words-status']\")"
            "?.dataset.wordsSeq === '1'",
            timeout=15_000,
        )
        assert (
            modal.locator("[data-slot='purchase_price'] [data-role='slot-words']").input_value()
            == "회원가"
        )
        modal.locator("[data-action='reanalyse-words']").click()
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='words-status']\")"
            "?.textContent.includes('다시 분석했습니다')",
            timeout=15_000,
        )
        _no_horizontal_scroll(page)

    # The storefront was read once, by the trial; the editor read nothing from it.
    assert shop.requests == [("GET", CAFE24_HOST, "/product/sample/4954/")]
    base = f"/api/v1/onboarding/suppliers/{key}"
    assert [(method, path) for method, path, _ in writes] == [
        ("POST", f"{base}/words/preview"),
        ("PUT", f"{base}/words"),
        ("POST", f"{base}/reanalyse"),
    ]
    words = {"purchase_price": ["회원가"], "channel_all_allowed": ["쿠팡, 스마트스토어 판매 가능"]}
    assert json.loads(writes[0][2]) == {"words": words}
    assert json.loads(writes[1][2]) == {"words": words, "expected_seq": 0}
    saved = container.onboarding_words.view(key)
    assert {s.slot: s.site for s in saved.slots}["channel_all_allowed"] == [
        "쿠팡, 스마트스토어 판매 가능"
    ]
    assert container.onboarding.current(key).words_seq == 1


def test_the_editor_fits_a_phone_without_horizontal_scroll(
    browser: Browser, client: TestClient, shop: TrialShop
) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    key = _analysed(container, shop)
    writes: list[tuple[str, str, str]] = []
    with _page(browser, client, writes, 390, 844) as page:
        _no_horizontal_scroll(page)
        _open_words(page, key)
        _no_horizontal_scroll(page)
        modal = page.locator(MODAL)
        modal.locator("[data-slot='purchase_price'] [data-role='slot-words']").fill("회원가")
        modal.locator("[data-action='preview-words']").click()
        modal.locator("[data-preview-sample]").wait_for(timeout=15_000)
        _no_horizontal_scroll(page)
        page.keyboard.press("Escape")
    assert [w[:2] for w in writes] == [
        ("POST", f"/api/v1/onboarding/suppliers/{key}/words/preview")
    ]
