"""What a seller sees in 통합DB after a store import (ADR-0042 S2a, §0 b3), in the real UI.

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. The store's catalog is a fake source with synthetic listings, synced
through the real StoreCatalog owner, and the application's ListingLinks owner reads it. Proven:

- every imported listing is a common product in the list, named by its listing, marked
  「도매처 미연결」, with its SmartStore price and how it came (「스토어에서 가져옴」);
- the automatic link reads 「1개 상품이 판매자코드·바코드로 자동 연결됨」 with 「확인」 and
  「되돌리기」, and 되돌리기 ends that link through the owner;
- a name-similar pair waits in 「같은 상품인가요?」 with 「같은 상품」 / 「다른 상품」, and
  다른 상품 records the seller's answer;
- a product's detail shows its store listings;
- every word is plain Korean: codes stay in data-* attributes.
"""

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from tests.conftest import LOCAL
from tests.integration.operate.test_s2a_listing_links import (
    FakeSource,
    Harness,
    fields,
    supplier_group,
)
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.product_support import Collections

pytestmark = pytest.mark.integration

FORWARDED = ("x-icbm-client", "content-type", "accept")
PANEL = "[data-role='store-links']"
ROWS = "[data-role='product-list'] tr[data-product]"
CODES = (
    "IMPORTED",
    "STRONG_KEY",
    "USER_CONFIRMED",
    "NAME_SIMILAR",
    "SELLER_CODE",
    "GTIN",
    "STORE_LINK",
    "MEMBERSHIP",
)


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
def served(config: AppConfig) -> Iterator[tuple[TestClient, Harness, dict[str, str]]]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        container: Container = client.app.state.container  # type: ignore[attr-defined]
        sources = Collections.of(container, config)
        groups = {
            "km": supplier_group(container, sources, "287", "국산 참기름 300ml"),
            "similar": supplier_group(container, sources, "300", "국산 들기름 500ml 2병 선물"),
        }
        source = FakeSource()
        source.put("101", fields("볶음 참깨 1kg", code="OWN-0101", price=12900))
        source.put("102", fields("참기름 300ml 스토어 판매분", code="KM287", price=8900))
        source.put("103", fields("국산 들기름 500ml 2병 선물세트", code="OWN-0103", price=15900))
        harness = Harness(container, source)
        harness.sync()
        container.store_links = harness.links
        yield client, harness, groups


@pytest.fixture
def page(
    browser: Browser, served: tuple[TestClient, Harness, dict[str, str]]
) -> Iterator[tuple[Page, list[dict[str, Any]]]]:
    client, _harness, _groups = served
    posted: list[dict[str, Any]] = []

    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        if request.method == "POST":
            posted.append({"path": parts.path, "body": json.loads(request.post_data or "{}")})
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    opened = browser.new_page()
    opened.route("**/*", answer)
    try:
        opened.goto(f"{LOCAL}/#/db")
        opened.wait_for_selector(f"{PANEL}:not([hidden])", timeout=15_000)
        opened.wait_for_selector("[data-role='db-listing-name']", timeout=15_000)
        yield opened, posted
    finally:
        opened.close()


def _plain(text: str) -> None:
    assert not any(code in text for code in CODES), text


def test_every_imported_listing_reads_as_a_common_product_without_a_supplier(
    page: tuple[Page, list[dict[str, Any]]],
) -> None:
    opened, _ = page
    row = opened.locator(ROWS, has=opened.locator("text=볶음 참깨 1kg"))
    assert row.count() == 1
    text = row.inner_text()
    assert "도매처 미연결" in text and "스토어 상품번호 101" in text
    assert "12,900원" in text and "스토어에서 가져옴" in text
    assert row.locator("[data-link-kind='IMPORTED']").count() >= 1
    _plain(text)
    # Its detail names it by its listing and shows the store listing.
    row.click()
    opened.wait_for_selector("[data-role='store-listings']", timeout=10_000)
    detail = opened.locator("[data-role='product-detail']").inner_text()
    assert "볶음 참깨 1kg" in detail and "도매처 미연결" in detail
    assert "도매처가 연결되지 않아 아직 등록 대상으로 고를 수 없습니다." in detail
    _plain(detail)


def test_the_automatic_link_reads_as_a_notice_with_undo(
    page: tuple[Page, list[dict[str, Any]]],
) -> None:
    opened, posted = page
    panel = opened.locator(PANEL)
    text = panel.inner_text()
    assert "1개 상품이 판매자코드·바코드로 자동 연결됨" in text
    assert "마지막 동기화" in text
    _plain(text)
    opened.click("[data-action='store-link-notices-show']")
    notice = opened.locator("[data-role='store-link-notices'] li")
    assert "참기름 300ml 스토어 판매분" in notice.inner_text()
    assert "국산 참기름 300ml" in notice.inner_text()
    notice.locator("[data-action='store-link-undo']").click()
    opened.wait_for_function(
        "() => !document.querySelector(\"[data-role='store-link-notice']\")", timeout=10_000
    )
    (undo,) = [p for p in posted if p["path"].endswith("/unlink")]
    assert undo["body"] == {"actor": "operator", "undo": True}


def test_a_similar_name_waits_for_the_sellers_answer(
    page: tuple[Page, list[dict[str, Any]]],
    served: tuple[TestClient, Harness, dict[str, str]],
) -> None:
    opened, posted = page
    _client, _harness, groups = served
    proposal = opened.locator(
        f"[data-role='store-link-proposals'] li[data-product='{groups['similar']}']"
    )
    text = proposal.inner_text()
    assert "국산 들기름 500ml 2병 선물세트" in text and "국산 들기름 500ml 2병 선물" in text
    assert "상품명이 비슷합니다" in text
    assert "같은 상품인가요?" in opened.locator(PANEL).inner_text()
    assert proposal.locator("[data-action='store-link-same']").inner_text() == "같은 상품"
    _plain(text)
    proposal.locator("[data-action='store-link-different']").click()
    opened.wait_for_selector("[data-role='store-link-decisions']", timeout=10_000)
    assert opened.locator("[data-role='store-link-proposals']").count() == 0
    (reject,) = [p for p in posted if p["path"].endswith("/reject")]
    assert reject["body"]["product_group_id"] == groups["similar"]
