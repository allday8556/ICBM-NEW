"""The 스토어 상품 동기화 card on 설정 › 스마트스토어 › API (ADR-0042 S1a, §0 a1–a2, b1).

The page is the real UI in the installed browser, answered in-process by the application under
test; the SmartStore caller talks to a fake transport and no provider is reached. The sync rows are
written straight into the test database (synthetic), so what is proven is what the seller sees for
each state: not connected, the first sync running with its progress, a complete sync with its count
and time, and a failed sync shown as such beside the last complete one.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page

from app.config import AppConfig
from tests.integration.connect.test_smartstore_operator_ui import (  # noqa: F401 - fixtures
    Traffic,
    _container,
    _no_egress,
    _page,
    browser,
    client,
    provider,
)

pytestmark = pytest.mark.integration

ACCOUNT = "mpa-fixture-ui"
CARD = "[data-role='store-catalog-status']"


def _run(
    config: AppConfig,
    run_id: str,
    *,
    state: str,
    outcome: str | None,
    error_code: str | None = None,
    target: int = 0,
    done: int = 0,
    started: str = "2026-10-15 05:02:00.000000",
) -> None:
    finished = None if state == "RUNNING" else started
    with sqlite3.connect(config.database_path) as raw:
        raw.execute(
            "INSERT INTO store_catalog_sync_runs (run_id, marketplace_key, marketplace_account_id,"
            " trigger, state, outcome, error_code, contract_version, total_elements, pages_read,"
            " listings_enumerated, details_target, details_done, details_failed, correlation_id,"
            " started_at, finished_at) VALUES (?, 'smartstore', ?, 'CONNECT', ?, ?, ?,"
            " 'fixture/v1', ?, 1, ?, ?, ?, 0, 'cid', ?, ?)",
            (
                run_id,
                ACCOUNT,
                state,
                outcome,
                error_code,
                target,
                target,
                target,
                done,
                started,
                finished,
            ),
        )


def _status(page: Page) -> str:
    page.wait_for_selector(CARD, timeout=15_000)
    page.wait_for_function(
        "(selector) => !document.querySelector(selector)?.textContent.includes('확인 중')"
        " || document.querySelector(selector)?.textContent.includes('스토어 상품 확인 중')",
        arg=CARD,
        timeout=15_000,
    )
    return page.locator(CARD).inner_text()


def test_before_connecting_the_card_says_the_first_sync_starts_by_itself(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    with _no_egress(), _page(browser, client, Traffic()) as page:
        text = _status(page)
        disabled = page.locator("[data-action='sync-store-catalog']").is_disabled()
    assert "스마트스토어 연결 후 자동으로 가져옵니다" in text
    assert disabled


def test_the_running_first_sync_shows_its_progress_then_the_complete_result(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    config: AppConfig,
) -> None:
    _container(client).store_catalog._account_of = lambda key: ACCOUNT  # type: ignore[method-assign]
    _run(config, "r-running", state="RUNNING", outcome=None, target=1200, done=340)
    with _no_egress(), _page(browser, client, Traffic()) as page:
        running = _status(page)
        disabled = page.locator("[data-action='sync-store-catalog']").is_disabled()
    assert "스토어 상품 가져오는 중 340 / 1,200" in running
    assert disabled
    with sqlite3.connect(config.database_path) as raw:
        raw.execute(
            "UPDATE store_catalog_sync_runs SET state = 'FINISHED', outcome = 'COMPLETE',"
            " details_done = 1200, finished_at = started_at WHERE run_id = 'r-running'"
        )
    with _no_egress(), _page(browser, client, Traffic()) as page:
        complete = _status(page)
    # The fixture records no observation, so the complete sync read 0 listings.
    assert "스토어 상품 0개 · 마지막 동기화" in complete


def test_a_failed_sync_is_shown_as_such_beside_the_last_complete_one(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    config: AppConfig,
) -> None:
    _container(client).store_catalog._account_of = lambda key: ACCOUNT  # type: ignore[method-assign]
    _run(config, "r-done", state="FINISHED", outcome="COMPLETE")
    _run(
        config,
        "r-failed",
        state="FINISHED",
        outcome="UNAVAILABLE",
        error_code="GW.RATE_LIMIT",
        started="2026-10-15 05:40:00.000000",
    )
    with _no_egress(), _page(browser, client, Traffic()) as page:
        text = _status(page)
        last = page.locator("[data-role='store-catalog-last-run']").inner_text()
    assert "마지막 동기화" in text
    assert "지금은 확인할 수 없음" in last and "호출 한도로 잠시 멈춤" in last
