"""ADR-0028 T4 in the real UI: 통합DB's tag recommendation and the editor's `AI 태그 적용`.

The served application's own profiled provider answers through the AIS-1 fakes, and its tag task
reads the platform through the T3 fake reader: nothing leaves the test, and no tag is sent.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser

from app.container import Container
from app.stages.register.authoring import AuthoredInputs
from app.stages.register.model import ListingShape
from app.stages.register.preparation import ListingValues
from tests.conftest import LOCAL
from tests.integration.ai.test_ais1_provider_profile import (
    FakeComplete,
    FakeProbe,
    _approve_all,
    _serving,
)
from tests.integration.ai.test_ait3_tag_task import ANSWER, TAGS, FakeReader
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    _page,
    browser,
    client,
    container,
    sources,
)
from tests.support.product_support import Collections, context
from tests.support.register_support import CID, OPERATOR, establish, ready_item

pytestmark = pytest.mark.integration


def test_tags_are_recommended_in_the_db_and_applied_in_the_editor(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    tmp_path: Path,
) -> None:
    from app.config import AppConfig

    config: AppConfig = container.config
    provider = container.ai_provider._provider
    provider._probe = FakeProbe(_serving(tmp_path))
    provider._complete = FakeComplete(value=json.loads(json.dumps(ANSWER)))
    _approve_all(container.ai_provider)
    reader = FakeReader()
    container.enrichment._tasks[TAGS].context._reader = reader
    account = establish(container, config, "smartstore", "uid-tags-ui")
    item = ready_item(container, sources, "7001", pricing=context(marketplace_key="smartstore"))
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
        panel = page.locator("[data-role='detail-tags'] [data-role='ai-tags']")
        page.wait_for_function(
            "() => document.querySelector('[data-action=ai-tags-request]')?.dataset.ready"
            " === 'true'"
        )
        panel.locator("[data-action='ai-tags-request']").click()
        tags = panel.locator("[data-role='ai-tags-list']")
        tags.wait_for(timeout=30_000)
        assert "#들기름" in tags.inner_text() and "#국산들기름" in tags.inner_text()
        assert panel.locator("[data-role='ai-tags-review']").is_visible()
        assert "제한 태그" in panel.locator("[data-role='ai-tags-removed']").inner_text()
        assert writes == [("POST", f"/api/v1/products/{item.group}/enrichment")]

        page.goto(
            f"{LOCAL}/#/register-editor?draft={created.draft_id}&unit={unit['unit_ref']}"
            "&mode=register"
        )
        block = page.locator("[data-role='editor-tags']")
        block.locator("[data-role='ai-tags-list']").wait_for(timeout=15_000)
        page.wait_for_function(
            "() => document.querySelector('[data-action=ai-tags-apply]')"
            " && !document.querySelector('[data-action=ai-tags-apply]').disabled"
        )
        block.locator("[data-action='ai-tags-apply']").click()
        page.locator("[data-role='ai-tags-applied']").wait_for(timeout=15_000)
        shown = page.locator("[data-role='editor-tags'] .tags").first.inner_text()
        assert "#들기름" in shown and "#국산들기름" in shown
        preparation_id = unit["authored"]["preparation_id"]
        assert writes[-1] == (
            "POST",
            f"/api/v1/register/preparations/{preparation_id}/apply-enrichment",
        )
    # One platform read per keyword and one AI call: the editor read the db's result.
    assert len(provider._complete.calls) == 1
    assert reader.recommended == ["생들기름", "예시 브랜드"]
