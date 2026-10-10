"""The Godomall template's reading rules, fail-closed (ADR-0030 §4, ADR-0034).

Every page here is synthetic, written in the shape 건강산's reconnaissance captures showed (Issue
#219 `6086058056`), and as the browser capture keeps it: no input carries a name or a value
(ADR-0019 §6.1). Each case states what a page must say before a fact is CONFIRMED, and that a page
which says something else is held for review rather than guessed. No supplier is contacted.
"""

import json
from pathlib import Path

import pytest

from app.stages.collect.facts import Availability, FieldStatus, SalesChannelScope, ShippingKind
from integrations.suppliers.base import ProbeResponse
from integrations.suppliers.collection import DocumentView, ReadKind
from integrations.suppliers.platforms.godomall import authenticated, login_required, vocabulary
from integrations.suppliers.platforms.godomall.collect import (
    SourceIdentity,
    UnresolvedIdentity,
    Vocabulary,
    goods_number,
    parse_fields,
    resolve,
)
from tests.unit.integrations.suppliers.test_site_config import site

NUMBER = "1000000001"
URL = f"https://shop.example/goods/goods_view.php?goodsNo={NUMBER}"
WORDS = Vocabulary(
    purchase_price=("판매가",),
    list_price=("정가",),
    minimum_price_phrase=("판매가격절대준수",),
    channel_all_allowed=("모든마켓 판매가능",),
    channel_forbid_coupang=("쿠팡판매 불가",),
)
BUY = (
    '<div class="btn_choice_box"><button class="btn_add_cart">장바구니</button>'
    '<button class="btn_add_order">바로 구매</button></div>'
)
SOLD_OUT = (
    '<div class="btn_choice_box btn_restock_box">'
    '<button class="btn_add_soldout" disabled>구매 불가</button></div>'
)
# A product without options: its one order line is written in advance.
ONE_LINE = (
    '<div class="item_choice_list"><table class="option_display_area">'
    '<tbody id="option_display_item_0"><tr><td>마그네슘</td></tr></tbody></table></div>'
)
TIERS = (
    '<div id="lyDelivery" class="layer_area" style="display:none;"><ul>'
    "<li> 0원 이상 ~ 200,000원 미만 <span> 3,000원 </span></li>"
    "<li> 200,000원 이상 <span> 0원 </span></li></ul></div>"
)
REGION = (
    '<div id="lyDeliveryZone" class="layer_area" style="display:none;">'
    '<div class="ly_tit"><strong>지역별배송비</strong></div><div class="ly_cont">{}</div></div>'
)


def row(label: str, value: str) -> str:
    return f"<dl><dt>{label}</dt><dd>{value}</dd></dl>"


BASE_ROWS = row("판매가", "<strong>15,000</strong>원") + row("상품코드", NUMBER)


def page(
    *,
    rows: str = BASE_ROWS,
    choice: str = ONE_LINE,
    controls: str = BUY,
    description: str | None = "<p>설명입니다</p>",
    og_url: str | None = URL,
) -> DocumentView:
    head = '<meta property="og:title" content="마그네슘">'
    if og_url is not None:
        head += f'<meta property="og:url" content="{og_url}">'
    detail = (
        ""
        if description is None
        else f'<div id="detail"><div class="detail_cont"><div class="txt-manual">{description}'
        "</div></div></div>"
    )
    body = (
        f"<html><head>{head}</head><body>"
        '<div class="goods_view_top"><form id="frmView"><input type="hidden">'
        '<div class="item_detail_tit"><h3>마그네슘</h3></div>'
        f'<div class="item_detail_list">{rows}</div>{choice}{controls}'
        f"</form></div>{detail}</body></html>"
    )
    return DocumentView(
        ReadKind.PRODUCT_READ, 200, "/goods/goods_view.php", None, "text/html", body
    )


def fields(view: DocumentView) -> dict:
    return parse_fields(view, WORDS)


def minimum(description: str) -> object:
    return fields(page(description=description))["minimum_sale_price"]


# ---------------------------------------------------------------- CONNECT


def test_a_redirect_to_the_login_page_proves_a_login_is_required() -> None:
    response = ProbeResponse(302, "/mypage/index.php", "/member/login.php", "")
    proven, signals = login_required(response)
    assert proven and signals == ("redirect_to_login",)


def test_a_status_alone_never_proves_a_login_is_required() -> None:
    proven, signals = login_required(ProbeResponse(403, "/mypage/index.php", None, "denied"))
    assert not proven and signals == ("access_denied",)


def test_only_a_member_page_proves_a_session() -> None:
    member = '<a href="/member/logout.php">로그아웃</a><a href="/mypage/order_list.php">주문</a>'
    assert authenticated(ProbeResponse(200, "/mypage/index.php", None, member))[0]
    # One signal is not enough, and a login form on the page proves the opposite.
    one = '<a href="/member/logout.php">x</a>'
    assert not authenticated(ProbeResponse(200, "/mypage/index.php", None, one))[0]
    with_form = member + '<form id="formLogin"></form>'
    assert not authenticated(ProbeResponse(200, "/mypage/index.php", None, with_form))[0]
    assert not authenticated(ProbeResponse(500, "/mypage/index.php", None, member))[0]


def test_a_site_s_words_are_appended_to_the_template_s() -> None:
    words = vocabulary(site(platform="godomall", label_overrides={"purchase_price": ["회원가"]}))
    assert words.purchase_price == ("회원가",)
    assert words.price == Vocabulary().price
    assert words.sales_channel_row == ("판매가능플랫폼",)


# ---------------------------------------------------------------- capture policy


def test_the_capture_policy_keeps_no_user_input() -> None:
    # ADR-0019 §6.1: no name or value attribute and no textarea, for every template's policy.
    platforms = Path(__file__).resolve().parents[5] / "integrations" / "suppliers" / "platforms"
    policies = sorted(platforms.glob("*/browser_capture_policy.json"))
    assert {path.parent.name for path in policies} >= {"cafe24", "godomall"}
    for path in policies:
        policy = json.loads(path.read_text("utf-8"))
        kept = {name for names in policy["allowed_attributes"].values() for name in names}
        assert not kept & {"name", "value"}, path
        assert "textarea" in policy["excluded_tags"], path


# ---------------------------------------------------------------- identity


def test_the_identity_is_the_number_every_declaration_agrees_on() -> None:
    found = resolve(page(), URL)
    assert isinstance(found, SourceIdentity) and found.source_product_id == NUMBER
    assert goods_number(URL + "&mtn=1") == NUMBER
    assert goods_number("https://shop.example/goods/goods_view.php?goodsNo=1&goodsNo=2") is None


@pytest.mark.parametrize(
    ("view", "url"),
    [
        (page(og_url=None), URL),
        (page(og_url="https://shop.example/goods/goods_view.php?goodsNo=999"), URL),
        (page(og_url=f"https://shop.example/goods/goods_list.php?goodsNo={NUMBER}"), URL),
        (page(), "https://shop.example/goods/goods_view.php?goodsNo=999"),
        (page(rows=row("판매가", "<strong>15,000</strong>원")), URL),
    ],
    ids=["no og:url", "og:url disagrees", "og:url not a product page", "url disagrees", "no code"],
)
def test_an_identity_that_is_not_corroborated_everywhere_is_unresolved(
    view: DocumentView, url: str
) -> None:
    assert isinstance(resolve(view, url), UnresolvedIdentity)


# ---------------------------------------------------------------- prices


@pytest.mark.parametrize(
    "cell", ["<strong>15,000</strong>원 ~ 16,000원", "문의", "<strong>1,50,00</strong>원"]
)
def test_a_price_cell_that_is_not_one_amount_is_held_for_review(cell: str) -> None:
    rows = row("판매가", cell) + row("상품코드", NUMBER)
    assert fields(page(rows=rows))["prices"].status is FieldStatus.REVIEW_REQUIRED


def test_a_price_row_stated_twice_with_two_amounts_is_held_for_review() -> None:
    rows = BASE_ROWS + row("판매가", "<strong>14,000</strong>원")
    assert fields(page(rows=rows))["prices"].status is FieldStatus.REVIEW_REQUIRED


# ---------------------------------------------------------------- the description minimum


@pytest.mark.parametrize(
    ("description", "amount"),
    [
        ("<p>판매가격절대준수 1개 21,000원 이상 판매 부탁드립니다.</p>", 21000),
        ("<p>판매가격절대준수 4,400원 이상</p><p>배송비 포함 24,000원 이상</p>", 4400),
        ("<p>판매가격절대준수 1개 21,000원 이상</p><p>1개 21,000원 / 2개 묶음 40,900원</p>", 21000),
        ("<ul><li>판매가격절대준수 1개 21,000원 이상</li></ul>", 21000),
    ],
    ids=["one unit", "shipping-inclusive sentence", "bundle sentence", "list item"],
)
def test_the_description_minimum_is_the_per_unit_amount_after_the_phrase(
    description: str, amount: int
) -> None:
    fact = minimum(description)
    assert fact.status is FieldStatus.CONFIRMED
    assert fact.value.amount_krw == amount


@pytest.mark.parametrize(
    "description",
    [
        "<p>판매가격절대준수 1개 21,000원 이상</p><p>판매가격절대준수 22,000원 이상</p>",
        "<p>판매가격절대준수 1개 21,000원 이상 / 판매가격절대준수 25,000원 이상</p>",
        "<p>판매가격절대준수 2개 묶음 40,900원 이상</p>",
        "<p>판매가격절대준수 2개 40,900원 이상</p>",
        "<p>판매가격절대준수 3개 63,000원 이상</p>",
        "<p>판매가격절대준수 정가 30,000원, 2개 이상 구매시 19,000원 이상</p>",
        "<p>판매가격절대준수 배송비 포함 24,000원 이상</p>",
        "<p>판매가격절대준수 부탁드립니다</p>",
        "<p>안내</p><div>판매가격절대준수 5,000원 이상</div>",
    ],
    ids=[
        "two sentences, two amounts",
        "two phrases in one sentence",
        "bundle",
        "two units",
        "three units",
        "another amount first",
        "shipping-inclusive",
        "no amount",
        "outside every sentence unit",
    ],
)
def test_a_description_minimum_that_is_not_one_per_unit_amount_is_held(description: str) -> None:
    assert minimum(description).status is FieldStatus.REVIEW_REQUIRED


def test_an_unclosed_description_never_states_a_minimum() -> None:
    view = page(description="<p>판매가격절대준수 1개 21,000원 이상</p>")
    body = view.body.replace("</div></div></div></body>", "</body>")
    unclosed = DocumentView(view.kind, 200, view.path, None, view.content_type, body)
    assert fields(unclosed)["minimum_sale_price"].status is FieldStatus.REVIEW_REQUIRED


def test_without_the_phrase_there_is_no_description_minimum() -> None:
    assert minimum("<p>1개 21,000원 이상 판매 부탁드립니다.</p>").status is FieldStatus.ABSENT


def test_a_minimum_row_reading_autonomous_states_no_minimum() -> None:
    # ADR-0030 §2, as in the Cafe24 template.
    rows = BASE_ROWS + row("최저판매가", "자율")
    assert fields(page(rows=rows))["minimum_sale_price"].status is FieldStatus.ABSENT


# ---------------------------------------------------------------- shipping


def shipping(dd: str) -> object:
    return fields(page(rows=BASE_ROWS + row("배송비", dd)))["shipping"]


def test_a_free_over_layer_is_a_conditional_policy_with_its_base_fee() -> None:
    fact = shipping(
        f"<strong>3,000원</strong><strong> / 주문시결제(선결제)</strong>{TIERS}{REGION.format('')}"
    )
    value = fact.value
    assert fact.status is FieldStatus.CONFIRMED
    assert (value.kind, value.fee_krw, value.free_over_krw) == (
        ShippingKind.CONDITIONAL,
        3000,
        200000,
    )
    # ADR-0034 §2: the page's words stay visible beside the price, the region layer's included.
    assert "금액별배송비" in value.policy_text and "지역별추가배송비" in value.policy_text


def test_a_fee_range_is_fixed_at_its_highest_amount() -> None:
    # ADR-0032 §4 PR-04: the owner's rule, for every template; the words are kept.
    fact = shipping("<strong>3,000원 ~ 4,000원</strong><strong> / 주문시결제(선결제)</strong>")
    assert fact.status is FieldStatus.CONFIRMED
    assert (fact.value.kind, fact.value.fee_krw) == (ShippingKind.FIXED, 4000)
    assert "3,000원 ~ 4,000원" in fact.value.policy_text
    assert any(evidence.normalized == "4000" for evidence in fact.evidence)


def test_a_fee_without_a_layer_is_fixed_and_zero_is_free() -> None:
    fixed = shipping("<strong>3,000원</strong>").value
    assert (fixed.kind, fixed.fee_krw) == (ShippingKind.FIXED, 3000)
    assert shipping("<strong>0원</strong>").value.kind is ShippingKind.FREE


@pytest.mark.parametrize(
    "dd",
    [
        "<strong>3,000원 ~ 4,000원</strong>" + TIERS,
        "<strong>3,000원 ~ 4,000원</strong> 제주 5,000원 추가",
        "<strong>3,000원</strong> 제주 5,000원 추가",
        "<strong>3,000원</strong>"
        + TIERS.replace("200,000원 이상 <span> 0원", "200,000원 이상 <span> 1,000원"),
        "<strong>4,000원</strong>" + TIERS,
        "<strong>3,000원</strong>" + TIERS.replace("</ul>", "</ul><p>제주 추가 5,000원</p>"),
        "<strong>3,000원</strong>" + TIERS + TIERS.replace("3,000원", "2,500원"),
        "<strong>3,000원</strong>" + TIERS + REGION.format("<p>제주 5,000원</p>"),
        "착불",
    ],
    ids=[
        "a range beside tiers",
        "a range and another amount",
        "another amount",
        "not free over",
        "base fee disagrees",
        "an amount outside the tiers",
        "two tier layers",
        "a region surcharge",
        "no amount",
    ],
)
def test_any_other_shipping_statement_is_held_for_review(dd: str) -> None:
    assert shipping(dd).status is FieldStatus.REVIEW_REQUIRED


# ---------------------------------------------------------------- stock and options


def test_stock_is_read_only_inside_the_choice_box() -> None:
    assert fields(page())["stock"].value.availability is Availability.ON_SALE
    assert fields(page(controls=SOLD_OUT))["stock"].value.availability is Availability.SOLD_OUT
    elsewhere = page(controls='<div class="btn_add_cart">장바구니</div>')
    assert fields(elsewhere)["stock"].status is FieldStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "controls",
    [
        BUY.replace("<button ", "<button disabled "),
        BUY.replace("<button ", '<button style="display:none" '),
        BUY.replace('<div class="btn_choice_box">', '<div class="btn_choice_box" hidden>'),
    ],
    ids=["disabled", "styled hidden", "hidden box"],
)
def test_a_control_no_buyer_can_use_is_no_offer(controls: str) -> None:
    assert fields(page(controls=controls))["stock"].status is FieldStatus.REVIEW_REQUIRED


def test_a_stock_row_of_none_beside_an_active_control_is_a_disagreement() -> None:
    view = page(rows=BASE_ROWS + row("상품재고", "0개"))
    assert fields(view)["stock"].status is FieldStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "choice",
    [
        "",
        '<div class="item_choice_list"><table class="option_display_area"></table></div>',
        ONE_LINE + '<select style="display:none"><option>90정</option></select>',
        ONE_LINE + '<div class="option_select_box">옵션</div>',
        ONE_LINE.replace("</tbody>", '</tbody><tbody id="option_display_item_1"></tbody>'),
    ],
    ids=[
        "no order list",
        "an empty order list",
        "a hidden select",
        "an option control",
        "two lines",
    ],
)
def test_options_are_absent_only_when_the_page_shows_one_line_and_no_control(choice: str) -> None:
    assert fields(page(choice=choice))["options"].status is FieldStatus.REVIEW_REQUIRED
    assert fields(page())["options"].status is FieldStatus.ABSENT


# ---------------------------------------------------------------- description and channels


def test_a_description_with_url_material_is_held_for_review() -> None:
    view = page(description="<p>판매가격절대준수 https://example.com 1개 21,000원 이상</p>")
    read = fields(view)
    assert read["detail_description"].status is FieldStatus.REVIEW_REQUIRED
    assert read["minimum_sale_price"].status is FieldStatus.REVIEW_REQUIRED
    assert "example.com" not in repr(read["minimum_sale_price"].evidence)


def test_a_channel_restriction_in_the_description_is_read() -> None:
    restricted = fields(page(description="<p>★ 쿠팡판매 불가 상품 ★</p>"))["sales_channels"]
    assert restricted.status is FieldStatus.CONFIRMED
    assert restricted.value.forbidden == ("coupang",)
    # A row that is not a channel row decides nothing.
    other = page(rows=BASE_ROWS + row("비고", "쿠팡판매 불가"))
    assert fields(other)["sales_channels"].status is FieldStatus.ABSENT


def test_a_channel_row_must_be_read_whole() -> None:
    # ADR-0031 §3, as in the Cafe24 template.
    allowed = fields(page(rows=BASE_ROWS + row("판매가능플랫폼", "모든마켓 판매가능")))
    assert allowed["sales_channels"].value.scope is SalesChannelScope.ALL_ALLOWED
    unread = fields(page(rows=BASE_ROWS + row("판매가능플랫폼", "문의")))
    assert unread["sales_channels"].status is FieldStatus.REVIEW_REQUIRED
    empty = fields(page(rows=BASE_ROWS + row("판매가능플랫폼", '<img src="/a.png">')))
    assert empty["sales_channels"].status is FieldStatus.REVIEW_REQUIRED
    both = page(
        rows=BASE_ROWS + row("판매가능플랫폼", "모든마켓 판매가능"),
        description="<p>쿠팡판매 불가</p>",
    )
    assert fields(both)["sales_channels"].status is FieldStatus.REVIEW_REQUIRED


def test_a_shown_notice_is_never_absent() -> None:
    view = page(
        description="<p>설명</p><h3>상품필수 정보</h3><table><tr><th>소비기한</th></tr></table>"
    )
    assert fields(view)["notice"].status is FieldStatus.REVIEW_REQUIRED
