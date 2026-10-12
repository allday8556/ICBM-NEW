"""ADR-0036 O2 in the real UI: 수집관리 › 공급처 관리 › 「+ 공급처 추가」.

The page runs in the installed browser; every request it makes is answered in-process by the
application under test, which reads the scripted storefront (``tests.support.onboarding_support``)
and connects through the fake CONNECT gateway. Proven here:

- 「+ 공급처 추가」 and the 새 공급처 추가 card open the v29 credential modal (공급처명, 도메인, ID,
  Password); 「플랫폼 확인」 shows the detected platform, its marker evidence and the robots.txt and
  terms reads with their warning; 「저장」 waits for the acknowledgement;
- 「저장」 creates the draft and saves the login through CONNECT's credential route, sending the
  password exactly once and clearing it; 「연결 테스트」 passes and the card shows 분석 중;
- 사이트 분석 turns live once the card is 분석 중 (O3) and 수집 규칙 stays inert; the password is
  never shown;
- an unsupported site shows "템플릿이 필요합니다" with no choice and no 저장; an ambiguous
  one offers exactly its two matched templates;
- neither the screen nor the open modal scrolls horizontally at 1920×1080 or at 390×844.
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
from app.main import create_app
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.fake_suppliers import KMRETAIL_PAGES, PASSWORD, USERNAME, FakeGateway
from tests.support.onboarding_support import (
    ROBOTS_PRODUCTS,
    Storefront,
    ambiguous_site,
    cafe24_site,
    plain_site,
)

pytestmark = pytest.mark.integration

SUPPLIERS = f"{LOCAL}/#/collect?view=suppliers"
HOST = "shop.onboard-sample.co.kr"
KEY = "site-shop-onboard-sample-co-kr"
PLAIN_HOST = "plain-sample.co.kr"
BOTH_HOST = "both.onboard-sample.co.kr"
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
def storefront() -> Storefront:
    return Storefront(
        sites={
            HOST: cafe24_site(ROBOTS_PRODUCTS),
            PLAIN_HOST: plain_site(),
            BOTH_HOST: ambiguous_site(),
        }
    )


@pytest.fixture
def client(config: AppConfig, storefront: Storefront) -> Iterator[TestClient]:
    app = create_app(
        config,
        onboarding_reader=storefront.reader(),
        supplier_gateway=FakeGateway(KMRETAIL_PAGES),
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
          return {
            page: doc.scrollWidth - doc.clientWidth,
            card: card ? card.scrollWidth - card.clientWidth : 0,
          };
        }"""
    )
    assert overflow == {"page": 0, "card": 0}, overflow


def _detect(page: Page, host: str = HOST, expect: str = "[data-detected='cafe24']") -> None:
    modal = page.locator(MODAL)
    modal.get_by_role("textbox", name="공급처명", exact=True).fill("온보드 샘플")
    modal.get_by_role("textbox", name="도메인", exact=True).fill(f"https://{host}")
    modal.get_by_role("textbox", name="ID", exact=True).fill(USERNAME)
    modal.locator("input[type=password]").fill(PASSWORD)
    modal.locator("[data-action='detect-platform']").click()
    page.wait_for_selector(expect, timeout=15_000)


def test_a_supplier_is_added_saved_tested_and_shown_in_analysis(
    browser: Browser, client: TestClient, storefront: Storefront
) -> None:
    writes: list[tuple[str, str, str]] = []
    with _page(browser, client, writes, 1920, 1080) as page:
        for button in page.locator("[data-supplier] .supplier-actions button").all():
            if button.inner_text().strip() in ("사이트 분석", "수집 규칙"):
                assert button.get_attribute("data-inert"), button.inner_text()
        page.locator("[data-action='add-supplier']").click()
        modal = page.locator(MODAL)
        assert modal.locator("h2").inner_text().startswith("새 공급처 추가")
        _detect(page)
        assert "cafe24_login_form" in modal.locator(".onboarding-evidence").inner_text()
        assert modal.locator("[data-warning='ROBOTS_DISALLOWS_PRODUCTS']").is_visible()
        assert "Disallow: /product/" in modal.locator("[data-role='robots-lines']").inner_text()
        terms = modal.locator("[data-policy='TERMS'] a")
        assert terms.get_attribute("href") == f"https://{HOST}/member/agreement.html"
        save = modal.locator("[data-action='save-supplier']")
        test = modal.locator("[data-action='test-supplier']")
        assert save.is_disabled() and test.is_disabled()
        _no_horizontal_scroll(page)
        # Detection wrote nothing: one detection call, no create yet.
        assert [w[:2] for w in writes] == [("POST", "/api/v1/onboarding/detect")]

        modal.locator("[data-role='policy-ack']").check()
        assert save.is_enabled()
        save.click()
        page.wait_for_function(
            "() => document.querySelector('[data-role=onboarding-status]')"
            "?.textContent.includes('초안으로 저장')"
        )
        assert modal.locator("input[type=password]").input_value() == ""
        assert test.is_enabled() and save.is_disabled()

        test.click()
        page.wait_for_selector(MODAL, state="detached", timeout=30_000)
        card = page.locator(f"[data-supplier='{KEY}']")
        card.locator("[data-onboarding-state='RECON']").wait_for(timeout=15_000)
        assert card.locator("[data-onboarding-state='RECON']").inner_text() == "분석 중"
        # ADR-0036 O3: 사이트 분석 is live on a RECON card; 수집 규칙 stays inert until O4.
        analysis = card.get_by_role("button", name="사이트 분석")
        assert analysis.get_attribute("data-action") == "site-analysis"
        assert analysis.get_attribute("data-inert") is None
        assert card.get_by_role("button", name="수집 규칙").get_attribute("data-inert")
        assert PASSWORD not in page.content()
        _no_horizontal_scroll(page)

    paths = [(method, path) for method, path, _ in writes]
    assert paths == [
        ("POST", "/api/v1/onboarding/detect"),
        ("POST", "/api/v1/onboarding/suppliers"),
        ("PUT", f"/api/v1/connect/suppliers/{KEY}/credentials"),
        ("POST", f"/api/v1/connect/suppliers/{KEY}/test"),
        ("POST", f"/api/v1/onboarding/suppliers/{KEY}/recon"),
    ]
    # The password went out exactly once, to CONNECT's credential route.
    carrying = [
        path
        for _, path, body in writes
        if PASSWORD in json.loads(body or "{}").values() or PASSWORD in body
    ]
    assert carrying == [f"/api/v1/connect/suppliers/{KEY}/credentials"]
    created = json.loads(writes[1][2])
    assert created["acknowledged"] is True and created["template"] == "cafe24"
    assert "password" not in created and "username" not in created


@pytest.mark.parametrize(("width", "height"), [(1920, 1080), (390, 844)])
def test_the_add_card_opens_the_modal_without_horizontal_scroll(
    browser: Browser, client: TestClient, width: int, height: int
) -> None:
    writes: list[tuple[str, str, str]] = []
    with _page(browser, client, writes, width, height) as page:
        _no_horizontal_scroll(page)
        page.locator("[data-action='add-supplier-card']").click()
        page.wait_for_selector(MODAL)
        _detect(page)
        _no_horizontal_scroll(page)
        page.keyboard.press("Escape")
    assert [w[:2] for w in writes] == [("POST", "/api/v1/onboarding/detect")]


def test_an_unsupported_site_stops_and_an_ambiguous_one_offers_only_its_matches(
    browser: Browser, client: TestClient
) -> None:
    writes: list[tuple[str, str, str]] = []
    with _page(browser, client, writes, 1920, 1080) as page:
        page.locator("[data-action='add-supplier']").click()
        modal = page.locator(MODAL)
        # NO_MARKERS: "a template is needed", no choice offered, nothing can be saved.
        _detect(page, PLAIN_HOST, "[data-detected='unsupported']")
        assert "템플릿이 필요합니다" in modal.locator("[data-role='template-needed']").inner_text()
        assert modal.locator("[data-role='platform-choice']").count() == 0
        assert modal.locator("[data-role='policy']").count() == 0
        assert modal.locator("[data-role='policy-ack']").count() == 0
        assert modal.locator("[data-action='save-supplier']").is_disabled()

        # AMBIGUOUS: a choice between exactly the two matched templates.
        modal.get_by_role("textbox", name="도메인", exact=True).fill(f"https://{BOTH_HOST}")
        modal.locator("[data-action='detect-platform']").click()
        page.wait_for_selector("[data-role='ambiguous']", timeout=15_000)
        choice = modal.locator("[data-role='platform-choice']")
        options = choice.locator("option").all_inner_texts()
        assert options == ["선택", "Cafe24", "고도몰"]
        assert modal.locator("[data-action='save-supplier']").is_disabled()
        choice.select_option("godomall")
        page.wait_for_selector("[data-policy='TERMS']", timeout=15_000)
        assert modal.locator("[data-role='platform-choice']").input_value() == "godomall"
        modal.locator("[data-role='policy-ack']").check()
        assert modal.locator("[data-action='save-supplier']").is_enabled()
        _no_horizontal_scroll(page)
    assert [w[:2] for w in writes] == [("POST", "/api/v1/onboarding/detect")] * 3
    assert json.loads(writes[2][2]) == {"origin": f"https://{BOTH_HOST}", "template": "godomall"}
