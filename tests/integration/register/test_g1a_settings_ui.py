"""The Settings target-policy surface in the real UI (Gate 1 G1-A, ADR-0015 §2).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. Proven here:
- Settings reads the registration target policy, edits the supported fields, saves them, and the
  same durable policy comes back after a reload and after an application restart;
- the save bar says what the **server** says is editable: only the target-policy surface, while
  every general setting stays read-only and its save stays inert;
- a save the server refuses shows the server's reason and writes nothing.

No provider is reached: a target policy is local configuration.
"""

import contextlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.stages.register.category_catalog import CategoryCatalogStore, ProviderCategory
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.product_support import raw
from tests.support.register_support import establish

pytestmark = pytest.mark.integration

MARKETPLACE = "smartstore"
POLICY_TAB = f"{LOCAL}/#/settings?tab=smartstore&sub=policy"
# The delivery half of the same policy revision lives in 배송 관리.
SHIPPING_TAB = f"{LOCAL}/#/settings?tab=smartstore&sub=shipping"
COMMON_TAB = f"{LOCAL}/#/settings?tab=common"
FORWARDED = ("x-icbm-client", "content-type", "accept")
TABLES = (
    "registration_target_policies",
    "registration_target_policy_revisions",
    "registration_target_policy_current",
)

VALUES = {
    "taxonomy_revision": "taxonomy-ui-1",
    "sanitizer_profile_version": "sanitizer-ui-1",
    "fee_fixed_krw": "0",
    "other_cost_rate": "0",
    "other_cost_fixed_krw": "0",
    "asset_profile": "asset-profile-ui-1",
    "min_images": "1",
    "max_images": "10",
}
# 2026-10-16: the fee rate is never typed; the seller picks a fee preset and the two parts fill
# from the server's reference table (판매수수료 3.003% + 주문관리수수료 3.63% = 6.633%).
PRESET = {"fee_store_type": "SMART_STORE", "fee_seller_grade": "GENERAL"}
REFERENCE = "smartstore-fees-20251001"
DELIVERY = {
    "base_fee_krw": "3000",
    "return_delivery_fee_krw": "3000",
    "exchange_delivery_fee_krw": "6000",
    "shipping_address_id": "200441202",
    "return_address_id": "200401837",
}


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            launched = launch_browser(playwright)
        except PlaywrightError as exc:
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({type(exc).__name__})")
        try:
            yield launched
        finally:
            launched.close()


@contextmanager
def _served(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@contextmanager
def _page(
    browser: Browser, client: TestClient, url: str, ready: str, writes: list[tuple[str, str]]
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        if request.method != "GET":
            writes.append((request.method, parts.path))
        response = client.request(
            request.method, path, content=request.post_data_buffer, headers=headers
        )
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(url)
        page.wait_for_selector(ready, timeout=15_000)
        yield page
    finally:
        page.close()


def _counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }


def _editor(account: str) -> str:
    return f".target-policy[data-account='{account}']"


def _save(page: Page, account: str, revision: int) -> None:
    page.locator(f"{_editor(account)} button[data-action='save-target-policy']").click()
    page.wait_for_selector(f"{_editor(account)} [data-policy-history='{revision}']", timeout=10_000)


def _fill_delivery(page: Page, account: str, values: dict[str, str]) -> None:
    editor = page.locator(_editor(account))
    for name, value in values.items():
        editor.locator(f"[data-policy-field='{name}']").fill(value)
    editor.locator("[data-policy-field='physical_delivery']").check()


def _pick_preset(page: Page, account: str, preset: dict[str, str]) -> None:
    editor = page.locator(_editor(account))
    editor.locator(f"[data-fee-reference='{REFERENCE}']").wait_for(timeout=10_000)
    for name, value in preset.items():
        editor.locator(f"[data-policy-field='{name}']").select_option(value)


def _fill(page: Page, account: str, values: dict[str, str]) -> None:
    editor = page.locator(_editor(account))
    for name, value in values.items():
        editor.locator(f"[data-policy-field='{name}']").fill(value)
    _pick_preset(page, account, PRESET)
    editor.locator("[data-policy-field='templates']").fill(
        "shipping=shipping-template-ui\nreturns=returns-template-ui"
    )
    for name in (
        "requires_representative",
        "duplicate_proof_required",
        "lookup_SELLER_CODE",
    ):
        editor.locator(f"[data-policy-field='{name}']").check()


def _state(page: Page, account: str) -> tuple[str | None, str | None, str]:
    editor = page.locator(_editor(account))
    chip = editor.locator("[data-policy-state]")
    return (
        chip.get_attribute("data-policy-state"),
        editor.locator("[data-policy-history]").get_attribute("data-policy-history"),
        chip.inner_text().strip(),
    )


def test_settings_saves_the_target_policy_and_it_survives_reload_and_restart(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        served: Container = client.app.state.container  # type: ignore[attr-defined]
        account = establish(served, config, MARKETPLACE, "uid-smartstore-1")
        with _page(browser, client, POLICY_TAB, _editor(account), writes) as page:
            state, history, _ = _state(page, account)
            assert (state, history) == ("none", "0")
            assert writes == []
            # The two server-owned references are shown, never authorable.
            editor = page.locator(_editor(account))
            for reference in ("category_mapping_revision", "detail_composition_revision"):
                assert editor.locator(f"[data-policy-field='{reference}']").count() == 0
                shown = editor.locator(f"[data-policy-reference='{reference}']")
                assert shown.locator("input, textarea, select").count() == 0
                assert "자동으로 기록됩니다" in shown.inner_text()
            # 2026-10-16: the two pricing labels are the server's too; the seller types only fees.
            for reference in ("fee_table_version", "pricing_policy_version"):
                assert editor.locator(f"[data-policy-field='{reference}']").count() == 0
                shown = editor.locator(f"[data-policy-reference='{reference}']")
                assert shown.locator("input, textarea, select").count() == 0
            # The delivery fields are not on this tab: they live in 배송 관리.
            assert editor.locator("[data-policy-field='base_fee_krw']").count() == 0
            _fill(page, account, VALUES)
            _save(page, account, 1)
            assert "저장됨" in _state(page, account)[2]
            chip = page.locator(f"{_editor(account)} [data-policy-state]")
            assert chip.get_attribute("data-policy-revision") == "1"
        with _page(browser, client, SHIPPING_TAB, _editor(account), writes) as page:
            editor = page.locator(_editor(account))
            assert editor.get_attribute("data-policy-section") == "delivery"
            assert editor.locator("[data-policy-field='fee_rate']").count() == 0
            _fill_delivery(page, account, DELIVERY)
            _save(page, account, 2)
            saved = _state(page, account)
            assert saved[:2] == ("saved", "2")
            assert "저장됨" in saved[2]
            revisions = f"/api/v1/settings/target-policies/{MARKETPLACE}/{account}/revisions"
            assert writes == [("POST", revisions), ("POST", revisions)]
            # A reload rebuilds the same state from the server, nothing kept in the page.
            page.reload()
            page.wait_for_selector(f"{_editor(account)} [data-policy-state='saved']")
            assert _state(page, account) == saved
            field = page.locator(f"{_editor(account)} [data-policy-field='base_fee_krw']")
            assert field.input_value() == "3000"
        stored = served.target_policies.policy(MARKETPLACE, account)
        current = stored.current
        assert current is not None and stored.inputs is not None
        # The save sent null for both; the server stamped its owner's revisions.
        assert stored.inputs.category_mapping_revision
        assert stored.inputs.detail_composition_revision
        # And the pricing labels: stamped from the saved fees, kept by the delivery-only save.
        pricing = stored.inputs.pricing_context
        assert re.fullmatch(r"fees-\d{8}-[0-9a-f]{12}", pricing.fee_table_version or "")
        assert re.fullmatch(r"pricing-\d{8}-[0-9a-f]{12}", pricing.pricing_policy_version or "")
    # A restart: a new process owns the same data directory and serves the same durable policy.
    with (
        _served(config) as client,
        _page(browser, client, SHIPPING_TAB, _editor(account), []) as page,
    ):
        page.wait_for_selector(f"{_editor(account)} [data-policy-state='saved']")
        assert _state(page, account) == saved
        restarted: Container = client.app.state.container  # type: ignore[attr-defined]
        target = restarted.registration_preflight.target_policy(MARKETPLACE, account)
        assert target is not None and target.policy_revision == current.policy_revision
        # The delivery save kept the policy tab's values whole.
        assert target.pricing_context.fee_rate == "0.06633"
        assert target.delivery_policy is not None
        assert target.delivery_policy.shipping_address_id == 200441202
        assert target.delivery_policy.return_address_id == 200401837
        assert target.delivery_policy.exchange_delivery_fee_krw == 6000
    assert _counts(config) == {
        "registration_target_policies": 1,
        "registration_target_policy_revisions": 2,
        "registration_target_policy_current": 1,
    }


def test_the_save_bar_is_server_owned_and_general_settings_stay_read_only(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client, _page(browser, client, COMMON_TAB, ".savebar", writes) as page:
        chip = page.locator(".savebar .chip")
        assert chip.get_attribute("data-save-scope") == (
            "REGISTRATION_TARGET_POLICY REGISTRATION_CATEGORY_METADATA AI_PROMPT_REGISTRY"
            " DETAIL_GUIDANCE SUPPLIER_SALES_POLICY OVERSEAS_SETTINGS"
        )
        text = chip.inner_text()
        assert "M0" not in text
        assert "회색 칸은 아직 설정할 수 없습니다" in text
        assert "등록 정책" in text and "카테고리 정보" in text
        # ADR-0039 §4: the supplier sales policy is saved from the supplier card.
        assert "공급처 판매정책 (수집관리 › 공급처 관리)" in text
        # ADR-0038 §5: the overseas settings are saved from their own card.
        assert "해외 구매대행 (공통 › 해외구매대행)" in text
        # Every general setting field is read-only, and the general save is inert.
        fields = page.locator("input[data-setting]")
        assert fields.count() > 0
        assert all(
            fields.nth(i).get_attribute("readonly") is not None for i in range(fields.count())
        )
        # There is no general save: each saveable setting has its own card button, and a card
        # with nothing saveable says so plainly.
        assert page.locator(".savebar-actions").count() == 0
        assert page.locator(".setting-card[data-pending='true'] .setting-pending").count() > 0
        assert writes == []
    assert _counts(config) == dict.fromkeys(TABLES, 0)


def test_a_refused_save_shows_the_server_reason_and_writes_nothing(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        served: Container = client.app.state.container  # type: ignore[attr-defined]
        account = establish(served, config, MARKETPLACE, "uid-smartstore-1")
        with _page(browser, client, POLICY_TAB, _editor(account), writes) as page:
            _fill(page, account, {**VALUES, "min_images": "5", "max_images": "2"})
            page.locator(f"{_editor(account)} button[data-action='save-target-policy']").click()
            toast = page.locator(".toast", has_text="입력값을 확인하세요")
            toast.wait_for(timeout=10_000)
            assert _state(page, account)[:2] == ("none", "0")
            # The typed values stay for a fix; nothing was promoted locally.
            field = page.locator(f"{_editor(account)} [data-policy-field='min_images']")
            assert field.input_value() == "5"
    assert len(writes) == 1
    assert _counts(config) == dict.fromkeys(TABLES, 0)


ADDRESS_BOOKS = "/api/v1/connect/marketplaces/smartstore/addressbooks"


def test_the_naver_address_book_becomes_named_dropdowns_and_the_choice_is_saved(
    browser: Browser, config: AppConfig
) -> None:
    # The address book is a provider read; here the page's request is answered with a fixture so
    # no provider is reached, and the save still goes through the real server.
    books = {
        "address_books": [
            {"address_book_no": 200401837, "name": "반품지", "address_type": "REFUND_OR_EXCHANGE"},
            {"address_book_no": 200441202, "name": "출고지", "address_type": "RELEASE"},
            {"address_book_no": 200583581, "name": "KM 출고반품지", "address_type": "GENERAL"},
        ]
    }
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        served: Container = client.app.state.container  # type: ignore[attr-defined]
        account = establish(served, config, MARKETPLACE, "uid-smartstore-1")
        with _page(browser, client, POLICY_TAB, _editor(account), writes) as page:
            _fill(page, account, VALUES)
            _save(page, account, 1)
        with _page(browser, client, SHIPPING_TAB, _editor(account), writes) as page:
            editor = page.locator(_editor(account))
            # Without a CONNECT session the editor's own load is refused quietly: the number
            # fields stay, with a hint instead of an error.
            hint = editor.locator("[data-address-books='NOT_LOADED']")
            hint.filter(has_text="스마트스토어 연결 후").wait_for(timeout=10_000)
            assert editor.locator("select[data-address-select]").count() == 0
            _fill_delivery(
                page, account, {**DELIVERY, "shipping_address_id": "", "return_address_id": "123"}
            )
            page.route(f"**{ADDRESS_BOOKS}", lambda route: route.fulfill(status=200, json=books))
            button = editor.locator("button[data-action='load-address-books']")
            for _ in range(2):  # a second load replaces the dropdowns, it never stacks them
                button.click()
                page.wait_for_selector(f"{_editor(account)} [data-address-books='3']")
            assert editor.locator("select[data-address-select]").count() == 2
            shipping = editor.locator("select[data-address-select='shipping_address_id']")
            returns = editor.locator("select[data-address-select='return_address_id']")
            # The empty field takes the first entry of its type; a kept number the book does not
            # list is shown as such, never silently replaced.
            assert shipping.input_value() == "200441202"
            assert returns.input_value() == "123"
            assert "주소록에 없는 번호" in returns.locator("option").first.inner_text()
            # The field's own type comes first, every entry is named.
            assert returns.locator("option").all_inner_texts()[1:] == [
                "반품지 · 반품·교환지 · 200401837",
                "출고지 · 출고지 · 200441202",
                "KM 출고반품지 · 일반 · 200583581",
            ]
            assert editor.locator("[data-policy-field='return_address_id']").is_hidden()
            returns.select_option("200583581")
            _save(page, account, 2)
        target = served.registration_preflight.target_policy(MARKETPLACE, account)
        assert target is not None and target.delivery_policy is not None
        assert target.delivery_policy.shipping_address_id == 200441202
        assert target.delivery_policy.return_address_id == 200583581
    revisions = f"/api/v1/settings/target-policies/{MARKETPLACE}/{account}/revisions"
    assert writes == [("POST", revisions), ("POST", revisions)]


def test_the_taxonomy_is_aligned_to_the_naver_catalog_only_after_the_seller_confirms(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        served: Container = client.app.state.container  # type: ignore[attr-defined]
        account = establish(served, config, MARKETPLACE, "uid-smartstore-1")
        CategoryCatalogStore(served.db, served.clock, served.audit).record(
            MARKETPLACE,
            (ProviderCategory("50000803", "비타민", "식품>건강식품>비타민", True),),
            endpoint_mapping_revision="test",
            actor="operator",
            correlation_id="align-catalog",
        )
        latest = served.category_catalog.catalog(MARKETPLACE, limit=1).taxonomy_revision
        assert latest
        with _page(browser, client, POLICY_TAB, _editor(account), writes) as page:
            editor = page.locator(_editor(account))
            state = editor.locator("[data-taxonomy-state='BEHIND']")
            state.wait_for(timeout=10_000)
            assert "다릅니다" in state.inner_text()
            editor.locator("button[data-action='align-taxonomy']").click()
            confirm = page.locator("[data-role='align-taxonomy-confirm']")
            confirm.wait_for()
            # Nothing changes before the seller confirms.
            field = editor.locator("[data-policy-field='taxonomy_revision']")
            assert field.input_value() == ""
            page.locator("button[data-action='confirm-align-taxonomy']").click()
            assert field.input_value() == latest
            editor.locator("[data-taxonomy-state='CURRENT']").wait_for()
            assert editor.locator("button[data-action='align-taxonomy']").is_disabled()
            assert writes == []
            values = {key: value for key, value in VALUES.items() if key != "taxonomy_revision"}
            _fill(page, account, values)
            _save(page, account, 1)
        target = served.registration_preflight.target_policy(MARKETPLACE, account)
        assert target is not None and target.taxonomy_revision == latest


def test_the_fee_preset_fills_the_fees_and_marks_what_the_seller_changed(
    browser: Browser, config: AppConfig
) -> None:
    writes: list[tuple[str, str]] = []
    with _served(config) as client:
        served: Container = client.app.state.container  # type: ignore[attr-defined]
        account = establish(served, config, MARKETPLACE, "uid-smartstore-1")
        with _page(browser, client, POLICY_TAB, _editor(account), writes) as page:
            editor = page.locator(_editor(account))
            # The free-typed fee rate is gone: the rate is composed from the preset.
            assert editor.locator("[data-policy-field='fee_rate']").count() == 0
            _fill(page, account, VALUES)
            sales = editor.locator("[data-policy-field='sales_fee_percent']")
            order = editor.locator("[data-policy-field='order_fee_percent']")
            assert (sales.input_value(), order.input_value()) == ("3.003", "3.63")
            total = editor.locator("[data-fee-total]")
            assert total.get_attribute("data-fee-total") == "0.06633"
            assert "판매수수료 3.003% + 주문관리수수료 3.63% = 6.633%" in total.inner_text()
            for name in ("sales_fee_percent", "order_fee_percent"):
                mark = editor.locator(f"[data-fee-source='{name}']")
                assert mark.get_attribute("data-fee-state") == "REFERENCE"
                assert mark.inner_text() == "참고값"
            # 참고값 · 기준일 · 출처, and the line telling the seller to fix a different notice.
            reference = editor.locator(f"[data-fee-reference='{REFERENCE}']").inner_text()
            assert "참고값 · 기준일 2025-10-01" in reference
            assert "네이버 공식 공지가 아닙니다" in reference
            notice = editor.locator("[data-fee-hint='notice']").inner_text()
            assert "판매자센터 공지와 다르면 직접 고쳐 주세요" in notice
            shipping = editor.locator("[data-fee-hint='shipping']").inner_text()
            assert "건당 기타 비용" in shipping and "≈ 109원" in shipping
            # Another grade fills its own reference value; a changed number is 직접 입력.
            editor.locator("[data-policy-field='fee_seller_grade']").select_option("SMALL")
            assert order.input_value() == "1.947"
            order.fill("1.9")
            mark = editor.locator("[data-fee-source='order_fee_percent']")
            assert mark.get_attribute("data-fee-state") == "MANUAL"
            assert mark.inner_text() == "직접 입력"
            assert total.get_attribute("data-fee-total") == "0.04903"
            assert writes == []
            _save(page, account, 1)
            # A reload shows the saved preset again, the changed number still marked.
            page.reload()
            page.wait_for_selector(f"{_editor(account)} [data-fee-reference='{REFERENCE}']")
            grade = editor.locator("[data-policy-field='fee_seller_grade']")
            assert grade.input_value() == "SMALL"
            assert order.input_value() == "1.9"
            assert mark.get_attribute("data-fee-state") == "MANUAL"
        stored = served.target_policies.policy(MARKETPLACE, account).inputs
        assert stored is not None and stored.fee_preset is not None
        assert stored.pricing_context.fee_rate == "0.04903"
        assert stored.fee_preset.model_dump(mode="json") == {
            "store_type": "SMART_STORE",
            "seller_grade": "SMALL",
            "reference_version": REFERENCE,
            "sales_fee_rate": "0.03003",
            "order_fee_rate": "0.019",
        }
