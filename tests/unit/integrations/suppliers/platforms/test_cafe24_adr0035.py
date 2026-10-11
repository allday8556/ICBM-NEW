"""cafe24-3: ADR-0035's readings, one test per invariant, fail closed.

- NR-01: only restricting phrases are read in a product name; an allowed reading never comes
  from a name.
- NR-02: a name restriction beside an all-allowed row is ``REVIEW_REQUIRED``.
- NR-03: one row stating an allowed phrase and restrictions reads as its restrictions, when every
  word of it is read.
- RS-01: a region surcharge never changes the priced fee, and its words are kept.
- QM-01: a per-quantity minimum row yields its ``1개`` amount; any other shape stays held.
- The text fixes of acceptance run 1: invisible characters are no text, a no-break space is a
  space.

Every page is written here; no supplier is contacted.
"""

import pytest

from app.stages.collect.facts import FieldStatus, SalesChannelScope, ShippingKind
from integrations.suppliers.platforms.cafe24 import vocabulary
from integrations.suppliers.platforms.cafe24.collect import Vocabulary, parse_fields
from tests.unit.integrations.suppliers.kmretail.test_km_facts_parser import (
    BUY,
    document,
    page,
    row,
)
from tests.unit.integrations.suppliers.test_site_config import site

WORDS = vocabulary(
    site(
        label_overrides={
            "minimum_price": ["폐쇄몰 최저판매가"],
            "channel_all_allowed": ["모든마켓 판매가능"],
            "channel_closed_only": ["폐쇄몰", "오픈마켓 판매불가"],
            "channel_forbid_coupang": ["쿠팡 판매 금지", "쿠팡 등록 불가", "쿠팡,토스 판매금지"],
            "title_suffix": ["- U-PICK B2B"],
        }
    )
)
ALLOWED = row("판매가능플랫폼", "모든마켓 판매가능")
# The real per-quantity minimum cell (U-PICK 4072). A line break the page collapsed left no
# separator between "40,800원 이상" and "4개".
QUANTITY_CELL = (
    "1개 13,900원 이상/ 2개 27,500원 이상 / 3개 40,800원 이상4개 53,900원 이상 / "
    "5개 66,700원 이상 / 6개 79,200원 이상"
)


def fields(body: str, words: Vocabulary = WORDS) -> dict:
    return parse_fields(document(body), words)


def test_the_region_surcharge_label_is_a_site_slot_with_the_template_s_default() -> None:
    assert Vocabulary().shipping_region_fee == ("추가배송비",)
    extended = vocabulary(site(label_overrides={"shipping_region_fee": ["지역별 추가배송비"]}))
    assert extended.shipping_region_fee == ("추가배송비", "지역별 추가배송비")


# ---------------------------------------------------------------- §1 restrictions in the name


@pytest.mark.parametrize(
    ("name", "name_row"),
    [("마그네슘 모든마켓 판매가능", ""), ("마그네슘", "마그네슘 모든마켓 판매가능")],
    ids=["title", "name row"],
)
def test_nr_01_an_allowed_reading_never_comes_from_a_name(name: str, name_row: str) -> None:
    rows = row("상품명", name_row) if name_row else ""
    channels = fields(page(name=name, rows=rows, body=BUY))["sales_channels"]
    assert channels.status is FieldStatus.ABSENT
    # Beside a restricting row, an allowed phrase in the name is no contradiction: it is unread.
    restricted = fields(page(name=name, rows=rows + row("판매가능플랫폼", "폐쇄몰"), body=BUY))[
        "sales_channels"
    ]
    assert restricted.value.scope is SalesChannelScope.CLOSED_MALL_ONLY


@pytest.mark.parametrize(
    ("name", "name_row"),
    [
        ("마그네슘 - 쿠팡 등록 불가 - U-PICK B2B", "마그네슘 - 쿠팡 등록 불가"),
        ("마그네슘 - 쿠팡판매금지", ""),
        ("마그네슘", "마그네슘 (쿠팡 판매 금지)"),
    ],
    ids=["title and row", "title only, spaces differ", "row only"],
)
def test_nr_02_a_name_restriction_beside_an_all_allowed_row_is_held(
    name: str, name_row: str
) -> None:
    rows = (row("상품명", name_row) if name_row else "") + ALLOWED
    channels = fields(page(name=name, rows=rows, body=BUY))["sales_channels"]
    assert channels.status is FieldStatus.REVIEW_REQUIRED
    assert channels.value is None


def test_a_name_restriction_alone_lists_the_forbidden_marketplace() -> None:
    body = page(
        name="마그네슘 - 쿠팡 등록 불가 - U-PICK B2B",
        rows=row("상품명", "마그네슘 - 쿠팡 등록 불가"),
        body=BUY,
    )
    channels = fields(body)["sales_channels"]
    assert channels.status is FieldStatus.CONFIRMED
    assert (channels.value.scope, channels.value.forbidden) == (
        SalesChannelScope.LISTED,
        ("coupang",),
    )
    assert channels.value.policy_text == "쿠팡 등록 불가"
    assert {e.locator for e in channels.evidence} == {"meta[og:title]", "th:상품명 + td"}


def test_a_closed_mall_phrase_in_the_name_is_closed_mall_only() -> None:
    channels = fields(page(name="[폐쇄몰] 마그네슘", body=BUY))["sales_channels"]
    assert channels.value.scope is SalesChannelScope.CLOSED_MALL_ONLY


def test_a_site_without_restricting_words_reads_nothing_from_a_name() -> None:
    body = page(name="마그네슘 - 쿠팡 등록 불가", body=BUY)
    assert fields(body, vocabulary(site()))["sales_channels"].status is FieldStatus.ABSENT


# ---------------------------------------------------------------- §1 NR-03 one row stating both

MIXED = "모든마켓 판매가능/쿠팡,토스 판매금지"


def test_nr_03_a_row_stating_both_reads_as_its_restrictions() -> None:
    channels = fields(page(rows=row("판매가능플랫폼", MIXED), body=BUY))["sales_channels"]
    assert channels.status is FieldStatus.CONFIRMED
    assert (channels.value.scope, channels.value.forbidden) == (
        SalesChannelScope.LISTED,
        ("coupang",),
    )
    # 토스 is no marketplace ICBM lists: its words stay for a later one.
    assert channels.value.policy_text == MIXED


@pytest.mark.parametrize(
    "value",
    [
        "모든마켓 판매가능/쿠팡 판매 금지/스마트스토어 등록 불가",
        "모든마켓 판매가능 · 쿠팡 판매 금지 (일부 제외)",
        "쿠팡 판매 금지/스마트스토어 등록 불가",
    ],
    ids=["unconfigured ban", "other words", "restricting row with an unconfigured ban"],
)
def test_nr_03_words_no_phrase_reads_hold_the_row(value: str) -> None:
    channels = fields(page(rows=row("판매가능플랫폼", value), body=BUY))["sales_channels"]
    assert channels.status is FieldStatus.REVIEW_REQUIRED
    assert channels.value is None


def test_nr_03_separators_alone_are_set_aside() -> None:
    value = "모든마켓 판매가능 · 쿠팡 판매 금지 / 오픈마켓 판매불가"
    channels = fields(page(rows=row("판매가능플랫폼", value), body=BUY))["sales_channels"]
    assert channels.value.scope is SalesChannelScope.CLOSED_MALL_ONLY


def test_nr_03_a_row_stating_both_beside_a_wholly_allowed_row_is_held() -> None:
    rows = row("판매가능플랫폼", MIXED) + ALLOWED
    assert fields(page(rows=rows, body=BUY))["sales_channels"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


# ---------------------------------------------------------------- §2 region surcharge

REGION_WORDS = "제주도 3,000원/도서산간 5,000원 추가"


@pytest.mark.parametrize(
    ("fee", "kind", "fee_krw"),
    [
        ("3,000원", ShippingKind.FIXED, 3000),
        ("무료", ShippingKind.FREE, None),
        ("3,000원 ~ 4,000원", ShippingKind.FIXED, 4000),
    ],
)
def test_rs_01_a_region_surcharge_is_kept_as_words_and_never_priced(
    fee: str, kind: ShippingKind, fee_krw: int | None
) -> None:
    body = page(rows=row("배송비", fee) + row("추가배송비", REGION_WORDS), body=BUY)
    shipping = fields(body)["shipping"]
    assert shipping.status is FieldStatus.CONFIRMED
    assert (shipping.value.kind, shipping.value.fee_krw) == (kind, fee_krw)
    assert shipping.value.policy_text == f"배송비 {fee} / 추가배송비 {REGION_WORDS}"
    assert "th:추가배송비 + td" in {e.locator for e in shipping.evidence}


def test_rs_01_the_same_surcharge_stated_twice_is_one_statement() -> None:
    body = page(
        rows=row("배송비", "3,000원")
        + row("추가배송비", REGION_WORDS)
        + row("추가배송비", REGION_WORDS),
        body=BUY,
    )
    shipping = fields(body)["shipping"]
    assert shipping.status is FieldStatus.CONFIRMED
    assert shipping.value.policy_text.count("추가배송비") == 1


@pytest.mark.parametrize(
    "rows",
    [
        row("배송비", "3,000원") + row("추가배송비", "제주도 안내 https://example.com/zone"),
        row("배송비", "3,000원") + row("추가배송비", "제주도 //example.com"),
        row("배송비", "3,000원")
        + row("추가배송비", REGION_WORDS)
        + row("추가배송비", "제주도 4,000원/도서산간 5,000원 추가"),
        row("추가배송비", REGION_WORDS),
        row("배송비", "3,000원") + row("추가배송비", '<img src="/web/upload/zone.jpg">'),
        row("배송비", "3,000원") + row("추가배송비", "<span><img src='/zone.png'></span>"),
        row("배송비", "3,000원") + row("추가배송비", "제주도 3,000원 추가 " * 15),
    ],
    ids=[
        "url",
        "protocol-relative url",
        "two different rows",
        "no fee row",
        "image only",
        "wrapped image only",
        "over 200 characters",
    ],
)
def test_rs_01_a_surcharge_that_cannot_be_kept_as_words_is_held(rows: str) -> None:
    shipping = fields(page(rows=rows, body=BUY))["shipping"]
    assert shipping.status is FieldStatus.REVIEW_REQUIRED
    assert shipping.value is None
    assert "example.com" not in repr(shipping.evidence)


@pytest.mark.parametrize("cell", ["", "<span></span>", "<span>\ufeff</span><br>"])
def test_an_empty_surcharge_cell_states_no_surcharge(cell: str) -> None:
    # cafe24-3's choice: a surcharge row whose cell holds nothing (or only invisible characters)
    # is read as no row; only a cell holding an image or another element without words is held.
    body = page(rows=row("배송비", "3,000원") + row("추가배송비", cell), body=BUY)
    shipping = fields(body)["shipping"]
    assert shipping.status is FieldStatus.CONFIRMED
    assert shipping.value.policy_text == "배송비 3,000원"


def test_a_surcharge_of_exactly_200_characters_is_kept() -> None:
    words = "가" * 200
    body = page(rows=row("배송비", "3,000원") + row("추가배송비", words), body=BUY)
    assert fields(body)["shipping"].value.policy_text.endswith(words)


# ---------------------------------------------------------------- §3 per-quantity minimum


def test_qm_01_a_per_quantity_minimum_row_reads_its_one_unit_amount() -> None:
    body = page(rows=row("폐쇄몰 최저판매가", QUANTITY_CELL), body=BUY)
    minimum = fields(body)["minimum_sale_price"]
    assert minimum.status is FieldStatus.CONFIRMED
    assert (minimum.value.label, minimum.value.amount_krw) == ("폐쇄몰 최저판매가", 13900)
    assert minimum.evidence[0].observed == QUANTITY_CELL
    assert minimum.evidence[0].normalized == "13900"


@pytest.mark.parametrize(
    "cell",
    [
        "1개 13,900원 이상",
        "1개 13,900원 이상, 2개 27,500원 이상",
        "1개 13,900원 이상\n2개 27,500원 이상",
    ],
    ids=["one entry", "comma", "line break"],
)
def test_qm_01_every_separator_is_read(cell: str) -> None:
    body = page(rows=row("최저판매가", cell), body=BUY)
    assert fields(body)["minimum_sale_price"].value.amount_krw == 13900


@pytest.mark.parametrize(
    "cell",
    [
        "2개 27,500원 이상 / 1개 13,900원 이상",
        "1개 13,900원 이상 / 2개 27,500원 이상 (택배)",
        "1개 13,900원 이상 / 2개 27,500원 이상 / 2개 26,000원 이상",
        "1개 13,900원 이상 / 1개 12,000원 이상",
        "1개 13,900원 이상 / 27,500원",
        "1개 13,900원 / 2개 27,500원 이상",
        "1개 13,900원 이상 // 2개 27,500원 이상",
        "1개 0원 이상",
        "1개 0원 이상 / 2개 27,500원 이상",
    ],
    ids=[
        "not 1개 first",
        "other words",
        "repeated quantity",
        "1개 twice",
        "unaccounted amount",
        "no 이상",
        "two separators",
        "zero alone",
        "zero first",
    ],
)
def test_qm_01_any_other_minimum_row_shape_is_held(cell: str) -> None:
    minimum = fields(page(rows=row("최저판매가", cell), body=BUY))["minimum_sale_price"]
    assert minimum.status is FieldStatus.REVIEW_REQUIRED
    assert minimum.value is None


@pytest.mark.parametrize(("cell", "status"), [("7,900원", "CONFIRMED"), ("자율", "ABSENT")])
def test_the_single_amount_and_no_minimum_readings_are_unchanged(cell: str, status: str) -> None:
    minimum = fields(page(rows=row("최저판매가", cell), body=BUY))["minimum_sale_price"]
    assert minimum.status.value == status


# ---------------------------------------------------------------- text fixes of run 1


@pytest.mark.parametrize("words", ["\ufeff\ufeff", "\u200b", "\u2060\u200b", "&#xFEFF;", "\u00a0"])
def test_a_description_of_invisible_characters_only_is_empty(words: str) -> None:
    description = fields(page(body=BUY + f'<div id="prdDetail"><p>{words}</p></div>'))[
        "detail_description"
    ]
    assert description.status is FieldStatus.REVIEW_REQUIRED


def test_invisible_characters_inside_words_are_dropped() -> None:
    body = page(body=BUY + '<div id="prdDetail"><p>\ufeff설명\u200b입니다\u00a0끝</p></div>')
    assert fields(body)["detail_description"].value.text == "설명입니다 끝"


def test_joiners_are_kept_because_they_change_what_is_shown() -> None:
    # A zero-width joiner builds an emoji sequence; a non-joiner shapes a script's letters.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    body = page(name=f"{family} 가족 세트 \u200c", body=BUY)
    assert fields(body)["original_name"].value.text == f"{family} 가족 세트 \u200c"


def test_a_row_of_only_invisible_characters_is_an_empty_cell() -> None:
    # The row is not stated, as an empty cell never was: no minimum, read from the page.
    body = page(rows=row("최저판매가", "\ufeff"), body=BUY)
    assert fields(body)["minimum_sale_price"].status is FieldStatus.ABSENT


NAME_4992 = "네이처그랜드 이너포에버 갱년기 앤 유산균 500mg x 60캡슐"


@pytest.mark.parametrize(
    "title",
    [
        f"{NAME_4992}\u00a0 - 쿠팡 등록 불가 - U-PICK B2B",
        f"{NAME_4992} \u00a0- 쿠팡 등록 불가 - U-PICK B2B",
        f"{NAME_4992}&nbsp; - 쿠팡 등록 불가 - U-PICK B2B",
        f"{NAME_4992}  - 쿠팡 등록 불가 - U-PICK B2B",
    ],
    ids=["nbsp then space", "space then nbsp", "nbsp entity", "two spaces"],
)
def test_4992_a_title_with_a_doubled_space_reads_as_its_row(title: str) -> None:
    # cafe24-2 compared the raw title with the cleaned row plus the suffix, so these never
    # matched and the name was held for review (acceptance run 1).
    for name_row in (f"{NAME_4992}\u00a0 - 쿠팡 등록 불가", f"{NAME_4992} - 쿠팡 등록 불가"):
        body = page(name=title, rows=row("상품명", name_row), body=BUY)
        name = fields(body)["original_name"]
        assert name.status is FieldStatus.CONFIRMED, (title, name_row)
        assert name.value.text == f"{NAME_4992} - 쿠팡 등록 불가"
        assert name.evidence[0].locator == "th:상품명 + td"


def test_a_title_that_differs_beyond_its_spaces_still_disagrees() -> None:
    body = page(
        name=f"{NAME_4992}  - 쿠팡 등록 불가 2개 - U-PICK B2B",
        rows=row("상품명", f"{NAME_4992} - 쿠팡 등록 불가"),
        body=BUY,
    )
    assert fields(body)["original_name"].status is FieldStatus.REVIEW_REQUIRED


@pytest.mark.parametrize(
    "material",
    [
        "https://example.com/x?token=abc",
        "www.example.com/x?token=abc",
        "example.co.kr/x?token=abc",
    ],
)
def test_url_shaped_region_words_are_held_and_never_quoted(material: str) -> None:
    # ADR-0035 §2: the surcharge row is held by its locator alone; the URL never reaches evidence.
    body = page(
        rows=row("배송비", "3,000원") + row("추가배송비", f"제주 {material} 추가"), body=BUY
    )
    fact = fields(body)["shipping"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert "example" not in repr(fact.evidence)
    assert all(evidence.observed is None for evidence in fact.evidence)
