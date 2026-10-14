"""ADR-0036 O3 in the real UI: 수집관리 › 공급처 관리 › 「사이트 분석」.

The page runs in the installed browser; every request it makes is answered in-process by the
application under test, whose collection gateway reads a scripted storefront
(``tests.support.onboarding_trial_support``) with a fixed session. Proven here:

- 「사이트 분석」 is live on an onboarded supplier's card while it is 분석 중 and opens the analysis
  panel (「수집 규칙」 beside it is O4's, proven in its own suite);
- 「샘플 읽기」 queues one trial; the panel then shows the sample's field table (읽음/없음/못 읽음
  with the value summary), its unread rows with the slot a rule proposes, its structural problem as
  "템플릿 수정 필요", the progress line "샘플 n/3 확인 가능" and the observed hosts;
- 「확인」 confirms an observed image host through the API and the row shows 확인됨;
- the storefront was read exactly once and no image was requested; no credential, session value or
  member text reaches the page;
- neither the screen nor the open panel scrolls horizontally at 1920×1080 or at 390×844.
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
from tests.support.fake_suppliers import KMRETAIL_PAGES, FakeGateway
from tests.support.onboarding_trial_support import (
    CAFE24_HOST,
    CAFE24_ORIGIN,
    IMAGE_HOST,
    MEMBER_EMAIL,
    MEMBER_NAME,
    SESSION_VALUE,
    StubSessions,
    TrialShop,
    cafe24_page,
    cafe24_url,
    recon_site,
    row,
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


def _sample_page(number: int) -> str:
    # Two product modules (a structure no words can read), and rows the default words do not name.
    return cafe24_page(
        number,
        duplicate_module=True,
        extra_rows=row("회원가", "9,000원") + row("발주마감", "오후 1시"),
    )


def _open_analysis(page: Page, key: str) -> None:
    card = page.locator(f"[data-supplier='{key}']")
    card.locator("[data-onboarding-state='RECON']").wait_for(timeout=15_000)
    # 「수집 규칙」 is live as well (O4, collect/words.js).
    assert card.locator("[data-action='collection-words']").get_attribute("data-inert") is None
    button = card.locator("[data-action='site-analysis']")
    assert button.get_attribute("data-inert") is None
    button.click()
    page.wait_for_selector("[data-role='site-analysis']", timeout=15_000)
    page.wait_for_selector("[data-role='no-samples']", timeout=15_000)


def _read_sample(page: Page, url: str) -> None:
    modal = page.locator(MODAL)
    modal.locator("[data-role='sample-url']").fill(url)
    modal.locator("[data-action='add-sample']").click()
    page.wait_for_selector("[data-sample] [data-role='field-table']", timeout=30_000)


def test_a_sample_is_read_reported_and_its_image_host_confirmed(
    browser: Browser, client: TestClient, shop: TrialShop
) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    key = recon_site(container, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(901)
    shop.serve(url, _sample_page(901))
    writes: list[tuple[str, str, str]] = []
    with _page(browser, client, writes, 1920, 1080) as page:
        _open_analysis(page, key)
        modal = page.locator(MODAL)
        assert "샘플 0/3 확인 가능" in modal.locator("[data-role='ready-count']").inner_text()
        _read_sample(page, url)

        sample = modal.locator("[data-sample]")
        fields = sample.locator("[data-role='field-table']")
        assert fields.locator("[data-field='original_name'] [data-status]").inner_text() == "읽음"
        assert fields.locator("[data-field='options'] [data-status]").inner_text() == "없음"
        assert "트라이얼 상품 901" in fields.locator("[data-field='original_name']").inner_text()
        assert "판매가 12,000원" in fields.locator("[data-field='prices']").inner_text()
        assert "판매 중" in fields.locator("[data-field='stock']").inner_text()
        unread = sample.locator("[data-role='unread-rows']")
        assert (
            unread.locator("[data-unread='회원가'] [data-candidate]").get_attribute(
                "data-candidate"
            )
            == "price"
        )
        assert "제안 없음" in unread.locator("[data-unread='발주마감']").inner_text()
        flag = sample.locator("[data-structural='DUPLICATED_ORDER_LIST']")
        assert "템플릿 수정 필요" in flag.inner_text()
        assert sample.locator("[data-role='not-verifiable']").is_visible()
        assert "샘플 0/3 확인 가능" in modal.locator("[data-role='ready-count']").inner_text()
        _no_horizontal_scroll(page)

        hosts = modal.locator("[data-role='observed-hosts']")
        image = hosts.locator(f"tr[data-host='{IMAGE_HOST}'][data-role-name='image']")
        image.locator("[data-action='confirm-host']").click()
        image.locator("[data-action='disable-host']").wait_for(timeout=15_000)
        assert "확인됨" in image.inner_text()
        # An asset host is listed and never confirmable.
        asset = hosts.locator("tr[data-host='assets.trial-cdn.com']")
        assert asset.locator("button").count() == 0

        content = page.content()
        for secret in (SESSION_VALUE, MEMBER_NAME, MEMBER_EMAIL, "csrf-secret-value-77"):
            assert secret not in content, secret
        _no_horizontal_scroll(page)

    # One product read on the storefront, and nothing else requested of any supplier host.
    assert shop.requests == [("GET", CAFE24_HOST, "/product/sample/901/")]
    paths = [(method, path) for method, path, _ in writes]
    assert paths == [
        ("POST", f"/api/v1/onboarding/suppliers/{key}/trials"),
        ("POST", f"/api/v1/onboarding/suppliers/{key}/hosts"),
    ]
    assert json.loads(writes[0][2]) == {"sample_url": url}
    confirmed = json.loads(writes[1][2])
    assert confirmed["host"] == IMAGE_HOST and confirmed["role"] == "image"


def test_the_panel_fits_a_phone_without_horizontal_scroll(
    browser: Browser, client: TestClient, shop: TrialShop
) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    key = recon_site(container, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(902)
    shop.serve(url, _sample_page(902))
    writes: list[tuple[str, str, str]] = []
    with _page(browser, client, writes, 390, 844) as page:
        _no_horizontal_scroll(page)
        _open_analysis(page, key)
        _no_horizontal_scroll(page)
        _read_sample(page, url)
        _no_horizontal_scroll(page)
        page.keyboard.press("Escape")
    assert [w[:2] for w in writes] == [("POST", f"/api/v1/onboarding/suppliers/{key}/trials")]
