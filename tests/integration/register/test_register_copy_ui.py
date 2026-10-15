"""The registration screens in a seller's words (2026-10-15 jargon sweep).

The owner's directive: strip every technical and design term that gets in the way of real use;
keep only what is needed for use. Through the real application in the installed browser:

- 등록관리 and the unit workspace show no internal term (snapshot, brake, canary, projection,
  M4, grant, "단위") and no server code as text — codes stay attributes;
- the real-registration safety section says what the seller does and what the administrator does;
- the preview names every request field, detail section and gap in Korean, never the request path,
  the section enum or the adapter's English sentence;
- a reason the copy table names only without its M4 layer prefix is still worded;
- the editor, the image chooser and the 판매 상태 panel word what they used to show as codes.

No provider is contacted: execution stays DRY_RUN. Where a test needs an answer the DRY_RUN
application cannot give, only that one answer is replaced in the browser.
"""

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser

from app.container import Container
from tests.conftest import LOCAL
from tests.integration.register.test_m5_register_execution import prepare
from tests.integration.register.test_m5_register_ui import (  # noqa: F401 - fixtures
    account,
    browser,
    client,
    container,
    prep,
    sources,
)
from tests.integration.register.test_register_flow_ui import _page
from tests.support.product_support import Collections
from tests.support.register_support import Preparation

pytestmark = pytest.mark.integration

REGISTER = f"{LOCAL}/#/register"
# Internal terms and codes a seller never reads on these screens.
JARGON = (
    "스냅샷",
    "브레이크",
    "카나리",
    "투영",
    "보호 쓰기",
    "실행 권한",
    "단위 동작",
    "미분류",
    "분류 오류",
    "M4",
    "REGISTER_",
    "LIVE_",
    "SMARTSTORE_",
    "QA ",
    "yyyy",
)


def _no_jargon(text: str) -> None:
    for term in JARGON:
        assert term not in text, term


def test_the_register_screen_reads_in_a_sellers_words(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    calls: list[tuple[str, str]] = []
    with _page(browser, client, calls) as page:
        page.goto(REGISTER)
        page.locator(".register-canary").wait_for(timeout=15_000)
        page.locator("[data-role='live-panel']").wait_for(timeout=15_000)
        unit = page.locator(f".register-unit[data-draft='{ready.draft_id}']")
        unit.wait_for(timeout=15_000)
        # The unit's category by its name once the catalog answers (or its number, never bare).
        page.wait_for_function(
            "() => ![...document.querySelectorAll('[data-role=category-name]')]"
            ".some((n) => n.textContent.includes('확인 중'))"
        )
        safety = page.locator("[data-section='safety']")
        # The section explains itself: who does what before anything is really sent.
        note = safety.locator("[data-role='safety-note']").inner_text()
        assert "설정 › API 관리" in note and "관리자" in note
        canary = safety.locator(".register-canary").inner_text()
        assert "실제 등록 전 점검" in canary and "아직 보낼 수 없음" in canary
        assert "연습 (마켓에 보내지 않음)" in canary
        live = safety.locator("[data-role='live-panel']").inner_text()
        assert "실제 등록 잠금" in live
        # The workspace: the preparation, the price action and the send state in plain words.
        text = unit.inner_text()
        assert "등록 요청됨" in text
        assert "마켓 전송" in text
        for shown in (
            text,
            safety.inner_text(),
            page.locator("[data-role='register-queue']").inner_text(),
        ):
            _no_jargon(shown)
        # Codes stay attributes: the requirement rows keep theirs.
        assert safety.locator("li[data-requirement='SINGLE_UNIT']").count() == 1
    assert all(method == "GET" for method, _ in calls)


PREVIEW = {
    "preview_version": "register-snapshot-preview/v1",
    "registration_snapshot_id": "snap",
    "payload_hash": "h",
    "builder_version": None,
    "name": "생들기름 350ml",
    "sale_prices_krw": [12000],
    "category_id": "50000803",
    "listing_identity": None,
    "projected": True,
    "refusal_code": None,
    "encoding_version": "v1",
    "sendable": False,
    "gaps": [
        "originProduct.detailAttribute.productInfoProvidedNotice: required on registration, but"
        " the reviewed notice type has no documented request child",
        "optionCombinations[].price: whether an option combination price is absolute is not"
        " captured",
    ],
    "document": [
        {"path": "originProduct.name", "value": "생들기름 350ml", "redacted": False},
        {"path": "originProduct.salePrice", "value": "12000", "redacted": False},
        {"path": "originProduct.statusType", "value": "SALE", "redacted": False},
        {
            "path": "originProduct.detailAttribute.minorPurchasable",
            "value": "true",
            "redacted": False,
        },
        {"path": "originProduct.images.representativeImage.url", "value": None, "redacted": True},
        {"path": "originProduct.someNewField", "value": "x", "redacted": False},
    ],
    "images": [],
    "detail": {
        "builder": "PLAN",
        "sections": ["TOP_GUIDANCE", "DETAIL_IMAGES", "BODY", "BOTTOM_GUIDANCE"],
        "renderer": "detail-renderer/v2",
        "image_slots": [],
        "paragraphs": [],
        "guidance": {"top": [], "bottom": []},
    },
    "not_sent": [
        {"path": "tags[0]", "value": "들기름"},
        {"path": "attributes.brand", "value": "DETAIL_PAGE_REFERENCE"},
    ],
}


def test_the_preview_names_fields_sections_and_gaps_in_korean(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    refused = {**PREVIEW, "projected": False, "refusal_code": "REGISTER_PREVIEW_PROJECTION_REFUSED"}
    answers = iter((PREVIEW, refused))

    def override(method: str, path: str, _body: bytes) -> tuple[int, object] | None:
        if method == "GET" and path.endswith("/preview"):
            return 200, next(answers)
        return None

    calls: list[tuple[str, str]] = []
    with _page(browser, client, calls, override) as page:
        page.goto(REGISTER)
        unit = page.locator(f".register-unit[data-draft='{ready.draft_id}']")
        unit.locator("button[data-action='PREVIEW_SNAPSHOT']").click(timeout=15_000)
        view = unit.locator(".register-preview-view")
        view.wait_for(timeout=10_000)
        text = view.inner_text()
        # Each gap is what it means; the adapter's sentence is only an attribute.
        assert "상품정보고시 유형은 아직 마켓에 보낼 수 없습니다" in text
        assert "옵션별 가격을 보내는 방식" in text
        assert "required on registration" not in text
        gap = view.locator("[data-gap='true']").first
        assert "required on registration" in (gap.get_attribute("data-gap-detail") or "")
        # The detail page's sections in Korean.
        assert "상단 공지 → 상세 이미지 → 본문 → 하단 공지" in text
        assert "TOP_GUIDANCE" not in text
        # Each request field by its name, its path only an attribute; values a seller reads.
        row = lambda path: view.locator(f"tr[data-field='{path}'] td")  # noqa: E731
        assert row("originProduct.name").first.inner_text() == "상품명"
        assert row("originProduct.salePrice").first.inner_text() == "판매가"
        assert row("originProduct.statusType").nth(1).inner_text() == "판매중"
        assert row("originProduct.detailAttribute.minorPurchasable").first.inner_text() == (
            "미성년자 구매"
        )
        assert row("originProduct.detailAttribute.minorPurchasable").nth(1).inner_text() == "예"
        assert row("originProduct.images.representativeImage.url").first.inner_text() == (
            "대표 이미지"
        )
        assert row("originProduct.someNewField").first.inner_text() == "기타 등록 정보"
        assert view.locator("tr[data-not-sent='tags[0]'] td").first.inner_text() == "태그"
        brand = view.locator("tr[data-not-sent='attributes.brand'] td")
        assert brand.first.inner_text() == "속성 · 브랜드"
        assert brand.nth(1).inner_text() == "상세페이지 참조"
        assert "등록 정보 1" not in text and "제외 정보 1" not in text
        assert "originProduct" not in text
        _no_jargon(text)
    # A refused projection says so in Korean, with its code only as an attribute.
    with _page(browser, client, calls, override) as page:
        page.goto(REGISTER)
        unit = page.locator(f".register-unit[data-draft='{ready.draft_id}']")
        unit.locator("button[data-action='PREVIEW_SNAPSHOT']").click(timeout=15_000)
        view = unit.locator(".register-preview-view[data-projected='false']")
        view.wait_for(timeout=10_000)
        text = view.inner_text()
        assert "미리보기 불가" in text
        assert "스마트스토어 등록 형식을 만들 수 없습니다" in text
        assert view.locator("[data-reason='REGISTER_PREVIEW_PROJECTION_REFUSED']").count() == 1
        _no_jargon(text)


# The page's own wording seams, through the served modules.
WORDING = """async () => {
  const m = await import('/js/pages/register.js');
  return {
    prefixed: m.reasonCopy('M4_PRICING.PRICING_FX_STALE'),
    bare: m.reasonCopy('PRICING_FX_STALE'),
    base: m.reasonCopy('M4_BASE.IMAGE_QA_FAILED'),
    layer: m.reasonCopy('M4_PRICING.BLOCKED'),
    finding: m.reasonCopy('M4_BASE.IMAGE_FINDING_TOO_SMALL'),
    unknown: m.reasonCopy('SOMETHING_NEW'),
    wired: m.reasonCopy('REGISTER_PREFLIGHT_NOT_WIRED'),
    unit: m.reasonCopy('NO_UNIT_SELECTED'),
    live: m.reasonCopy('LIVE_RESIDUAL_RISK_NOT_ACCEPTED'),
    field: m.previewFieldLabel(
      'originProduct.detailAttribute.productInfoProvidedNotice.etc.manufacturer'),
    gap: m.gapCopy('anything else'),
  };
}"""


def test_every_reason_is_worded_and_never_its_code(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
) -> None:
    calls: list[tuple[str, str]] = []
    with _page(browser, client, calls) as page:
        page.goto(REGISTER)
        page.locator(".register-canary").wait_for(timeout=15_000)
        found = page.evaluate(WORDING)
    # An Item row's unprefixed code and the preflight's prefixed one read the same sentence.
    assert found["prefixed"] == found["bare"]
    assert "환율" in found["bare"]
    assert "다른 이미지" in found["base"]
    assert found["layer"] == "판매가를 확인해야 합니다."
    assert "이미지" in found["finding"]
    assert found["unknown"] == "확인이 필요한 항목입니다."
    assert "관리자" in found["wired"]
    assert "1건" in found["unit"]
    assert "관리자" in found["live"]
    assert found["field"] == "상품정보고시 · 제조사"
    assert "채울 수 없는 칸" in found["gap"]
    for text in found.values():
        assert "_" not in text, text


def test_the_editor_the_images_and_the_listing_panel_word_their_codes(
    browser: Browser,  # noqa: F811
    client: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    sources: Collections,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    from tests.support.register_support import draft, ready_item

    item = ready_item(container, sources, "1234")
    draft_id = draft(container.registrations, account, [item])
    listings = {
        "interval_s": 0,
        "last_run": None,
        "adoptable_suppliers": [],
        "listings": [
            {
                "kind": "REGISTERED",
                "registration_id": "reg-1",
                "product_name": "생들기름 350ml",
                "marketplace_product_id": "100",
                "deleted_by_icbm": False,
                "lifecycle_state": "ACTIVE",
                "last_result": "READ_FAILED",
                "error_code": "SMARTSTORE_SERVER_ERROR",
                "sale_status": None,
                "display_status": None,
                "sale_price": None,
                "snapshot_sale_price": 12000,
                "stock_quantity": None,
                "observed_at": None,
                "drift": ["SOMETHING_NEW"],
            }
        ],
    }

    def override(method: str, path: str, _body: bytes) -> tuple[int, object] | None:
        if method == "GET" and path == "/api/v1/operate/listings":
            return 200, listings
        if method == "POST" and path == "/api/v1/operate/listings/sync":
            return 500, {
                "error": {
                    "class": "UNKNOWN",
                    "code": "UNHANDLED_EXCEPTION",
                    "message": "boom: an English failure",
                    "correlation_id": "c",
                }
            }
        return None

    calls: list[tuple[str, str]] = []
    with _page(browser, client, calls, override) as page:
        page.goto(REGISTER)
        unit = page.locator(f".register-unit[data-draft='{draft_id}']")
        images = unit.locator(f"[data-item-images='{item.item_id}'][data-images-state='READY']")
        images.wait_for(timeout=15_000)
        # The image check and who chose: words, never "QA" or the actor's id.
        qa = images.locator("[data-qa]").first.inner_text()
        assert qa in ("이미지 검사 통과", "이미지 확인 필요", "이미지 검사 실패", "검사 전")
        origin = images.locator("[data-selection-by]").first.inner_text()
        assert origin in ("직접 선택함", "자동으로 선택됨", "아직 선택 없음")
        _no_jargon(images.inner_text())
        # The 판매 상태 panel: a failed read and an unknown drift in words, codes as attributes.
        panel = page.locator("[data-role='listing-sync']")
        panel.locator("[data-listing-sync='READY']").wait_for(timeout=15_000)
        failed = panel.locator("[data-listing-state='READ_FAILED']")
        assert failed.get_attribute("data-error-code") == "SMARTSTORE_SERVER_ERROR"
        assert "SMARTSTORE" not in (failed.get_attribute("title") or "")
        assert panel.locator("[data-drift='SOMETHING_NEW']").inner_text() == "차이 있음"
        panel.locator("button[data-action='sync-listings']").click()
        toasts = page.locator("#toastStack")
        toasts.get_by_text("요청을 처리하지 못했습니다").first.wait_for(timeout=5_000)
        assert "boom" not in toasts.inner_text()
        _no_jargon(panel.inner_text())
        # The editor names a unit it cannot find as a product, and its steps in plain words.
        page.goto(f"{LOCAL}/#/register-editor?draft={draft_id}&unit=gone&mode=register")
        page.get_by_text("이 상품을 찾을 수 없습니다").wait_for(timeout=15_000)
        units = client.get(
            f"/api/v1/register/units/{draft_id}", headers={"X-ICBM-Client": "pytest"}
        ).json()
        page.goto(
            f"{LOCAL}/#/register-editor?draft={draft_id}&unit={units[0]['unit_ref']}&mode=register"
        )
        page.locator("[data-role='ready-strip']").wait_for(timeout=15_000)
        strip = page.locator("[data-role='ready-strip']").inner_text()
        assert "불통" not in strip and "사전 점검" not in strip
        _no_jargon(page.locator("[data-role='register-editor']").inner_text())
