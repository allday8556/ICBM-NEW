"""ADR-0027 AIS-2 in the real UI: 통합DB ✨ AI 추천 and the editor's recommendation and apply.

The served application's own profiled provider answers through the AIS-1 fakes (a fake serving
process and a fake completion), approved through its own profile service: nothing leaves the
test. Without an approved profile the button stays inert with the capability's reason.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser

from app.container import Container
from app.stages.products.tasks import PRODUCT_NAME_TASK
from app.stages.register.authoring import AuthoredInputs
from app.stages.register.preparation import ListingValues
from tests.conftest import LOCAL
from tests.integration.ai.test_ais1_provider_profile import (
    FakeComplete,
    FakeProbe,
    _approve_all,
    _serving,
)
from tests.integration.ai.test_ais2_product_name import ANSWER
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    _page,
    account,
    browser,
    client,
    container,
    sources,
)
from tests.support.product_support import Collections
from tests.support.register_support import draft, ready_item

pytestmark = pytest.mark.integration

RECOMMENDED = ANSWER["product_name"]["recommended"]


def test_the_db_button_is_inert_with_its_reason_without_an_approved_profile(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
) -> None:
    item = ready_item(container, sources, "2468")
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        page.goto(f"{LOCAL}/#/db?product={item.group}")
        button = page.locator("[data-role='ai-name'] [data-action='ai-name-request']")
        page.wait_for_function(
            "() => document.querySelector('[data-action=ai-name-request]')?.dataset.ready"
        )
        assert button.get_attribute("data-ready") == "false"
        assert button.get_attribute("aria-disabled") == "true"
        assert "AI 공급자가 설정되지 않았습니다" in (button.get_attribute("data-inert") or "")
        button.click(force=True)
        assert page.locator("[data-role='ai-name-none']").is_visible()
    assert writes == []


def test_a_recommendation_is_asked_in_the_db_and_applied_in_the_editor(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    provider = container.ai_provider._provider
    provider._probe = FakeProbe(_serving(tmp_path))
    provider._complete = FakeComplete(value=ANSWER)
    _approve_all(container.ai_provider)
    item = ready_item(container, sources, "1357")
    draft_id = draft(container.registrations, account, [item])
    container.registration_preparations.create(
        draft_id,
        item_ids=[item.item_id],
        inputs=AuthoredInputs(category=None, listing=ListingValues(), detail=None),
        actor="operator",
    )
    headers = {"X-ICBM-Client": "pytest"}
    unit = client.get(f"/api/v1/register/units/{draft_id}", headers=headers).json()[0]
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        page.goto(f"{LOCAL}/#/db?product={item.group}")
        panel = page.locator("[data-role='ai-name']")
        page.wait_for_function(
            "() => document.querySelector('[data-action=ai-name-request]')?.dataset.ready"
            " === 'true'"
        )
        panel.locator("[data-action='ai-name-request']").click()
        recommended = panel.locator("[data-role='ai-name-recommended']")
        recommended.wait_for(timeout=30_000)
        assert recommended.inner_text() == RECOMMENDED
        assert "신뢰도 82%" in panel.inner_text()
        assert panel.locator("[data-role='ai-name-fresh']").is_visible()
        assert "gpt-5.6-sol-2026" in panel.locator("[data-role='ai-name-model']").inner_text()
        assert writes == [("POST", f"/api/v1/products/{item.group}/enrichment")]

        page.goto(
            f"{LOCAL}/#/register-editor?draft={draft_id}&unit={unit['unit_ref']}&mode=register"
        )
        block = page.locator("[data-role='editor-ai-name']")
        block.locator("[data-role='ai-name-recommended']").wait_for(timeout=15_000)
        apply = block.locator("[data-action='ai-name-apply']")
        page.wait_for_function(
            "() => !document.querySelector('[data-action=ai-name-apply]')?.disabled"
        )
        apply.click()
        block = page.locator("[data-role='editor-ai-name']")
        block.locator("[data-role='ai-name-applied']").wait_for(timeout=15_000)
        assert page.locator("input[name='name']").input_value() == RECOMMENDED
        preparation_id = unit["authored"]["preparation_id"]
        assert writes[-1] == (
            "POST",
            f"/api/v1/register/preparations/{preparation_id}/apply-enrichment",
        )
        # One call: the editor read the db's result, it did not ask again.
        assert len(provider._complete.calls) == 1
    assert PRODUCT_NAME_TASK.task_key == "TASK_PRODUCT_RECOMMEND_BUNDLE_V1"
