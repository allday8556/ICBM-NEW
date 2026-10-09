"""ADR-0029 C3 in the real UI: 통합DB's category recommendation and the editor's apply.

The served application's provider answers through the AIS-1 fakes; the catalog is the served
application's own durable snapshot, and its target policy names that catalog's taxonomy.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser

from app.container import Container
from app.stages.register.authoring import AuthoredInputs
from app.stages.register.category_catalog import CategoryCatalogStore
from app.stages.register.model import ListingShape
from app.stages.register.policy import StaticRegistrationPolicy
from app.stages.register.preparation import ListingValues
from tests.conftest import LOCAL
from tests.integration.ai.test_aic2_category_task import LEAVES, _answer
from tests.integration.ai.test_ais1_provider_profile import (
    FakeComplete,
    FakeProbe,
    _approve_all,
    _serving,
)
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    _page,
    browser,
    client,
    container,
    sources,
)
from tests.support.product_support import Collections, context
from tests.support.register_support import CID, OPERATOR, establish, ready_item, target

pytestmark = pytest.mark.integration


def test_a_category_is_recommended_in_the_db_and_applied_in_the_editor(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    tmp_path: Path,
) -> None:
    provider = container.ai_provider._provider
    provider._probe = FakeProbe(_serving(tmp_path))
    provider._complete = FakeComplete(value=_answer())
    _approve_all(container.ai_provider)
    catalog = CategoryCatalogStore(container.db, container.clock, container.audit)
    catalog.record(
        "smartstore", LEAVES, endpoint_mapping_revision="test", actor="operator", correlation_id="c"
    )
    taxonomy = catalog.current("smartstore").taxonomy_revision
    account = establish(container, container.config, "smartstore", "uid-category-ui")
    container.registration_preflight._policies = StaticRegistrationPolicy(
        (target(account, marketplace_key="smartstore", taxonomy_revision=taxonomy),)
    )
    item = ready_item(container, sources, "7002", pricing=context(marketplace_key="smartstore"))
    with container.registrations.transaction() as tx:
        created = tx.create_draft(
            "smartstore",
            account,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id=CID,
        )
        tx.add_draft_item(
            created.draft_id,
            item.item_id,
            item.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id=CID,
        )
    container.registration_preparations.create(
        created.draft_id,
        item_ids=[item.item_id],
        inputs=AuthoredInputs(category=None, listing=ListingValues(), detail=None),
        actor="operator",
    )
    headers = {"X-ICBM-Client": "pytest"}
    unit = client.get(f"/api/v1/register/units/{created.draft_id}", headers=headers).json()[0]
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        page.goto(f"{LOCAL}/#/db?product={item.group}")
        panel = page.locator("[data-role='detail-ai-category'] [data-role='ai-category']")
        page.wait_for_function(
            "() => document.querySelector('[data-action=ai-category-request]')?.dataset.ready"
            " === 'true'"
        )
        panel.locator("[data-action='ai-category-request']").click()
        name = panel.locator("[data-role='ai-category-name']")
        name.wait_for(timeout=30_000)
        assert name.inner_text() == "식품>식용유/오일>들기름"
        assert panel.locator("[data-role='ai-category-confidence']").inner_text() == "90%"
        assert writes == [("POST", f"/api/v1/products/{item.group}/enrichment")]

        page.goto(
            f"{LOCAL}/#/register-editor?draft={created.draft_id}&unit={unit['unit_ref']}"
            "&mode=register"
        )
        block = page.locator("[data-role='editor-ai-category']")
        block.locator("[data-role='ai-category-name']").wait_for(timeout=15_000)
        page.wait_for_function(
            "() => document.querySelector('[data-action=ai-category-apply]')"
            " && !document.querySelector('[data-action=ai-category-apply]').disabled"
        )
        block.locator("[data-action='ai-category-apply']").click()
        page.locator("[data-role='ai-category-applied']").wait_for(timeout=15_000)
        assert page.locator("input[name='category_id']").input_value() == "50000803"
    assert len(provider._complete.calls) == 1
