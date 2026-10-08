"""The registration editor in the real UI (2026-10-08 owner UX, phase 1).

등록관리 keeps its queue; a unit's row opens the 상품등록 / 상품수정 choice, and the chosen
work opens the editor in its own tab. Phase 1 adds no contract: the editor reads the same unit
read, saves through the same preparation route, and colours its 등록 준비 strip from the
server's preflight only. What is proven here is that the row and the choice send nothing, that
the editor's six steps place the existing pieces, that a slot without an owner stays inert, and
that the save from the editor is the one preparation write the register page already makes.

No provider is reached: execution stays DRY_RUN and every seam refuses locally.
"""

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser

from app.container import Container
from app.platform.core.errors import AppError
from app.stages.connect.marketplace.capability import RemoteOutcome
from tests.conftest import LOCAL
from tests.integration.register.test_m5_register_execution import (
    FakeSender,
    context,
    execution,
    prepare,
)
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
from tests.support.register_support import CATEGORY, Preparation, draft, ready_item

pytestmark = pytest.mark.integration

STEPS = ["basic", "images", "price", "notice", "detail", "etc"]


def test_a_queue_row_opens_the_editor_and_the_editor_saves_through_the_preparation_route(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    item = ready_item(container, sources, "1234")
    draft_id = draft(container.registrations, account, [item])
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        # The new tab is the browser's; here its address is kept so this page can follow it.
        page.evaluate("() => { window.open = (url) => { window.__opened = url; return null; }; }")
        row = page.locator("[data-role='register-queue'] tr[data-queue-unit]").first
        row.locator("td").first.click()
        choice = page.locator("[data-role='unit-choice']")
        choice.wait_for()
        # 상품수정 needs a registration the server confirmed; this unit has none.
        assert choice.locator("[data-choice='register']").is_enabled()
        assert choice.locator("[data-choice='edit']").is_disabled()
        choice.locator("[data-choice='register']").click()
        opened = page.evaluate("() => window.__opened")
        assert opened.startswith("/#/register-editor?")
        assert f"draft={draft_id}" in opened and "mode=register" in opened
        assert writes == []

        page.goto(f"{LOCAL}{opened}")
        editor = page.locator("[data-role='register-editor']")
        editor.wait_for(timeout=15_000)
        assert editor.get_attribute("data-draft") == draft_id
        # The application chrome is not drawn around the editor.
        assert page.evaluate("() => document.body.classList.contains('bare-page')")
        assert [s.get_attribute("data-step") for s in page.locator(".stepper .step").all()] == STEPS
        strip = page.locator("[data-role='ready-strip'] .ready-item")
        assert [s.get_attribute("data-step") for s in strip.all()] == STEPS
        # Nothing was evaluated yet, so the strip judges nothing.
        assert {s.get_attribute("data-ready") for s in strip.all()} == {"none"}
        # Only the current step is shown; a strip item moves to its step.
        assert page.locator(".editor-pane:not([hidden])").get_attribute("data-pane") == "1"
        strip.nth(3).click()
        assert page.locator(".editor-pane:not([hidden])").get_attribute("data-pane") == "4"
        # A slot without an owner keeps its place and is inert.
        bulk = page.locator("[data-role='image-bulk'] .btn")
        assert bulk.count() == 2
        assert all(b.get_attribute("aria-disabled") == "true" for b in bulk.all())
        # An AI control is not rendered at all until a server owner exists (Issue #127).
        assert page.locator("[data-role='register-editor'] .btn.ai").count() == 0
        text = editor.text_content() or ""
        assert "자동번역" not in text and "배경 이미지 제거" not in text
        assert writes == []

        # The operator authors the unit across the steps; the save is the register page's own.
        page.locator(".stepper .step[data-step='basic']").click()
        page.fill("input[name='category_id']", CATEGORY)
        page.fill("input[name='name']", "편집기가 저장한 상품명")
        page.locator(".stepper .step[data-step='notice']").click()
        page.fill("input[name='attribute.brand']", "브랜드")
        page.fill("input[name='notice.manufacturer']", "제조사")
        page.locator("input[data-detail-reference='notice'][data-field-key='origin']").check()
        page.locator(".stepper .step[data-step='detail']").click()
        page.fill("textarea[name='detail_body']", "상세 본문")
        page.locator(".editor-foot button[data-action='SAVE_PREPARATION']").click()
        page.wait_for_function(
            "() => document.querySelector('.register-authoring')?.dataset.preparation"
        )
        assert writes == [("POST", "/api/v1/register/preparations")]
        stored = container.registrations.preparations_of_draft(draft_id)
        assert len(stored) == 1 and stored[0].current.revision_no == 1
        # Authored, the unit is named by its preparation; the editor followed it by its Items.
        assert editor.get_attribute("data-unit") == stored[0].preparation_id
        assert f"unit={stored[0].preparation_id}" in page.url
        assert stored[0].current.listing["name"]["value"] == "편집기가 저장한 상품명"
        assert "상세 본문" in str(stored[0].current.detail)
        # The editor came back on the step it saved from.
        assert page.locator(".editor-pane:not([hidden])").get_attribute("data-pane") == "5"
        assert container.jobs.count(job_type_prefix="register.create") == 0


def test_the_editor_without_a_unit_says_so_and_reads_nothing_else(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        page.goto(f"{LOCAL}/#/register-editor")
        page.get_by_text("편집할 상품이 없습니다").wait_for(timeout=15_000)
        assert page.locator("[data-role='register-editor']").count() == 0
        assert writes == []


def test_a_unit_with_an_intent_opens_read_only_and_the_edit_mode_sends_nothing(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    """상품수정 is read-only in phase 1. A unit with a Snapshot and an Intent is never authored
    in the editor, the edit banner says so, and only the server's own actions stay offered."""
    ready = prepare(container, sources, container.registrations, account, prep)
    run = execution(
        container, prep, sender=FakeSender(outcome=RemoteOutcome.UNKNOWN, product_id=None)
    )
    with pytest.raises(AppError):
        run.service.run(context(ready))
    units = client.get(f"/api/v1/register/units/{ready.draft_id}").json()
    assert len(units) == 1 and units[0]["intent"] is not None
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        page.goto(
            f"{LOCAL}/#/register-editor?draft={ready.draft_id}&unit={units[0]['unit_ref']}&mode=edit"
        )
        editor = page.locator("[data-role='register-editor']")
        editor.wait_for(timeout=15_000)
        assert editor.get_attribute("data-mode") == "edit"
        assert page.locator("[data-role='edit-banner']").is_visible()
        assert page.locator("[data-role='editor-mode']").inner_text().strip() == "상품수정"
        # No authoring form and no save: the unit is frozen and sent.
        assert page.locator("form.register-authoring").count() == 0
        assert page.locator("button[data-action='SAVE_PREPARATION']").count() == 0
        # The step colours still come from the server's preflight only.
        strip = page.locator("[data-role='ready-strip'] .ready-item")
        assert [s.get_attribute("data-step") for s in strip.all()] == STEPS
        # The server's own action verdicts are what the editor offers.
        assert page.locator("button[data-action='CREATE_ENQUEUE']").first.is_disabled()
        assert writes == []
