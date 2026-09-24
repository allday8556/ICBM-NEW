"""M4 and REGISTER ReviewItems on their own existing screens (Gate 2 G2-C B1, review 5810256789).

The page runs in the installed browser, and every request is answered in-process by the
application under test. 통합DB shows a Product's M4 items and 등록관리 an account's REGISTER items,
each with its producers' coverage verdict; there is no top-level review screen. A resolution sends
the scope and generation it was shown; the owner is re-derived, so a still-derived condition stays
open, and a reload rebuilds the same state from durable rows. A coverage that is not current is
said so, never shown as "nothing to review". The pages count nothing and decide nothing.
"""

import pytest
from playwright.sync_api import Browser, Page

from app.config import AppConfig
from app.container import Container
from app.review.preflight_producer import PREFLIGHT_PRODUCER
from app.review.products_producer import PRODUCTS_PRODUCER
from tests.collect_submit_support import served
from tests.conftest import LOCAL
from tests.integration.test_g2b_collect_review import UnclearShop, hold_the_running_recovery
from tests.integration.test_g2b_collect_review_ui import _page, browser  # noqa: F401
from tests.integration.test_g2c_review_counts import m4_item
from tests.integration.test_g2c_review_paths import passes, prepared

pytestmark = pytest.mark.integration


def _resolve_and_reload(page: Page, block_selector: str, reason: str) -> str:
    block = page.locator(block_selector)
    block.wait_for(timeout=15_000)
    assert block.get_attribute("data-coverage") == "CURRENT"
    item = block.locator("[data-review-item][data-state='OPEN']", has_text=reason)
    assert item.count() == 1
    item_id = str(item.get_attribute("data-review-item"))
    item.locator("[data-role='review-disposition']").select_option("FOLLOW_UP_REQUIRED")
    item.locator("[data-action='resolve-review']").click()
    # The owner still derives the condition: the item stays open, at the same generation.
    page.wait_for_selector(
        f"{block_selector} [data-role='review-outcome'][data-outcome='CONDITION_PERSISTS']",
        timeout=15_000,
    )
    again = page.locator(f"[data-review-item='{item_id}']")
    assert (again.get_attribute("data-state"), again.get_attribute("data-generation")) == (
        "OPEN",
        "1",
    )
    page.reload()
    reloaded = page.locator(f"[data-review-item='{item_id}']")
    reloaded.wait_for(timeout=15_000)
    assert reloaded.get_attribute("data-state") == "OPEN"
    return item_id


def test_a_product_shows_its_m4_review_items_on_the_product_db(
    browser: Browser,  # noqa: F811
    config: AppConfig,
) -> None:
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        hold_the_running_recovery(container)
        result, _ = m4_item(container, config, "7101")
        passes(container)
        group = str(result.product_group_id)
        writes: list[tuple[str, str]] = []
        with _page(browser, api, writes) as page:
            page.goto(f"{LOCAL}/#/db?product={group}")
            selector = (
                f"[data-role='product-review'][data-product='{group}'] [data-role='review-items']"
            )
            item_id = _resolve_and_reload(page, selector, "IMAGE_SELECTION_MISSING")
            assert writes == [("POST", f"/api/v1/review/items/{item_id}/resolve")]
            assert container.review_items.item(item_id).producer == PRODUCTS_PRODUCER
            # The owner moves and no pass has run: the list says it is not current.
            m4_item(container, config, "7102")
            page.reload()
            block = page.locator(selector)
            block.wait_for(timeout=15_000)
            assert block.get_attribute("data-coverage") == "NOT_CURRENT"
            assert (
                block.locator(f"[data-coverage='{PRODUCTS_PRODUCER}']").get_attribute("data-reason")
                == "REVIEW_OWNER_MOVED_SINCE_PASS"
            )


def test_an_account_shows_its_register_review_items_on_registration_management(
    browser: Browser,  # noqa: F811
    config: AppConfig,
) -> None:
    with served(config, UnclearShop()) as api:
        container: Container = api.app.state.container  # type: ignore[attr-defined]
        hold_the_running_recovery(container)
        made = prepared(api, container, config)
        passes(container)
        writes: list[tuple[str, str]] = []
        with _page(browser, api, writes) as page:
            page.goto(f"{LOCAL}/#/register")
            selector = (
                f"[data-role='register-review'][data-account='{made['account']}']"
                " [data-role='review-items']"
            )
            item_id = _resolve_and_reload(page, selector, "AUTHORING_REVISIONS_UNOWNED")
            assert writes == [("POST", f"/api/v1/review/items/{item_id}/resolve")]
            assert container.review_items.item(item_id).producer == PREFLIGHT_PRODUCER
            scope = page.locator(f"[data-review-item='{item_id}'] [data-role='review-scope']")
            assert made["preparation_id"][:8] in scope.inner_text()
        # A resolution changed no REGISTER fact: the candidate still states the condition.
        still = container.registration_preparations.evaluate(made["preparation_id"])
        assert "AUTHORING_REVISIONS_UNOWNED" in {r.code for r in still.reasons}
        history = container.review_items.history(item_id)
        assert [e.event.value for e in history] == ["OPENED", "RESOLUTION_RECORDED"]
        assert (history[-1].actor, history[-1].disposition is not None) == ("operator", True)
