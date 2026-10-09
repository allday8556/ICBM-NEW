"""The Cafe24 template against KM통상's parser (ADR-0030 §4, §10).

The template is derived from KM통상's parser, so with the default vocabulary it must read every
document KM통상's own tests use exactly as KM통상's parser does: the same identities, the same facts
and evidence, and the same image roles in the same order (only the rule names say ``cafe24``).
Every fixture is written here or is an existing synthetic fixture; no supplier is contacted.
"""

from dataclasses import asdict
from pathlib import Path

import pytest

from app.stages.collect.facts import FieldStatus
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
    assert {k: v for k, v in ours_fields.items() if k != "shipping"} == {
        k: v for k, v in km.items() if k != "shipping"
    }
    # The one stated difference (ADR-0030 §10): a fee cell that is not exactly one amount is
    # REVIEW_REQUIRED, with evidence that says so, where KM통상 reads its first amount.
    if ours_fields["shipping"] != km["shipping"]:
        assert ours_fields["shipping"].status is FieldStatus.REVIEW_REQUIRED
        assert ours_fields["shipping"].value is None
        assert FieldStatus.REVIEW_REQUIRED in {e.status for e in ours_fields["shipping"].evidence}


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
    assert words == Vocabulary(detail_container="goodsDetail")
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
            assert login_required(response) == km_login_required(response)


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
