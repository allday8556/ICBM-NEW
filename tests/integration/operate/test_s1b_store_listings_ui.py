"""통합DB 「스토어 상품」 in the real UI (ADR-0042 S1b, §0 a2 and b2).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. The catalog is filled by the StoreCatalog owner's own sync over the fake
source of the S1a suite (synthetic values); no provider is reached. Proven here, as the seller
sees it:
- a store that is not connected reads a plain prompt that leads to 설정 › API 관리, and nothing can
  be synced;
- every status filter shows the server's count, and choosing one lists exactly those listings,
  each with its price, stock, options, shipping, status and seller code; a value the catalog does
  not keep reads 데이터 없음, and no provider token is shown as text;
- 「마지막 동기화」 stays on screen while a sync runs, beside its progress; 지금 동기화 starts one
  read-only sync and the list is read again when it ends;
- the search finds a listing by name or seller code, and the server pages the list;
- at 375 px the page never scrolls sideways; only the table scrolls inside its panel.
"""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route

from app.config import AppConfig
from app.container import Container
from app.stages.operate.catalog.service import AUTO, StoreCatalogService
from tests.conftest import LOCAL
from tests.integration.connect.test_smartstore_operator_ui import (  # noqa: F401 - fixtures
    _no_egress,
    browser,
)
from tests.integration.operate.test_s1a_store_catalog import ACCOUNT, CID, FakeSource
from tests.integration.operate.test_s1b_store_listings import _store

pytestmark = pytest.mark.integration

STORE_TAB = f"{LOCAL}/#/db?view=store"
FORWARDED = ("x-icbm-client", "content-type", "accept")
ROWS = "[data-role='store-listings'] tr[data-listing]"
LAST_SYNC = "[data-role='store-last-sync']"
# What a seller must never read as text: provider tokens and ICBM codes.
TOKENS = re.compile(
    r"\b(SALE|OUTOFSTOCK|SUSPENSION|CLOSE|WAIT|DELETE|STATUS_DELETE|HTTP_404|NOT_SEEN|ENDED|"
    r"NOT_READ|OTHER_STATUS|ON_SALE|READ_FAILED|CONDITIONAL_FREE|PREPAID|INCOMPLETE|"
    r"STORE_CATALOG_[A-Z_]+)\b"
)


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


def _container(client: TestClient) -> Container:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    return container


def _connect(client: TestClient, source: FakeSource) -> StoreCatalogService:
    service = _container(client).store_catalog
    service._source = source
    service._account_of = lambda key: ACCOUNT  # type: ignore[method-assign]
    service._pause_s = 0.0
    return service


@contextmanager
def _screen(chrome: Browser, client: TestClient, url: str = STORE_TAB) -> Iterator[Page]:
    writes: list[tuple[str, str]] = []

    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        if request.method != "GET":
            writes.append((request.method, parts.path))
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = chrome.new_page()
    page.route("**/*", answer)
    page.writes = writes  # type: ignore[attr-defined]
    try:
        page.goto(url)
        page.wait_for_selector("[data-role='store-workspace']", timeout=15_000)
        yield page
    finally:
        page.close()


def _rows(page: Page) -> list[str]:
    return [row.get_attribute("data-origin-product") or "" for row in page.locator(ROWS).all()]


def _choose(page: Page, key: str) -> None:
    page.locator(f"[data-role='store-filters'] [data-filter='{key}']").click()
    page.wait_for_function(
        "(key) => document.querySelector(\"[data-role='store-listings']\")?.dataset.filter === key",
        arg=key,
        timeout=15_000,
    )


def test_a_store_that_is_not_connected_leads_to_api_settings(
    browser: Browser,  # noqa: F811
    client: TestClient,
) -> None:
    with _no_egress(), _screen(browser, client) as page:
        prompt = page.locator("[data-role='store-not-connected']").inner_text()
        disabled = page.locator("[data-action='sync-store-catalog']").is_disabled()
        page.locator("[data-action='open-store-settings']").click()
        page.wait_for_url(re.compile(r"#/settings\?tab=smartstore&sub=api"), timeout=15_000)
    assert "스마트스토어가 연결되어 있지 않습니다" in prompt and "마지막 동기화 없음" in prompt
    assert "API 관리" in prompt and disabled


def test_each_filter_shows_the_servers_count_and_lists_exactly_its_listings(
    browser: Browser,  # noqa: F811
    client: TestClient,
    source: FakeSource,
) -> None:
    _store(source, _connect(client, source), None)
    counts = client.get("/api/v1/operate/store-catalog/listings").json()["counts"]
    with _no_egress(), _screen(browser, client) as page:
        shown = {
            chip.get_attribute("data-filter"): chip.locator(
                "[data-role='filter-count']"
            ).inner_text()
            for chip in page.locator("[data-role='store-filters'] [data-filter]").all()
        }
        assert shown == {key: str(value) for key, value in counts.items()}
        assert sorted(_rows(page)) == ["101", "102", "103", "104", "105", "106", "107", "108"]
        bar = page.locator("[data-role='store-sync-bar']").inner_text()
        failure = page.locator("[data-role='store-sync-failure']").inner_text()
        whole = page.locator("[data-role='store-workspace']").inner_text()
        on_sale = page.locator("tr[data-origin-product='101']").inner_text()
        _choose(page, "OUT_OF_STOCK")
        out_of_stock = _rows(page)
        _choose(page, "NOT_SEEN")
        not_seen = (_rows(page), page.locator(ROWS).first.inner_text())
        _choose(page, "ENDED")
        ended = (_rows(page), page.locator(ROWS).first.inner_text())
        _choose(page, "NOT_READ")
        not_read = (_rows(page), page.locator(ROWS).first.inner_text())
        _choose(page, "SALE_CLOSED")
        closed = _rows(page)
    # The second sync was INCOMPLETE (a live listing outside it): the last complete one stays.
    assert "스토어 상품 7개" in bar and "마지막 동기화" in bar
    assert "일부만 확인됨" in failure and "목록에 없는 판매 상품이 있음" in failure
    assert "Red Mug" in on_sale and "10,000원" in on_sale and "판매중" in on_sale
    assert "KM-101" in on_sale and "색상" in on_sale and "조합 1개" in on_sale
    assert "3,000원 (30,000원 이상 무료)" in on_sale and "선결제" in on_sale
    assert "데이터 없음" in on_sale  # 할인 and the unnamed category
    assert out_of_stock == ["102"]
    assert not_seen[0] == ["106"] and "스토어에서 확인 안 됨" in not_seen[1]
    assert "마지막으로 읽은 상태: 판매중" in not_seen[1]
    assert ended[0] == ["107"] and "종료(삭제됨)" in ended[1]
    assert "판매 상태가 삭제로 확인됨" in ended[1]
    assert not_read[0] == ["108"] and "확인 전" in not_read[1] and "상세를 읽지 못함" in not_read[1]
    assert closed == ["104"]
    assert TOKENS.search(whole) is None, TOKENS.search(whole)


def test_the_last_sync_stays_visible_while_a_sync_runs_and_the_list_reloads_after(
    browser: Browser,  # noqa: F811
    client: TestClient,
    config: AppConfig,
    source: FakeSource,
) -> None:
    service = _connect(client, source)
    source.listing("101", "102")
    service.sync(trigger=AUTO, correlation_id=CID)
    with sqlite3.connect(config.database_path) as raw:
        raw.execute(
            "INSERT INTO store_catalog_sync_runs (run_id, marketplace_key, marketplace_account_id,"
            " trigger, state, outcome, error_code, contract_version, full_read, total_elements,"
            " pages_read, listings_enumerated, details_target, details_done, details_failed,"
            " correlation_id, started_at, finished_at) VALUES ('r-running', 'smartstore', ?,"
            " 'AUTO', 'RUNNING', NULL, NULL, 'fixture/v1', 0, 1200, 3, 1200, 1200, 10, 0, 'cid',"
            " '2999-01-01 00:00:00.000000', NULL)",
            (ACCOUNT,),
        )
    with _no_egress(), _screen(browser, client) as page:
        running = page.locator("[data-role='store-sync-bar']").inner_text()
        last_sync = page.locator(LAST_SYNC).inner_text()
        disabled = page.locator("[data-action='sync-store-catalog']").is_disabled()
        # The run ends; the next status poll sees it and the list is read again.
        with sqlite3.connect(config.database_path) as raw:
            raw.execute(
                "UPDATE store_catalog_sync_runs SET state = 'FINISHED', outcome = 'ERROR',"
                " error_code = 'FIXTURE', finished_at = started_at WHERE run_id = 'r-running'"
            )
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='store-sync-bar']\")?.dataset.state"
            " === 'complete'",
            timeout=15_000,
        )
        after = page.locator("[data-role='store-sync-bar']").inner_text()
        enabled = page.locator("[data-action='sync-store-catalog']").is_enabled()
    assert "스토어 상품 가져오는 중 10 / 1,200" in running
    assert last_sync.startswith("마지막 동기화 ") and "없음" not in last_sync
    assert last_sync in running and disabled
    assert "가져오는 중" not in after and last_sync in after and "확인 실패" in after
    assert enabled


def test_sync_now_starts_one_read_only_sync_and_shows_its_result(
    browser: Browser,  # noqa: F811
    client: TestClient,
    source: FakeSource,
) -> None:
    service = _connect(client, source)
    service.sync(trigger=AUTO, correlation_id=CID)  # an empty store: a valid complete result
    source.listing("301")
    with _no_egress(), _screen(browser, client) as page:
        empty = page.locator("[data-role='store-listings']").inner_text()
        before = page.locator("[data-role='store-sync-bar']").inner_text()
        page.locator("[data-action='sync-store-catalog']").click()
        page.wait_for_selector("tr[data-origin-product='301']", timeout=15_000)
        after = page.locator("[data-role='store-sync-bar']").inner_text()
        writes = list(page.writes)  # type: ignore[attr-defined]
    assert "스토어에 상품이 없습니다" in empty and "스토어 상품 0개 · 마지막 동기화" in before
    assert "스토어 상품 1개 · 마지막 동기화" in after
    assert writes == [("POST", "/api/v1/operate/store-catalog/sync")]


def test_the_search_finds_by_name_or_seller_code_and_the_server_pages_the_list(
    browser: Browser,  # noqa: F811
    client: TestClient,
    source: FakeSource,
) -> None:
    service = _connect(client, source)
    _store(source, service, None)
    with _no_egress(), _screen(browser, client) as page:
        page.locator("[data-role='store-search'] input[type='search']").fill("mug")
        page.locator("[data-role='store-search'] button[type='submit']").click()
        page.wait_for_url(re.compile(r"q=mug"), timeout=15_000)
        page.wait_for_function(
            f'() => document.querySelectorAll("{ROWS}").length === 2', timeout=15_000
        )
        by_name = sorted(_rows(page))
        counted = page.locator(
            "[data-role='store-filters'] [data-filter='ALL'] strong"
        ).inner_text()
        page.locator("[data-role='store-search'] input[type='search']").fill("KM-101")
        page.locator("[data-role='store-search'] button[type='submit']").click()
        page.wait_for_function(
            f'() => document.querySelectorAll("{ROWS}").length === 1', timeout=15_000
        )
        by_code = _rows(page)
    assert by_name == ["101", "102"] and counted == "2" and by_code == ["101"]


def test_the_server_pages_the_list_fifty_at_a_time(
    browser: Browser,  # noqa: F811
    client: TestClient,
    source: FakeSource,
) -> None:
    service = _connect(client, source)
    source.listing(*[str(1000 + n) for n in range(120)])
    service.sync(trigger=AUTO, correlation_id=CID)
    with _no_egress(), _screen(browser, client, f"{STORE_TAB}&filter=ON_SALE") as page:
        first = (len(_rows(page)), page.locator("[data-role='store-page-range']").inner_text())
        page.locator("[data-role='store-listings'] [data-action='next-page']").click()
        page.wait_for_url(re.compile(r"page=2"), timeout=15_000)
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='store-page-range']\")"
            "?.textContent.startsWith('51')",
            timeout=15_000,
        )
        second = (len(_rows(page)), page.locator("[data-role='store-page-range']").inner_text())
    assert first == (50, "1–50 / 120개")
    assert second == (50, "51–100 / 120개")


def test_at_375_px_the_page_never_scrolls_sideways(
    browser: Browser,  # noqa: F811
    client: TestClient,
    source: FakeSource,
) -> None:
    _store(source, _connect(client, source), None)
    with _no_egress(), _screen(browser, client) as page:
        page.set_viewport_size({"width": 375, "height": 812})
        page.wait_for_timeout(300)
        geometry = page.evaluate(
            """() => {
              const table = document.querySelector('.store-table-scroll');
              const last = document.querySelector("[data-role='store-last-sync']");
              return {
                page: document.documentElement.scrollWidth <= document.documentElement.clientWidth,
                table: table.scrollWidth > table.clientWidth,
                lastSyncVisible: last.getBoundingClientRect().width > 0,
                lastSyncInside: last.getBoundingClientRect().right <= window.innerWidth,
              };
            }"""
        )
    assert geometry == {
        "page": True,
        "table": True,
        "lastSyncVisible": True,
        "lastSyncInside": True,
    }
