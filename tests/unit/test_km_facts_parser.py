"""KM통상's source-truth parser contract (Issue #52 ruling 5702780630).

Every fixture is written here. No supplier is contacted, no capture is opened, and the parser is
handed nothing but an immutable document view.
"""

import pytest

from app.collect.facts import Availability, EvidenceKind, FieldStatus, ShippingKind
from integrations.suppliers.collection import DocumentView, ReadKind
from integrations.suppliers.kmretail.collect import parse_fields, resolve
from integrations.suppliers.kmretail.collect.identity import (
    UNRESOLVED_ABSENT,
    UNRESOLVED_DISAGREE,
    UNRESOLVED_INCOMPLETE,
    UNRESOLVED_SHAPE,
    SourceIdentity,
    UnresolvedIdentity,
)

URL = "https://kmretail.co.kr/product/%EC%83%81%ED%92%88/355/category/23/display/1/"


def document(body: str) -> DocumentView:
    return DocumentView(
        kind=ReadKind.PRODUCT_READ,
        status=200,
        path="/product/x/355/",
        location=None,
        content_type="text/html",
        body=body,
    )


def page(
    *,
    name: str = "[어바틀] 프리미엄 마그네슘 90정",
    number: str = "355",
    retailer_item: str | None = None,
    with_retailer_item: bool = True,
    canonical: str | None = "https://kmretail.co.kr/product/%EC%83%81%ED%92%88/355",
    rows: str = "",
    body: str = "",
    head_extra: str = "",
) -> str:
    """A product page in this storefront's own shape: declared metadata, then labelled rows."""
    meta = [f'<meta property="og:title" content="{name}">']
    if number:
        meta.append(f'<meta property="product:productId" content="{number}">')
    if with_retailer_item:
        stated = number if retailer_item is None else retailer_item
        meta.append(
            f'<meta property="product:retailer_item_id" content="{stated}">' if stated else ""
        )
    if canonical:
        meta.append(f'<link rel="canonical" href="{canonical}">')
    return (
        f"<html><head>{''.join(meta)}{head_extra}</head><body>"
        f'<div class="infoArea"><table><tbody>{rows}</tbody></table></div>{body}'
        "</body></html>"
    )


def row(label: str, value: str, *, hidden: bool = False) -> str:
    klass = ' class="displaynone xans-record-"' if hidden else ' class="xans-record-"'
    return f"<tr{klass}><th><span>{label}</span></th><td>{value}</td></tr>"


BUY = '<div class="xans-product-action"><a><span id="btnBuy">바로 구매하기</span></a></div>'
SOLD_OUT = '<div class="xans-product-action"><span class="btnEm">SOLD OUT</span></div>'


# ---------------------------------------------------------------- P0: source identity


def test_the_identity_is_the_number_the_page_declares() -> None:
    resolved = resolve(document(page()), URL)
    assert isinstance(resolved, SourceIdentity)
    assert resolved.source_product_id == "355"
    assert resolved.agreed == (
        "meta[product:productId]",
        "meta[product:retailer_item_id]",
        "link[canonical]",
        "url",
    )


def test_a_renamed_product_keeps_its_identity() -> None:
    # The name lives in its own path segment and in og:title; neither is the identity.
    before = resolve(document(page(name="예전 이름")), URL)
    after = resolve(
        document(page(name="완전히 다른 이름")),
        "https://kmretail.co.kr/product/%EB%8B%A4%EB%A5%B8/355/category/23/display/1/",
    )
    assert isinstance(before, SourceIdentity) and isinstance(after, SourceIdentity)
    assert before.source_product_id == after.source_product_id == "355"


def test_a_page_that_declares_no_number_is_unresolved() -> None:
    unresolved = resolve(document(page(number="", canonical=None)), URL)
    assert isinstance(unresolved, UnresolvedIdentity)
    assert unresolved.reason == UNRESOLVED_ABSENT


@pytest.mark.parametrize("number", ["", "  ", "abc", "35-5", "355a"])
def test_a_number_that_is_not_digits_is_never_an_identity(number: str) -> None:
    unresolved = resolve(document(page(number=number, canonical=None)), URL)
    assert isinstance(unresolved, UnresolvedIdentity)
    assert unresolved.reason in (UNRESOLVED_ABSENT, UNRESOLVED_SHAPE)


@pytest.mark.parametrize(
    ("kwargs", "url", "absent"),
    [
        ({"with_retailer_item": False}, URL, "meta[product:retailer_item_id]"),
        ({"canonical": None}, URL, "link[canonical]"),
        ({}, "https://kmretail.co.kr/", "url"),
    ],
)
def test_a_corroboration_the_page_does_not_make_leaves_the_identity_unresolved(
    kwargs: dict[str, object], url: str, absent: str
) -> None:
    # This field decides whether a revision history is appended to or split in two. A declaration
    # the page never made is not agreement, so one missing corroboration is enough to stop.
    unresolved = resolve(document(page(**kwargs)), url)  # type: ignore[arg-type]
    assert isinstance(unresolved, UnresolvedIdentity)
    assert unresolved.reason == UNRESOLVED_INCOMPLETE
    assert unresolved.missing == (absent,)


@pytest.mark.parametrize(
    ("kwargs", "url", "unreadable"),
    [
        ({"retailer_item": "35-5"}, URL, "meta[product:retailer_item_id]"),
        ({"retailer_item": ""}, URL, "meta[product:retailer_item_id]"),
        (
            {"canonical": "https://kmretail.co.kr/product/%EC%83%81%ED%92%88/"},
            URL,
            "link[canonical]",
        ),
        (
            {"canonical": "https://kmretail.co.kr/category/23/product/%EC%83%81%ED%92%88/355/"},
            URL,
            "link[canonical]",
        ),
        ({}, "https://kmretail.co.kr/product/355/", "url"),
        ({}, "https://kmretail.co.kr/product/%EC%83%81%ED%92%88/x/category/355/", "url"),
    ],
)
def test_a_corroboration_it_cannot_read_is_never_assumed_to_agree(
    kwargs: dict[str, object], url: str, unreadable: str
) -> None:
    # The number sits at one position of a product path. A digit in a category or display segment
    # is not the identity, and a path that is not a product path states nothing at all.
    unresolved = resolve(document(page(**kwargs)), url)  # type: ignore[arg-type]
    assert isinstance(unresolved, UnresolvedIdentity)
    assert unresolved.reason == UNRESOLVED_INCOMPLETE
    assert unresolved.missing == (unreadable,)


def test_declarations_that_disagree_leave_the_identity_unresolved() -> None:
    # Two numbers, no way to tell which is the product: nothing is invented.
    unresolved = resolve(document(page(retailer_item="999")), URL)
    assert isinstance(unresolved, UnresolvedIdentity)
    assert unresolved.reason == UNRESOLVED_DISAGREE
    assert unresolved.seen == ("355", "999")


def test_an_identity_is_never_derived_from_the_name() -> None:
    import hashlib

    name = "[어바틀] 프리미엄 마그네슘 90정"
    resolved = resolve(document(page(name=name)), URL)
    assert isinstance(resolved, SourceIdentity)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    assert resolved.source_product_id not in (digest, digest[:16], name)


# ---------------------------------------------------------------- P1: the facts


def fields(**kwargs: object):
    return parse_fields(document(page(**kwargs)))  # type: ignore[arg-type]


def test_the_original_name_is_the_page_s_own() -> None:
    fact = fields(rows=row("상품명", "[어바틀] 프리미엄 마그네슘 90정", hidden=True))[
        "original_name"
    ]
    assert fact.status is FieldStatus.CONFIRMED
    assert fact.value is not None and fact.value.text == "[어바틀] 프리미엄 마그네슘 90정"
    assert {evidence.kind for evidence in fact.evidence} == {
        EvidenceKind.ATTRIBUTE,
        EvidenceKind.DOM_TEXT,
    }


def test_each_price_keeps_the_exact_label_the_page_used() -> None:
    fact = fields(rows=row("판매가", "39,000원", hidden=True) + row("소비자가", "45,000원"))[
        "prices"
    ]
    assert fact.status is FieldStatus.CONFIRMED
    assert fact.value is not None
    assert [(p.label, p.amount_krw) for p in fact.value.prices] == [
        ("판매가", 39000),
        ("소비자가", 45000),
    ]
    assert all(isinstance(p.amount_krw, int) for p in fact.value.prices), "won, never a float"


def test_a_price_row_without_an_amount_is_not_a_price() -> None:
    fact = fields(rows=row("판매가", "전화문의"))["prices"]
    assert fact.status is not FieldStatus.CONFIRMED
    assert fact.value is None


def test_the_minimum_sale_price_is_read_only_when_the_page_states_it() -> None:
    stated = fields(rows=row("판매가", "39,000원") + row("최저지도가", "35,000"))
    assert stated["minimum_sale_price"].status is FieldStatus.CONFIRMED
    value = stated["minimum_sale_price"].value
    assert value is not None and (value.label, value.amount_krw) == ("최저지도가", 35000)

    unstated = fields(rows=row("판매가", "39,000원"))
    assert unstated["minimum_sale_price"].status is FieldStatus.ABSENT
    assert unstated["minimum_sale_price"].value is None, "never derived from the sale price"


def test_shipping_keeps_the_page_s_own_policy_words() -> None:
    fact = fields(rows=row("배송방법", "택배") + row("배송비", "3,000원"))["shipping"]
    assert fact.status is FieldStatus.CONFIRMED
    assert fact.value is not None
    assert fact.value.kind is ShippingKind.FIXED and fact.value.fee_krw == 3000
    assert "택배" in fact.value.policy_text and "3,000원" in fact.value.policy_text


def test_a_shipping_method_without_a_fee_states_no_amount() -> None:
    fact = fields(rows=row("배송방법", "택배"))["shipping"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert fact.value is None, "a fee is never guessed from a method"


def test_free_shipping_carries_no_fee() -> None:
    fact = fields(rows=row("배송방법", "택배") + row("배송비", "0원"))["shipping"]
    assert fact.value is not None
    assert fact.value.kind is ShippingKind.FREE and fact.value.fee_krw is None


# ---------------------------------------------------------------- stock


def test_an_active_purchase_control_is_on_sale() -> None:
    fact = fields(body=BUY)["stock"]
    assert fact.status is FieldStatus.CONFIRMED
    assert fact.value is not None and fact.value.availability is Availability.ON_SALE


def test_sold_out_with_no_purchase_path_is_sold_out() -> None:
    fact = fields(body=SOLD_OUT)["stock"]
    assert fact.status is FieldStatus.CONFIRMED
    assert fact.value is not None and fact.value.availability is Availability.SOLD_OUT


def test_a_sold_out_mark_the_reader_cannot_see_is_not_a_state() -> None:
    # This storefront keeps a SOLD OUT element in the button row and hides it while the product
    # is on sale. A hidden mark is not an offer withdrawn, and its words do not become the
    # visible container's words.
    hidden = (
        '<div class="xans-product-action"><div class="ec-base-button">'
        '<a><span id="btnBuy">바로 구매하기</span></a>'
        '<span class="btnEm displaynone">SOLD OUT</span></div></div>'
    )
    fact = fields(body=hidden)["stock"]
    assert fact.status is FieldStatus.CONFIRMED
    assert fact.value is not None and fact.value.availability is Availability.ON_SALE


def test_both_a_purchase_control_and_a_visible_sold_out_mark_need_review() -> None:
    fact = fields(body=BUY + SOLD_OUT)["stock"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert fact.value is None


def test_a_page_with_neither_needs_review() -> None:
    fact = fields()["stock"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED


def test_what_only_a_script_says_is_not_stated() -> None:
    scripted = fields(body=BUY + '<script>var msg = "SOLD OUT";</script>')["stock"]
    assert scripted.value is not None
    assert scripted.value.availability is Availability.ON_SALE


# ---------------------------------------------------------------- coverage without fabrication


def test_a_product_with_no_axis_to_choose_states_no_options() -> None:
    # This storefront writes the option container on every product, painted or not. The container
    # alone is not a doubt: a product with an axis writes a select inside it, and this one does
    # not, so the page has stated that there is nothing to choose.
    optionless = (
        '<table><tbody class="xans-element- xans-product xans-product-option">'
        '<tr class="displaynone"><td class="selectButton"><a href="#none">옵션 선택</a></td></tr>'
        "</tbody></table>"
    )
    assert fields(body=optionless)["options"].status is FieldStatus.ABSENT
    assert fields()["options"].status is FieldStatus.ABSENT


def test_stated_option_axes_keep_their_own_values_in_order() -> None:
    # Two axes, and two values that differ only by count and by grade. Neither axis absorbs the
    # other and no value is dropped, merged or reordered; the field itself stays REVIEW_REQUIRED
    # because no accepted evidence shows how this storefront names an axis.
    axes = (
        '<table><tbody class="xans-product-option">'
        "<tr><th>선택</th><td>"
        '<select id="product_option_id1">'
        "<option>1개</option><option>2개</option><option>10개</option></select>"
        '<select id="product_option_id2">'
        "<option>특</option><option>상</option></select>"
        "</td></tr></tbody></table>"
    )
    fact = fields(body=axes)["options"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert fact.value is None, "an axis this storefront has never been seen to name is not a value"
    observed = [evidence.observed for evidence in fact.evidence]
    assert observed == ["1개 / 2개 / 10개", "특 / 상"]


def test_a_cart_line_total_is_not_a_quantity_tier() -> None:
    # The order form carries the line's own total for a quantity the reader has not chosen. It is
    # not a supplier tier, and reading it as one would invent a source total.
    order_form = (
        "<table><tbody><tr><td>[어바틀] 프리미엄 마그네슘 90정</td>"
        '<td><span class="quantity_price">10000</span></td></tr></tbody></table>'
    )
    assert fields(body=order_form)["quantity_tiers"].status is FieldStatus.ABSENT
    assert fields()["quantity_tiers"].status is FieldStatus.ABSENT


def test_a_stated_quantity_tier_is_held_for_review_in_the_page_s_own_words() -> None:
    tiers = (
        "<table><caption>수량별 가격</caption><tbody>"
        "<tr><th>10개</th><td>90,000원</td></tr>"
        "<tr><th>20개</th><td>160,000원</td></tr>"
        "</tbody></table>"
    )
    fact = fields(body=tiers)["quantity_tiers"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert fact.value is None
    observed = " ".join(evidence.observed or "" for evidence in fact.evidence)
    assert "수량별 가격" in observed
    assert "9,000" not in observed and "8,000" not in observed, "a source total is never divided"


@pytest.mark.parametrize(
    ("field_key", "label"),
    [("brand", "브랜드"), ("manufacturer", "제조사"), ("origin", "원산지")],
)
def test_a_labelled_coverage_row_is_read_and_an_absent_one_is_absent(
    field_key: str, label: str
) -> None:
    stated = fields(rows=row(label, "표시값"))[field_key]
    assert stated.status is FieldStatus.CONFIRMED
    assert stated.value is not None and stated.value.text == "표시값"
    assert fields()[field_key].status is FieldStatus.ABSENT


def detail(inner: str) -> str:
    """The detail area in this storefront's own shape: the tab strip, then the content block."""
    return (
        '<div id="prdDetail"><div id="dMenu"><ul>'
        '<li class="selected"><a href="#prdDetail">상품정보</a></li>'
        '<li><a href="#prdInfo">구매안내</a></li>'
        '<li><a href="#prdReview">사용후기</a></li>'
        "</ul></div>"
        f'<div class="cont">{inner}</div></div>'
    )


def test_a_description_of_images_alone_states_no_text() -> None:
    # The retained captures hold exactly this: a description made of images, above it the tab
    # strip the storefront repeats over every panel. The tabs are navigation, so confirming them
    # as the description would record chrome as a source fact.
    images_only = fields(body=detail('<img ec-data-src="//a.invalid/1.jpg">'))
    assert images_only["detail_description"].status is FieldStatus.REVIEW_REQUIRED
    assert images_only["detail_description"].value is None

    with_text = fields(body=detail("<p>제품 설명</p>"))["detail_description"]
    assert with_text.status is FieldStatus.CONFIRMED
    assert with_text.value is not None and with_text.value.text == "제품 설명"
    assert "상품정보" not in with_text.value.text, "the tab strip is not the description"

    assert fields()["detail_description"].status is FieldStatus.ABSENT


def test_every_field_of_the_contract_is_answered() -> None:
    from app.collect.facts import FIELD_REGISTRY, IMAGES_FIELD

    answered = set(fields())
    expected = set(FIELD_REGISTRY) - {IMAGES_FIELD}
    assert answered == expected, "images are derived from the references, never parsed here"
    assert all(
        fact.status in (FieldStatus.CONFIRMED, FieldStatus.ABSENT, FieldStatus.REVIEW_REQUIRED)
        for fact in fields().values()
    )


def test_no_field_is_confirmed_without_evidence() -> None:
    for key, fact in fields(rows=row("판매가", "1,000원"), body=BUY).items():
        assert fact.evidence, key
        if fact.status is FieldStatus.CONFIRMED:
            assert fact.value is not None, key
        else:
            assert fact.value is None, key
