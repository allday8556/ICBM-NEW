"""The Cafe24 template against KM통상's parser (ADR-0030 §4, §10).

The template is derived from KM통상's parser, so on every document KM통상's own tests use it must
read the same identities and assign the same image roles in the same order (only the rule names
say ``cafe24``). For each fact it must give the same status and value, or a stated difference that
fails closed (the ``facts`` module lists them). Evidence is not compared: the template writes its
own locators. Every fixture is written here or is an existing synthetic one; no supplier is
contacted.

``cafe24-3`` (ADR-0035) adds differences that are words only or the owner's rule, each proven on a
corpus page of its own shape:
- a region surcharge (``추가배송비``) is kept after KM통상's shipping policy text, with the same
  kind and fee, and the field is held for review when it cannot be kept as words;
- text is cleaned of invisible characters and a no-break space is a space: a description made only
  of invisible characters is held for review, and a title's doubled space reads as one;
- a per-quantity minimum row reads its ``1개`` amount, where KM통상 reads the quantity 1.
"""

from dataclasses import asdict
from pathlib import Path

import pytest

from app.stages.collect.facts import (
    FieldStatus,
    MoneyValue,
    ShippingKind,
    ShippingValue,
    TextValue,
)
from integrations.suppliers.kmretail.collect import classify_images as km_classify
from integrations.suppliers.kmretail.collect import parse_fields as km_fields
from integrations.suppliers.kmretail.collect import resolve as km_resolve
from integrations.suppliers.platforms.cafe24 import authenticated, login_required, vocabulary
from integrations.suppliers.platforms.cafe24.collect import (
    Vocabulary,
    classify_images,
    parse_fields,
    resolve,
)
from integrations.suppliers.site_config import SiteRegion
from tests.unit.integrations.suppliers.kmretail import test_km_image_roles as km_images
from tests.unit.integrations.suppliers.kmretail.test_km_facts_parser import (
    BUY,
    SOLD_OUT,
    URL,
    document,
    page,
    row,
)
from tests.unit.integrations.suppliers.test_site_config import site

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures"

CORPUS = {
    "plain": page(rows=row("판매가", "12,000원") + row("상품명", "마그네슘"), body=BUY),
    "sold_out": page(rows=row("판매가", "12,000원"), body=SOLD_OUT),
    "both_states": page(rows=row("판매가", "12,000원"), body=BUY + SOLD_OUT),
    "hidden_rows": page(
        rows=row("판매가", "9,900원", hidden=True)
        + row("소비자가", "15,000원")
        + row("최저지도가", "자율")
        + row("배송방법", "택배")
        + row("배송비", "3,000원")
        + row("브랜드", "어바틀")
        + row("제조사", "원바이오")
        + row("원산지", "국산"),
        body=BUY,
    ),
    "minimum_amount": page(rows=row("최저지도가", "8,000원") + row("배송비", "무료"), body=BUY),
    "options": page(
        body=BUY
        + '<div class="xans-product-option"><select><option>- 선택 -</option>'
        + "<option>90정</option><option>180정</option></select></div>"
    ),
    "tiers": page(rows=row("수량별 가격", "10개 이상 5% 할인"), body=BUY),
    "detail_text": page(
        body=BUY + '<div id="prdDetail"><ul class="dMenu"><li>상세</li></ul><p>설명입니다</p></div>'
    ),
    "detail_images": page(body=BUY + '<div id="prdDetail"><img src="/web/a.jpg"></div>'),
    "no_number": page(number=""),
    "disagree": page(retailer_item="356"),
    "no_canonical": page(canonical=None),
    "script_only": page(body=BUY + "<script>var price='판매가 1원'</script>"),
    # ADR-0035's shapes (cafe24-3), so each stated difference below is read, not only claimed.
    "region_surcharge": page(
        rows=row("배송비", "3,000원") + row("추가배송비", "제주도 3,000원/도서산간 5,000원 추가"),
        body=BUY,
    ),
    "per_quantity_minimum": page(
        rows=row("최저지도가", "1개 13,900원 이상/ 2개 27,500원 이상 / 3개 40,800원 이상"),
        body=BUY,
    ),
    "invisible_description": page(body=BUY + '<div id="prdDetail"><p>&#xFEFF;&#xFEFF;</p></div>'),
    "spaced_title": page(name="마그네슘&nbsp; 90정"),
}
FILES = {
    path.name: path.read_text("utf-8")
    for path in (
        FIXTURES / "extension" / "km_product.html",
        *sorted((FIXTURES / "adaptive" / "pages").glob("*.html")),
    )
}


@pytest.mark.parametrize("body", [*CORPUS.values(), *FILES.values()], ids=[*CORPUS, *FILES])
def test_the_template_reads_every_km_document_as_km_does(body: str) -> None:
    view = document(body)
    ours, theirs = resolve(view, URL), km_resolve(view, URL)
    # Each package has its own identity types, so they are compared by kind and content.
    assert (type(ours).__name__, asdict(ours)) == (type(theirs).__name__, asdict(theirs))
    ours_fields, km = parse_fields(view), km_fields(view)
    assert set(ours_fields) == set(km)
    for key, theirs_fact in km.items():
        ours_fact = ours_fields[key]
        if (ours_fact.status, ours_fact.value) == (theirs_fact.status, theirs_fact.value):
            continue
        # The template's stated differences (ADR-0030 §10, the module docstring) only ever hold
        # a field for review where KM통상 decided, or follow ADR-0010 §10 for stock.
        stock_rule = key == "stock" and theirs_fact.status is FieldStatus.REVIEW_REQUIRED
        whole_text = (
            ours_fact.status is FieldStatus.CONFIRMED
            and isinstance(ours_fact.value, TextValue)
            and isinstance(theirs_fact.value, TextValue)
            and ours_fact.value.text.startswith(theirs_fact.value.text)
        )
        # A fee cell that is exactly 무료 states free shipping, where KM통상 finds no amount in it.
        free_fee = (
            key == "shipping"
            and isinstance(ours_fact.value, ShippingValue)
            and ours_fact.value.kind is ShippingKind.FREE
        )
        # cafe24-3 (ADR-0035), words only: a region surcharge's words follow KM통상's policy text,
        # with the same kind and fee; and a title's no-break or doubled space is one space.
        surcharge_words = (
            key == "shipping"
            and isinstance(ours_fact.value, ShippingValue)
            and isinstance(theirs_fact.value, ShippingValue)
            and (ours_fact.value.kind, ours_fact.value.fee_krw)
            == (theirs_fact.value.kind, theirs_fact.value.fee_krw)
            and ours_fact.value.policy_text.startswith(f"{theirs_fact.value.policy_text} / ")
        )
        spaces_only = (
            isinstance(ours_fact.value, TextValue)
            and isinstance(theirs_fact.value, TextValue)
            and ours_fact.value.text == " ".join(theirs_fact.value.text.split())
        )
        # cafe24-3 (ADR-0035 §3): a per-quantity minimum row reads its 1개 amount, where KM통상
        # reads the cell's first number, the quantity 1.
        one_unit_minimum = (
            key == "minimum_sale_price"
            and isinstance(ours_fact.value, MoneyValue)
            and isinstance(theirs_fact.value, MoneyValue)
            and theirs_fact.value.amount_krw == 1
            and (ours_fact.evidence[0].observed or "").startswith("1개 ")
        )
        assert (
            ours_fact.status is FieldStatus.REVIEW_REQUIRED
            or stock_rule
            or whole_text
            or free_fee
            or surcharge_words
            or spaces_only
            or one_unit_minimum
        ), key


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("region_surcharge", "shipping"),
        ("per_quantity_minimum", "minimum_sale_price"),
        ("invisible_description", "detail_description"),
        ("spaced_title", "original_name"),
    ],
)
def test_each_adr_0035_corpus_page_exercises_its_stated_difference(name: str, key: str) -> None:
    view = document(CORPUS[name])
    ours, theirs = parse_fields(view)[key], km_fields(view)[key]
    assert (ours.status, ours.value) != (theirs.status, theirs.value)


@pytest.mark.parametrize(
    "body", [km_images.PAGE, *CORPUS.values(), *FILES.values()], ids=["roles", *CORPUS, *FILES]
)
def test_the_template_assigns_the_same_image_roles(body: str) -> None:
    ours = classify_images(body, URL)
    theirs = km_classify(body, URL)
    assert [(c.url, c.role, c.order, c.source) for c in ours] == [
        (c.url, c.role, c.order, c.source) for c in theirs
    ]
    assert [c.rule.replace("cafe24.", "km.") for c in ours] == [c.rule for c in theirs]


def test_a_site_word_is_read_only_for_the_site_that_names_it() -> None:
    body = page(rows=row("도매가", "7,000원"), body=BUY)
    assert parse_fields(document(body))["prices"].status is FieldStatus.ABSENT
    words = vocabulary(site(label_overrides={"price": ["도매가"]}))
    prices = parse_fields(document(body), words)["prices"]
    assert prices.status is FieldStatus.CONFIRMED
    assert [(p.label, p.amount_krw) for p in prices.value.prices] == [("도매가", 7000)]


def test_site_words_extend_the_template_s_and_never_replace_them() -> None:
    words = vocabulary(site(label_overrides={"price": ["도매가", "판매가"]}))
    assert words.price == ("판매가", "소비자가", "정가", "공급가", "도매가")


def test_a_relocated_description_region_is_read_where_the_site_puts_it() -> None:
    body = page(body=BUY + '<div id="goodsDetail"><p>다른 스킨의 설명</p></div>')
    assert parse_fields(document(body))["detail_description"].status is FieldStatus.ABSENT
    words = vocabulary(site(region_overrides={"detail": SiteRegion("id", "goodsDetail")}))
    assert words == Vocabulary(detail_container="#goodsDetail")
    fact = parse_fields(document(body), words)["detail_description"]
    assert fact.status is FieldStatus.CONFIRMED
    images = classify_images(
        page(body='<div id="goodsDetail"><img src="/web/d.jpg"></div>'), URL, words
    )
    assert [c.role.value for c in images] == ["DETAIL"]


def test_an_extra_sold_out_word_reads_as_sold_out() -> None:
    body = page(body='<div class="xans-product-action"><span>재고없음</span></div>')
    words = vocabulary(site(label_overrides={"sold_out": ["재고없음"]}))
    assert parse_fields(document(body))["stock"].status is FieldStatus.REVIEW_REQUIRED
    assert parse_fields(document(body), words)["stock"].value.availability.value == "SOLD_OUT"


def test_connect_predicates_are_km_s() -> None:
    from integrations.suppliers.base import ProbeResponse
    from integrations.suppliers.kmretail import authenticated as km_authenticated
    from integrations.suppliers.kmretail import login_required as km_login_required

    for body in (
        '<div class="xans-layout-statelogon"></div>'
        '<a href="/exec/front/Member/logout/">로그아웃</a>',
        '<div class="xans-layout-statelogoff"></div>toMoveLoginCheckModule',
        "",
    ):
        for status in (200, 302, 403):
            response = ProbeResponse(
                status=status, path="/myshop/index.html", location=None, body=body
            )
            assert authenticated(response) == km_authenticated(response)
            ours, theirs = login_required(response), km_login_required(response)
            # Stated difference (ADR-0007 §4): a denial alone proves nothing for the template.
            if theirs[1] == ("access_denied",):
                assert ours == (False, ("access_denied",))
            else:
                assert ours[0] == theirs[0] and set(ours[1]) == set(theirs[1])


def test_a_fee_cell_with_a_condition_is_never_flattened_into_its_first_amount() -> None:
    # ADR-0010 §7; the one stated difference from KM통상's parser (ADR-0030 §10).
    for cell in ("3,000원 (50,000원 이상 구매 시 무료)", "50,000원 이상 무료"):
        shipping = parse_fields(document(page(rows=row("배송비", cell), body=BUY)))["shipping"]
        assert shipping.status is FieldStatus.REVIEW_REQUIRED, cell
        assert shipping.value is None
    assert (
        parse_fields(document(page(rows=row("배송비", "3,000원"), body=BUY)))["shipping"].status
        is FieldStatus.CONFIRMED
    )


def test_a_fee_range_is_read_as_its_highest_amount() -> None:
    # ADR-0032 §4, the owner's rule (cafe24-2).
    shipping = parse_fields(document(page(rows=row("배송비", "3,000원 ~ 5,000원"), body=BUY)))[
        "shipping"
    ]
    assert shipping.status is FieldStatus.CONFIRMED
    assert shipping.value.fee_krw == 5000


def test_an_active_purchase_control_decides_on_sale_beside_sold_out_words() -> None:
    # ADR-0010 §10: BUY/CART active is ON_SALE; sold-out words beside it are not authoritative.
    stock = parse_fields(document(page(body=BUY + SOLD_OUT)))["stock"]
    assert stock.status is FieldStatus.CONFIRMED
    assert stock.value.availability.value == "ON_SALE"
    neither = parse_fields(document(page()))["stock"]
    assert neither.status is FieldStatus.REVIEW_REQUIRED


def test_declarations_that_disagree_are_held_for_review() -> None:
    renamed = page(name="다른 이름", rows=row("상품명", "마그네슘"), body=BUY)
    assert parse_fields(document(renamed))["original_name"].status is FieldStatus.REVIEW_REQUIRED
    same = page(name="마그네슘", rows=row("상품명", "마그네슘"), body=BUY)
    assert parse_fields(document(same))["original_name"].status is FieldStatus.CONFIRMED
    two_prices = page(rows=row("판매가", "12,000원") + row("판매가", "13,000원"), body=BUY)
    assert parse_fields(document(two_prices))["prices"].status is FieldStatus.REVIEW_REQUIRED
    two_brands = page(rows=row("브랜드", "가") + row("브랜드", "나"), body=BUY)
    assert parse_fields(document(two_brands))["brand"].status is FieldStatus.REVIEW_REQUIRED


def test_a_shown_notice_is_held_for_review_and_an_unshown_one_is_absent() -> None:
    shown = page(rows=row("상품정보제공고시", "식품위생법에 따른 표시"), body=BUY)
    assert parse_fields(document(shown))["notice"].status is FieldStatus.REVIEW_REQUIRED
    assert parse_fields(document(page(body=BUY)))["notice"].status is FieldStatus.ABSENT


def test_a_shipping_method_without_a_fee_is_held_for_review_coherently() -> None:
    fields = parse_fields(document(page(rows=row("배송방법", "택배"), body=BUY)))
    shipping = fields["shipping"]
    assert shipping.status is FieldStatus.REVIEW_REQUIRED
    assert FieldStatus.REVIEW_REQUIRED in {e.status for e in shipping.evidence}


def test_a_long_description_is_kept_whole() -> None:
    words = "설명 " * 300
    body = page(body=BUY + f'<div id="prdDetail"><p>{words}</p></div>')
    fact = parse_fields(document(body))["detail_description"]
    assert fact.value.text == words.strip()
    assert len(fact.evidence[0].observed) <= 200


@pytest.mark.parametrize(
    ("cell", "amount"),
    [("12,000원", 12000), ("16900", 16900), ("2,900원63% (부가세포함)", 2900), ("문의", None)],
)
def test_a_price_row_states_exactly_one_amount_or_holds_the_prices(
    cell: str, amount: int | None
) -> None:
    prices = parse_fields(document(page(rows=row("판매가", cell), body=BUY)))["prices"]
    if amount is None:
        assert prices.status is FieldStatus.REVIEW_REQUIRED
    else:
        assert prices.value.prices[0].amount_krw == amount
    several = page(rows=row("판매가", "12,000원 → 9,900원"), body=BUY)
    assert parse_fields(document(several))["prices"].status is FieldStatus.REVIEW_REQUIRED


def test_a_denial_alone_never_proves_a_login_is_required() -> None:
    from integrations.suppliers.base import ProbeResponse

    denied = ProbeResponse(status=403, path="/myshop/index.html", location=None, body="")
    assert login_required(denied) == (False, ("access_denied",))
