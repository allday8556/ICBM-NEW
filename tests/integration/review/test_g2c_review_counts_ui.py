"""The dashboard's and 품절's review counts in the real UI (Gate 2 G2-C, ADR-0016 §7).

The page runs in the installed browser, and every request is answered in-process by the
application under test. The server decides every state: a kind it cannot count yet is shown as
such, never as 0; only an authoritative count is a number; and neither screen shows its empty
"nothing to do" card on a count that is not authoritative. The pages count nothing themselves.
"""

import pytest
from playwright.sync_api import Browser

from app.config import AppConfig
from tests.conftest import LOCAL
from tests.integration.review.test_g2b_collect_review import UnclearShop, collect_unclear
from tests.integration.review.test_g2b_collect_review_ui import _page, browser  # noqa: F401
from tests.support.collect_submit_support import served

pytestmark = pytest.mark.integration


def test_the_dashboard_shows_each_kind_as_the_server_states_it(
    browser: Browser,  # noqa: F811
    config: AppConfig,
) -> None:
    with served(config, UnclearShop()) as api:
        collect_unclear(api, "4242")  # one COLLECT STOCK item; both STOCK producers current
        writes: list[tuple[str, str]] = []
        with _page(browser, api, writes) as page:
            page.goto(f"{LOCAL}/#/dashboard")
            table = page.locator("[data-role='dashboard-review'] [data-role='review-counts']")
            table.wait_for(timeout=15_000)
            assert page.locator("#content .empty-state").count() == 0
            rows = {
                row.get_attribute("data-kind"): (
                    row.get_attribute("data-state"),
                    row.locator("[data-role='review-count']").inner_text(),
                )
                for row in table.locator("tbody tr").all()
            }
            assert rows["STOCK"] == ("CURRENT", "1건")
            for kind in ("COMPLIANCE", "FULFILLMENT", "SOURCE_CHANGE", "COLLECT_EVIDENCE"):
                state, text = rows[kind]
                assert state == "NOT_WIRED" and "0건" not in text, (kind, text)
            assert rows["REGISTRATION_ERROR"] == ("CURRENT", "0건")
            assert writes == []  # rendering reads only


def test_soldout_says_nothing_to_check_only_on_an_authoritative_zero(
    browser: Browser,  # noqa: F811
    config: AppConfig,
) -> None:
    # Since M6-B both STOCK producers are wired; with both current and nothing open the count is
    # an authoritative zero, so the approved empty card is shown — beside the recheck panel.
    with served(config, UnclearShop()) as api:
        writes: list[tuple[str, str]] = []
        with _page(browser, api, writes) as page:
            page.goto(f"{LOCAL}/#/soldout")
            page.locator("#content .empty-state").wait_for(timeout=15_000)
            assert page.locator("[data-role='soldout-not-authoritative']").count() == 0
            recheck = page.locator("[data-role='stock-recheck']")
            recheck.locator("[data-stock-recheck='READY']").wait_for(timeout=15_000)
            assert "판매 중인 등록 상품이 없습니다" in recheck.inner_text()
            assert writes == []
