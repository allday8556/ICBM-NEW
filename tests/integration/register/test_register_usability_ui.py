"""The registration screens a non-developer seller can use (2026-10-15 usability check).

Through the real application on a migrated database, in the installed browser, with a durable
G1-A policy and a reviewed G1-B SmartStore category (ETC notice):

- the category is found by its Korean name in the server's own leaf catalog, shown with its whole
  path, and the leaf id is what the form keeps;
- a category without reviewed metadata is a 409 the screen words in Korean, never a 500 and never
  the server's English message;
- every notice field is labelled with the provider schema's own Korean name, the key staying only
  an attribute; a collected value with an exact field is filled in, marked 수집값, and saved as the
  source's fact while it is unchanged;
- 준비 내용 만들기 with an empty required field moves to its step, focuses it and names every
  missing field in Korean, sending nothing;
- the pending list and the editor name a unit by its collected product name before it is authored.

No provider is contacted: execution stays DRY_RUN.
"""

import contextlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser

from app.config import AppConfig
from app.container import Container
from app.stages.register.category_catalog import CategoryCatalogStore, ProviderCategory
from integrations.marketplaces.smartstore.notice_schema import load_notice_schema
from tests.conftest import LOCAL
from tests.integration.register.test_authoring_unowned_revisions import (  # noqa: F401 - fixture
    draft,
)
from tests.integration.register.test_authoring_unowned_revisions_ui import (  # noqa: F401
    _page,
    api,
    browser,
    container,
)
from tests.support.gate1_support import (
    CATEGORY,
    CLIENT,
    ETC_NOTICE_KEYS,
    MARKET,
    OPERATOR,
    TAXONOMY,
    policy_inputs,
    record_reviewed_metadata,
)
from tests.support.product_support import raw
from tests.support.register_support import choose_category

pytestmark = pytest.mark.integration

UNPREPARED = "50002608"
METADATA_MISSING_COPY = (
    "이 카테고리는 아직 등록 정보(필수 항목·고시)가 준비되지 않았습니다. "
    "설정 › 상품/카테고리에서 준비하거나 다른 카테고리를 선택하세요."
)
# The collected product of the ``draft`` fixture (tests.support.collect_support.base_fields).
COLLECTED_NAME = "생들기름 350ml"
COLLECTED_BRAND = "예시 브랜드"
COLLECTED_ORIGIN = "국산"


def _catalog(served: Container) -> None:
    CategoryCatalogStore(served.db, served.clock, served.audit).record(
        MARKET,
        (
            ProviderCategory(CATEGORY, "비타민", "식품>건강식품>비타민", True),
            ProviderCategory(UNPREPARED, "루테인", "식품>건강식품>영양제>루테인", True),
        ),
        endpoint_mapping_revision="test",
        actor="operator",
        correlation_id="usability-catalog",
    )


def _editor(client: TestClient, draft_id: str) -> str:
    unit = client.get(f"/api/v1/register/units/{draft_id}", headers=CLIENT).json()[0]
    return f"{LOCAL}/#/register-editor?draft={draft_id}&unit={unit['unit_ref']}&mode=register"


def test_the_category_is_found_by_name_and_its_notice_reads_in_korean(
    browser: Browser,  # noqa: F811
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, _ = draft
    _catalog(container)
    etc = load_notice_schema().types["ETC"]
    calls: list[tuple[str, str, int]] = []
    with _page(browser, api, calls) as page:
        unit = page.locator(f".register-unit[data-draft='{draft_id}']")
        search = unit.locator("[data-role='category-search']")
        assert search.get_attribute("placeholder") == "카테고리 검색 (예: 루테인)"
        # No bare number field is offered: the leaf id is the picker's own hidden value.
        assert unit.locator("input[name='category_id']").get_attribute("type") == "hidden"
        search.fill("비타")
        option = unit.locator(f"[data-role='category-results'] li[data-category='{CATEGORY}']")
        option.wait_for(timeout=10_000)
        assert "식품 > 건강식품 > 비타민" in option.inner_text()
        assert unit.locator("[data-role='category-results'] li").count() == 1
        option.click()
        current = unit.locator(f"[data-role='category-current'][data-category='{CATEGORY}']")
        current.wait_for()
        assert (
            current.locator("[data-role='category-path']").inner_text()
            == "식품 > 건강식품 > 비타민"
        )
        assert unit.locator("input[name='category_id']").input_value() == CATEGORY
        # The reviewed requirements load; each notice field carries the schema's Korean name.
        unit.locator("input[name='notice.itemName']").wait_for(timeout=10_000)
        fields = unit.locator(".register-authoring-fields")
        for key in ETC_NOTICE_KEYS:
            caption = fields.locator(f"span[data-field-key='{key}']")
            label = etc.field(key)
            assert label is not None
            assert caption.inner_text().startswith(label.label), key
            assert caption.get_attribute("title") == key
        shown = fields.inner_text()
        assert not [key for key in ETC_NOTICE_KEYS if key in shown]
        assert etc.name in shown
        # The collected brand fills the attribute of that exact name, marked 수집값; the
        # manufacturer the collection did not state is left empty.
        mark = unit.locator("label[data-field-key='brand'] [data-prefill='collected']")
        mark.wait_for(timeout=10_000)
        assert mark.inner_text() == "수집값"
        assert unit.locator("input[name='attribute.brand']").input_value() == COLLECTED_BRAND
        assert unit.locator("input[name='notice.manufacturer']").input_value() == ""

        # A category without reviewed requirements is a 409 the screen words in Korean.
        search.fill("루테인")
        other = unit.locator(f"[data-role='category-results'] li[data-category='{UNPREPARED}']")
        other.wait_for(timeout=10_000)
        other.click()
        problem = unit.locator("[data-role='category-problem']")
        problem.wait_for(timeout=10_000)
        assert problem.locator("[data-role='category-problem-text']").inner_text() == (
            METADATA_MISSING_COPY
        )
        # 2026-10-16: the one thing to do about it is offered right there.
        prepare = problem.locator("button[data-action='PREPARE_CATEGORY_METADATA']")
        assert prepare.inner_text() == "이 카테고리 등록 정보 준비"
        assert unit.locator("input[name='notice.itemName']").count() == 0
        assert "reviewed metadata" not in page.locator("body").inner_text()
    missing = f"/api/v1/register/drafts/{draft_id}/authoring-metadata/{UNPREPARED}"
    assert ("GET", missing, 409) in calls
    assert not any(status >= 500 for _, _, status in calls)
    assert not any(method != "GET" for method, _, _ in calls)


def test_a_collected_value_is_prefilled_and_saved_as_the_sources_fact(
    browser: Browser,  # noqa: F811
    api: TestClient,  # noqa: F811
    config: AppConfig,
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, _ = draft
    current = api.get(
        f"/api/v1/settings/category-metadata/{MARKET}/{TAXONOMY}/{CATEGORY}", headers=CLIENT
    ).json()["current"]["metadata_revision"]
    # 주방용품: its ``producer`` is the provider's 제조국, which the collected 원산지 states.
    record_reviewed_metadata(api, notice_type="KITCHEN_UTENSILS", expected=current)
    producer = load_notice_schema().types["KITCHEN_UTENSILS"].field("producer")
    assert producer is not None and producer.label == "제조국"
    calls: list[tuple[str, str, int]] = []
    with _page(browser, api, calls) as page:
        unit = page.locator(f".register-unit[data-draft='{draft_id}']")
        choose_category(unit, CATEGORY)
        mark = unit.locator("label[data-field-key='producer'] [data-prefill='collected']")
        mark.wait_for(timeout=10_000)
        assert unit.locator("input[name='notice.producer']").input_value() == COLLECTED_ORIGIN
        # The brand is changed by the operator: it is the operator's own, and no longer marked.
        unit.locator("input[name='attribute.brand']").fill("직접 쓴 브랜드")
        assert unit.locator("label[data-field-key='brand'] [data-prefill]").count() == 0
        unit.locator("input[name='name']").fill("합성 상품")
        for field in unit.locator(".register-authoring-fields input[required]").all():
            if not field.input_value():
                field.fill("합성 값")
        unit.locator("textarea[name='detail_body']").fill("상세 본문")
        unit.locator("button[data-action='SAVE_PREPARATION']").click()
        page.wait_for_function(
            "() => document.querySelector('.register-authoring')?.dataset.preparation !== ''"
        )
    assert ("POST", "/api/v1/register/preparations", 200) in calls
    with contextlib.closing(raw(config)) as connection:
        (listing_json,) = connection.execute(
            "SELECT listing_json FROM registration_preparation_revisions"
        ).fetchone()
    listing = json.loads(listing_json)
    # Saved unchanged, the collected value is the source's fact, never the operator's.
    assert listing["notices"]["producer"]["value"] == COLLECTED_ORIGIN
    assert listing["notices"]["producer"]["provenance"] == "SOURCE_FACT"
    assert listing["attributes"]["brand"]["value"] == "직접 쓴 브랜드"
    assert listing["attributes"]["brand"]["provenance"] == "OPERATOR_CONFIRMED"


def test_an_empty_required_field_is_shown_focused_and_named_and_nothing_is_sent(
    browser: Browser,  # noqa: F811
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, _ = draft
    metadata = api.get(
        f"/api/v1/register/drafts/{draft_id}/authoring-metadata/{CATEGORY}", headers=CLIENT
    ).json()
    # What the operator must type: required and not left to the provider (the required brand
    # attribute is filled from the collection).
    must = [
        field
        for field in metadata["notice_fields"]
        if field["required"] and not field["omitted_default"]
    ]
    assert must and all(field["label"] for field in must)
    calls: list[tuple[str, str, int]] = []
    with _page(browser, api, calls) as page:
        page.goto(_editor(api, draft_id))
        editor = page.locator("[data-role='register-editor']")
        editor.wait_for(timeout=15_000)
        # Before it is authored, the editor is named by the collected product.
        page.wait_for_function(
            "(name) => document.querySelector('[data-role=editor-title]')?.textContent === name",
            arg=COLLECTED_NAME,
        )
        choose_category(page, CATEGORY)
        page.locator("input[name='notice.itemName']").wait_for(state="attached", timeout=10_000)
        page.locator("label[data-field-key='brand'] [data-prefill]").wait_for(
            state="attached", timeout=10_000
        )
        page.fill("input[name='name']", "합성 상품")
        page.locator(".editor-foot button[data-action='SAVE_PREPARATION']").click()
        note = page.locator("[data-role='missing-required']")
        note.wait_for(timeout=10_000)
        # The step holding the first empty field is shown, and that field has the focus.
        assert page.locator(".editor-pane:not([hidden])").get_attribute("data-pane") == "4"
        first = page.evaluate("() => document.activeElement?.name")
        assert first == f"notice.{must[0]['key']}"
        assert note.locator("li").all_inner_texts() == [field["label"] for field in must]
        assert note.locator("b").inner_text() == f"필수 항목 {len(must)}개가 비어 있습니다"
        assert page.locator("[aria-invalid='true']").count() == len(must)
        assert not [call for call in calls if call[0] != "GET"]
        # Filled in, the same save is the one preparation write.
        for field in must:
            page.fill(f"input[name='notice.{field['key']}']", "합성 값")
        page.locator(".editor-foot button[data-action='SAVE_PREPARATION']").click()
        page.wait_for_function(
            "() => document.querySelector('.register-authoring')?.dataset.preparation"
        )
    assert [call for call in calls if call[0] != "GET"] == [
        ("POST", "/api/v1/register/preparations", 200)
    ]


def test_the_pending_list_names_a_unit_by_its_collected_product(
    browser: Browser,  # noqa: F811
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    calls: list[tuple[str, str, int]] = []
    with _page(browser, api, calls) as page:
        name = page.locator(
            "[data-role='register-queue'] tr[data-queue-unit] [data-role='unit-name']"
        )
        page.wait_for_function(
            "() => document.querySelector(\"[data-role='register-queue'] [data-role='unit-name']\")"
            "?.dataset.nameSource === 'collected'"
        )
        assert name.inner_text() == COLLECTED_NAME
        assert "상품 1개" not in page.locator("[data-role='register-queue']").inner_text()
    assert not any(method != "GET" for method, _, _ in calls)


def test_a_category_without_requirements_is_prepared_from_the_picker(
    browser: Browser,  # noqa: F811
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    """2026-10-16: 「이 카테고리 등록 정보 준비」 opens the category's form right from the picker;
    the seller chooses the notice type by name, ticks 확인 완료 themselves, and the category's
    fields appear without leaving the screen."""
    draft_id, _ = draft
    _catalog(container)
    latest = container.category_catalog.catalog(MARKET, limit=1).taxonomy_revision
    assert latest
    # The policy is aligned to the synchronized catalog (설정's 「최신 카테고리 체계로 맞추기」):
    # a new category is recorded under the current catalog's taxonomy only.
    view = api.get(f"/api/v1/settings/target-policies/{MARKET}", headers=CLIENT).json()
    policy = view["accounts"][0]
    account = policy["marketplace_account_id"]
    aligned = api.post(
        f"/api/v1/settings/target-policies/{MARKET}/{account}/revisions",
        json={
            "actor": OPERATOR,
            "expected_current_revision": policy["current"]["policy_revision"],
            "inputs": {**policy_inputs(account), "taxonomy_revision": latest},
        },
        headers=CLIENT,
    )
    assert aligned.status_code == 200, aligned.text
    etc = load_notice_schema().types["ETC"]
    calls: list[tuple[str, str, int]] = []
    with _page(browser, api, calls) as page:
        unit = page.locator(f".register-unit[data-draft='{draft_id}']")
        search = unit.locator("[data-role='category-search']")
        search.fill("루테인")
        other = unit.locator(f"[data-role='category-results'] li[data-category='{UNPREPARED}']")
        other.wait_for(timeout=10_000)
        other.click()
        prepare = unit.locator("button[data-action='PREPARE_CATEGORY_METADATA']")
        prepare.wait_for(timeout=10_000)
        prepare.click()
        modal = page.locator(f"[data-metadata-modal='{latest}/{UNPREPARED}']")
        modal.wait_for(timeout=10_000)
        assert "식품 > 건강식품 > 영양제 > 루테인" in modal.inner_text()

        def field(name: str) -> Any:
            return modal.locator(f"[data-meta-field='{name}']")

        # A leaf of the current catalog starts as a registrable leaf without options; the review
        # tick is never set for the seller, and the name limit is left for them with a hint.
        assert field("leaf").is_checked() and field("registrable").is_checked()
        assert not field("options_supported").is_checked()
        assert (field("max_options").input_value(), field("max_dimensions").input_value()) == (
            "1",
            "1",
        )
        assert field("name_max_length").input_value() == ""
        assert modal.locator("[data-meta-hint='name_max_length']").count() == 1
        assert not field("reviewed").is_checked()
        # The notice type is chosen by its Korean name; the value is the stored enum.
        field("notice_type").select_option(label=etc.name)
        assert field("notice_type").input_value() == "ETC"
        field("name_max_length").fill("100")
        field("evidence_reference").fill("seller-center/lutein/2026-10-16")
        field("reviewed").check()
        modal.locator("button[data-action='save-category-metadata']").click()
        # The category is read again: its fields appear in the same form.
        unit.locator("input[name='notice.itemName']").wait_for(timeout=10_000)
        assert page.locator("[data-metadata-modal]").count() == 0
        assert unit.locator("[data-role='category-problem']").is_hidden()
    recorded = f"/api/v1/settings/category-metadata/{MARKET}/{latest}/{UNPREPARED}/revisions"
    assert [call for call in calls if call[0] != "GET"] == [("POST", recorded, 200)]
    stored = container.registration_preflight.category_metadata(MARKET, latest, UNPREPARED)
    assert stored is not None and stored.reviewed is True
    assert stored.notice is not None and stored.notice.notice_type == "ETC"
    assert (stored.options.options_supported, stored.options.max_options) == (False, 1)
