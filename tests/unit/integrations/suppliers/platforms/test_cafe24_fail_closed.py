"""The Cafe24 template fails closed where KM통상's parser reads a first value, a guess or nothing
(ADR-0010 §7, §8, §10; ADR-0007 §4; ADR-0030 §2, §10). Every page here is written in the test."""

import pytest

from app.stages.collect.facts import Availability, FieldStatus, ShippingKind
from integrations.suppliers.collection import ImageRole
from integrations.suppliers.platforms.cafe24.collect import classify_images, parse_fields
from tests.unit.integrations.suppliers.kmretail.test_km_facts_parser import (
    BUY,
    SOLD_OUT,
    URL,
    document,
    page,
    row,
)


def fields(body: str) -> dict:
    return parse_fields(document(body))


def test_an_unclosed_description_block_is_never_read_as_the_description() -> None:
    body = page(
        body=BUY
        + '<div id="prdDetail"><div class="cont"><p>본품 설명</div>'
        + '<div class="board">후기 http://blog.ex/p?id=1</div>'
    )
    assert fields(body)["detail_description"].status is FieldStatus.REVIEW_REQUIRED


def test_a_description_with_url_material_is_held_for_review_and_never_aborts() -> None:
    body = page(body=BUY + '<div id="prdDetail"><p>자세히 https://ex.com/a 참고</p></div>')
    fact = fields(body)["detail_description"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert all("//" not in (e.observed or "") for e in fact.evidence)


@pytest.mark.parametrize(
    "control",
    [
        '<a class="btnBuy" style="display: none;">BUY</a>',
        '<button class="btnBuy" disabled>BUY</button>',
        '<a class="btnBuy" hidden>BUY</a>',
    ],
)
def test_a_hidden_or_disabled_control_is_no_offer(control: str) -> None:
    body = page(body=f'<div class="xans-product-action">{control}<span>SOLD OUT</span></div>')
    stock = fields(body)["stock"]
    assert stock.status is FieldStatus.CONFIRMED
    assert stock.value.availability is Availability.SOLD_OUT


def test_a_header_cart_link_is_not_this_product_s_control() -> None:
    body = page(body='<ul><li class="btnCart"><a>CART</a></li></ul>' + SOLD_OUT)
    assert fields(body)["stock"].value.availability is Availability.SOLD_OUT


def test_an_overflow_hidden_style_hides_nothing() -> None:
    body = page(
        body='<div class="xans-product-action"><a id="btnBuy" style="overflow:hidden">BUY</a></div>'
    )
    assert fields(body)["stock"].value.availability is Availability.ON_SALE


@pytest.mark.parametrize(
    "elsewhere",
    [
        "<option>파랑 [품절]</option>",
        '<div id="prdDetail"><p>재고 품절 시 순차 발송</p></div>',
    ],
)
def test_sold_out_words_outside_the_action_area_decide_nothing(elsewhere: str) -> None:
    body = page(body=elsewhere).replace("<head>", "<head><title>[품절] 상품</title>")
    assert fields(body)["stock"].status is FieldStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "cell", ["판매가의 80% (12,000원)", "2025년부터 자율", "15,000원 ~ 20,000원"]
)
def test_a_minimum_cell_that_is_not_one_amount_is_held_for_review(cell: str) -> None:
    assert fields(page(rows=row("최저판매가", cell)))["minimum_sale_price"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


def test_a_minimum_of_one_amount_or_more_is_read() -> None:
    fact = fields(page(rows=row("최저판매가", "29,500원 이상")))["minimum_sale_price"]
    assert fact.status is FieldStatus.CONFIRMED
    assert fact.value.amount_krw == 29500


def test_fee_rows_that_disagree_or_a_conditional_method_are_held_for_review() -> None:
    two = page(rows=row("배송비", "3,000원") + row("배송비", "무료"), body=BUY)
    assert fields(two)["shipping"].status is FieldStatus.REVIEW_REQUIRED
    conditional = page(rows=row("배송방법", "택배 (5만원 이상 무료)") + row("배송비", "3,000원"))
    assert fields(conditional)["shipping"].status is FieldStatus.REVIEW_REQUIRED
    free = fields(page(rows=row("배송비", "무료")))["shipping"]
    assert free.value.kind is ShippingKind.FREE


def test_a_meta_price_no_row_states_holds_the_prices() -> None:
    body = page(
        rows=row("판매가", "15,000원"),
        head_extra='<meta property="product:sale_price:amount" content="12000">',
    )
    assert fields(body)["prices"].status is FieldStatus.REVIEW_REQUIRED
    agreeing = page(
        rows=row("판매가", "15,000원"),
        head_extra='<meta property="product:price:amount" content="15000">',
    )
    assert fields(agreeing)["prices"].status is FieldStatus.CONFIRMED


def test_badly_grouped_digits_are_not_an_amount() -> None:
    assert fields(page(rows=row("판매가", "1,50,00원")))["prices"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


def test_a_heading_never_takes_a_value_from_another_row() -> None:
    body = page(
        rows="<tr><th>브랜드</th><th>원산지</th></tr><tr><td>나이키</td><td>베트남</td></tr>"
    )
    parsed = fields(body)
    assert parsed["origin"].status is FieldStatus.ABSENT
    assert parsed["brand"].status is FieldStatus.ABSENT


@pytest.mark.parametrize(
    "shown",
    [
        "<h2>상품정보제공고시</h2>",
        "<div>상품정보 제공고시</div>",
        "<th>품명 및 모델명</th><td>A</td>",
    ],
)
def test_a_shown_notice_is_never_absent(shown: str) -> None:
    assert fields(page(body=shown))["notice"].status is FieldStatus.REVIEW_REQUIRED


def test_a_container_whose_class_only_starts_with_the_option_class_is_not_it() -> None:
    body = page(
        body='<div class="xans-product-optionnotice"></div>'
        '<div class="xans-product-option"><select><option>A</option></select></div>'
    )
    assert fields(body)["options"].status is FieldStatus.REVIEW_REQUIRED


def test_url_material_in_a_row_value_is_held_for_review() -> None:
    fact = fields(page(rows=row("브랜드", "나이키 (https://nike.com)")))["brand"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert all("//" not in (e.observed or "") and "//" not in e.locator for e in fact.evidence)


def test_tab_strip_images_inside_the_description_are_layout() -> None:
    body = page(
        body='<div id="prdDetail"><ul class="dMenu"><li><img src="/tab.gif"></li></ul>'
        '<img src="/web/d.jpg"></div>'
    )
    roles = {c.url.rsplit("/", 1)[-1]: c.role for c in classify_images(body, URL)}
    assert roles["tab.gif"] is ImageRole.UI_COMMON
    assert roles["d.jpg"] is ImageRole.DETAIL
