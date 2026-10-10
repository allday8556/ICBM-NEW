"""ADR-0033 G4 in the real UI: the product editor's 상세페이지 step, 상단 공지 and 하단 공지.

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. Each group shows the notices the server resolved for the unit, in order,
and offers 기본값 / 끄기 / 직접 작성. 직접 작성 is the settings card's own editor: the same
five-template preview and presets, all the server's. The choice is saved with the preparation
through the one preparation save, and the unit's BLOCKED reason is the server's preflight reason.
Nothing is placed and nothing reaches a marketplace.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Locator, Page

from app.container import Container
from app.stages.register.guidance import GuidanceMode
from tests.conftest import LOCAL
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    _page,
    account,
    browser,
    client,
    container,
    prep,
    sources,
)
from tests.support.product_support import Collections
from tests.support.register_support import (
    CATEGORY,
    OPERATOR,
    TAXONOMY,
    Preparation,
    draft,
    ready_item,
)

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
TIMEOUT = 15_000
TOP: dict[str, Any] = {"blocks": [{"heading": "당일발송", "lines": ["12시 이전 주문시 당일발송"]}]}
INPUTS: dict[str, Any] = {
    "category": {
        "category_id": CATEGORY,
        "mapping_revision": "mapping-test-1",
        "taxonomy_revision": TAXONOMY,
        "confirmation": "OPERATOR_CONFIRMED",
    },
    "name": {"value": "공지 편집 상품"},
    "attributes": {"brand": {"value": "브랜드"}},
    "notices": {"manufacturer": {"value": "제조사"}, "origin": {"detail_page_reference": True}},
    "detail_composition_revision": "detail-test-1",
    "detail_body": "상세 본문",
    "detail_sections": ["BODY"],
}


def _authored_unit(
    app: TestClient, served: Container, collections: Collections, account_id: str
) -> tuple[str, str, str]:
    item = ready_item(served, collections, "1234")
    draft_id = draft(served.registrations, account_id, [item])
    saved = app.post(
        "/api/v1/settings/detail-guidance/revisions",
        json={"actor": OPERATOR, "placement": "TOP", "kind": "STANDING", "template": "CLEAN"}
        | {"content": TOP},
        headers=CLIENT,
    )
    assert saved.status_code == 200, saved.text
    created = app.post(
        "/api/v1/register/preparations",
        json={
            "draft_id": draft_id,
            "item_ids": [item.item_id],
            "actor": OPERATOR,
            "inputs": INPUTS,
        },
        headers=CLIENT,
    )
    assert created.status_code == 200, created.text
    return draft_id, str(created.json()["preparation_id"]), str(saved.json()["image_sha256"])


def _group(page: Page, placement: str) -> Locator:
    group = page.locator(f"[data-role='editor-guidance-{placement}'][data-state='ready']")
    group.wait_for(timeout=TIMEOUT)
    return group


def _checked(group: Locator) -> list[str]:
    return [
        b.get_attribute("data-mode") or ""
        for b in group.locator("[data-role='dg-modes'] [data-mode]").all()
        if b.get_attribute("aria-checked") == "true"
    ]


def test_the_detail_step_shows_the_resolved_notices_and_saves_the_choice(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    draft_id, preparation_id, standing_sha = _authored_unit(client, container, sources, account)
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        page.goto(
            f"{LOCAL}/#/register-editor?draft={draft_id}&unit={preparation_id}&mode=register&step=5"
        )
        page.locator("[data-role='register-editor']").wait_for(timeout=TIMEOUT)
        top, bottom = _group(page, "top"), _group(page, "bottom")
        # The inert placeholder is gone; each group reads the server's resolution.
        assert page.get_by_text("상·하단 공지는 준비 중").count() == 0
        assert page.locator("[data-no-data='상단 공지'], [data-no-data='하단 공지']").count() == 0
        entries = top.locator("[data-role='dg-entry']")
        assert entries.count() == 1
        assert entries.first.get_attribute("data-source") == "STORE"
        assert entries.first.get_attribute("data-sha256") == standing_sha
        page.wait_for_function(
            "img => img.complete && img.naturalWidth === 860",
            arg=entries.first.locator("img").element_handle(),
            timeout=TIMEOUT,
        )
        assert bottom.locator("[data-role='dg-entry']").count() == 0
        assert bottom.locator("[data-role='dg-none']").is_visible()
        # The unit is BLOCKED by the server's own reason, shown where it applies.
        assert top.locator("[data-role='dg-unplaced']").is_visible()
        assert bottom.locator("[data-role='dg-unplaced']").count() == 0
        assert page.locator(".ready-item[data-step='detail']").get_attribute("data-ready") == "bad"
        assert _checked(top) == ["DEFAULT"] and _checked(bottom) == ["DEFAULT"]
        assert [b.inner_text().strip() for b in top.locator("[data-mode]").all()] == [
            "기본값",
            "끄기",
            "직접 작성",
        ]

        # 끄기 on top and 직접 작성 on the bottom: nothing is sent until the preparation is saved.
        top.locator("[data-mode='OFF']").click()
        assert _checked(top) == ["OFF"]
        assert "저장하면" in top.locator("[data-role='dg-mode-note']").inner_text()
        bottom.locator("[data-mode='CUSTOM']").click()
        custom = bottom.locator("[data-role='dg-custom']")
        headings = custom.locator("[data-role='dg-heading']")
        assert [h.input_value() for h in headings.all()] == ["배송 안내", "C/S 안내"]
        assert writes == []
        # A product's own notice needs its design: the save refuses locally and sends nothing.
        custom.locator("[data-role='dg-line']").first.fill("○○택배로 발송됩니다")
        custom.locator("[data-role='dg-line']").nth(1).fill("평일 10:00 ~ 17:00")
        custom.locator("[data-role='dg-preview'] [data-role='dg-template'] img").nth(4).wait_for(
            timeout=TIMEOUT
        )
        page.locator(".editor-foot button[data-action='SAVE_PREPARATION']").click()
        page.get_by_text("템플릿을 고르세요").first.wait_for(timeout=TIMEOUT)
        assert ("POST", f"/api/v1/register/preparations/{preparation_id}") not in writes
        # The presets are the settings card's: the example text replaces the edited inputs only
        # after a confirmation, and suggests its template.
        page.once("dialog", lambda dialog: dialog.accept())
        custom.locator("[data-role='dg-preset'][data-preset='DOMESTIC']").click()
        custom.locator("[data-role='dg-preview'] [data-role='dg-template'] img").nth(4).wait_for(
            timeout=TIMEOUT
        )
        assert set(writes) == {("POST", "/api/v1/settings/detail-guidance/preview")}
        custom.locator("[data-template='DOMESTIC']").click()
        assert custom.locator("[data-template='DOMESTIC']").get_attribute("aria-checked") == "true"
        filled = [h.input_value() for h in custom.locator("[data-role='dg-heading']").all()]

        page.locator(".editor-foot button[data-action='SAVE_PREPARATION']").click()
        saved = page.locator("[data-role='editor-guidance-bottom'][data-saved-mode='CUSTOM']")
        saved.wait_for(timeout=TIMEOUT)
        assert ("POST", f"/api/v1/register/preparations/{preparation_id}") in writes
        stored = container.registrations.preparation(preparation_id)
        assert stored is not None and stored.current.revision_no == 2
        choice = stored.current.listing["guidance"]
        assert choice["top"] == {"mode": GuidanceMode.OFF.value}
        assert choice["bottom"]["mode"] == "CUSTOM" and choice["bottom"]["template"] == "DOMESTIC"
        assert [b["heading"] for b in choice["bottom"]["content"]["blocks"]] == filled
        # Read again from the server: no top notice, the product's own bottom notice, and the
        # bottom now carries the BLOCKED reason instead of the top.
        top, bottom = _group(page, "top"), _group(page, "bottom")
        assert _checked(top) == ["OFF"] and _checked(bottom) == ["CUSTOM"]
        assert top.locator("[data-role='dg-entry']").count() == 0
        own = bottom.locator("[data-role='dg-entry']")
        assert own.count() == 1 and own.first.get_attribute("data-source") == "PRODUCT"
        assert own.first.get_attribute("data-sha256") == choice["bottom"]["sha256"]
        assert top.locator("[data-role='dg-unplaced']").count() == 0
        assert bottom.locator("[data-role='dg-unplaced']").is_visible()
        # The saved notice opens in the editor with its text and design.
        reopened = bottom.locator("[data-role='dg-custom']")
        assert [
            h.input_value() for h in reopened.locator("[data-role='dg-heading']").all()
        ] == filled
        assert (
            reopened.locator("[data-template='DOMESTIC']").get_attribute("aria-checked") == "true"
        )
        assert container.jobs.count(job_type_prefix="register.create") == 0


def test_a_save_from_the_register_page_keeps_the_stored_choice(
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    """The preparation read gives the choice back in the shape the save takes, so a surface that
    does not edit it sends it back unchanged (the register page's form does exactly this)."""
    _draft_id, preparation_id, _sha = _authored_unit(client, container, sources, account)
    off = {"top": {"mode": "OFF"}, "bottom": {"mode": "DEFAULT"}}
    item_ids = list(container.registrations.preparation(preparation_id).current.item_ids)  # type: ignore[union-attr]
    first = client.post(
        f"/api/v1/register/preparations/{preparation_id}",
        json={"item_ids": item_ids, "actor": OPERATOR, "inputs": INPUTS | {"guidance": off}},
        headers=CLIENT,
    )
    assert first.status_code == 200, first.text
    echoed = first.json()["inputs"]
    again = client.post(
        f"/api/v1/register/preparations/{preparation_id}",
        json={"item_ids": item_ids, "actor": OPERATOR, "inputs": echoed},
        headers=CLIENT,
    )
    assert again.status_code == 200, again.text
    stored = container.registrations.preparation(preparation_id)
    assert stored is not None and stored.current.revision_no == 3
    assert stored.current.listing["guidance"]["top"] == {"mode": "OFF"}
