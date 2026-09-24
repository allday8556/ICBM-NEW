"""The dashboard's and 품절's review counts in the real UI (Gate 2 G2-C, ADR-0016 §7).

The page runs in the installed browser, and every request is answered in-process by the
application under test. The server decides every state: a kind it cannot count yet is shown as
such, never as 0; only an authoritative count is a number; and neither screen shows its empty
"nothing to do" card on a count that is not authoritative. The pages count nothing themselves.
"""

import pytest
from playwright.sync_api import Browser

from app.config import AppConfig
from tests.collect_submit_support import served
from tests.conftest import LOCAL
from tests.integration.test_g2b_collect_review import UnclearShop, collect_unclear
from tests.integration.test_g2b_collect_review_ui import _page, browser  # noqa: F401

pytestmark = pytest.mark.integration


def test_the_dashboard_shows_each_kind_as_the_server_states_it(
    browser: Browser,  # noqa: F811
    config: AppConfig,
) -> None:
    with served(config, UnclearShop()) as api:
        collect_unclear(api, "4242")  # one COLLECT STOCK item, known but not a count
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
            assert rows["STOCK"] == ("NOT_WIRED", "확인된 1건 이상")
            for kind in ("COMPLIANCE", "FULFILLMENT", "SOURCE_CHANGE", "COLLECT_EVIDENCE"):
                state, text = rows[kind]
                assert state == "NOT_WIRED" and "0건" not in text, (kind, text)
            assert rows["REGISTRATION_ERROR"] == ("CURRENT", "0건")
            assert writes == []  # rendering reads only


def test_soldout_never_says_there_is_nothing_to_check_on_a_stock_count_it_cannot_make(
    browser: Browser,  # noqa: F811
    config: AppConfig,
) -> None:
    with served(config, UnclearShop()) as api:
        writes: list[tuple[str, str]] = []
        with _page(browser, api, writes) as page:
            page.goto(f"{LOCAL}/#/soldout")
            panel = page.locator("[data-role='soldout-review']")
            panel.wait_for(timeout=15_000)
            assert panel.get_attribute("data-state") == "NOT_WIRED"
            assert page.locator("[data-role='soldout-not-authoritative']").count() == 1
            # The approved empty card ("확인할 재고 항목이 없습니다") is not shown, and no zero.
            assert page.locator("#content .empty-state").count() == 0
            assert "0건" not in panel.inner_text()
            assert writes == []
