"""cafe24-2's rules fail closed (ADR-0031 §3, ADR-0032 §4, ADR-0010 §7).

Every page is written here."""

import pytest

from app.stages.collect.facts import FieldStatus, SalesChannelScope, ShippingKind
from integrations.suppliers.kmretail.collect import parse_fields as km_fields
from integrations.suppliers.platforms.cafe24 import vocabulary
from integrations.suppliers.platforms.cafe24.collect import parse_fields
from tests.unit.integrations.suppliers.kmretail.test_km_facts_parser import (
    BUY,
    document,
    page,
    row,
)
from tests.unit.integrations.suppliers.test_site_config import site

WORDED = vocabulary(
    site(
        label_overrides={
            "channel_all_allowed": ["모든마켓 판매가능"],
            "channel_closed_only": ["폐쇄몰", "오픈마켓 판매불가"],
            "channel_forbid_coupang": ["쿠팡판매 불가"],
            "title_suffix": ["- U-PICK B2B"],
        }
    )
)
UNWORDED = vocabulary(site())


@pytest.mark.parametrize(
    "cell",
    [
        "3,000원 ~ 50,000원 이상 무료",
        "3,000원 ~ 4,000원 (50,000원 이상 구매 시 무료)",
        "3,000원~4,000원 / 제주 7,000원",
        "2,500원~3,000원 조건부무료",
    ],
)
def test_a_range_with_anything_more_is_a_condition(cell: str) -> None:
    shipping = parse_fields(document(page(rows=row("배송비", cell), body=BUY)), WORDED)["shipping"]
    assert shipping.status is FieldStatus.REVIEW_REQUIRED


def test_an_exact_range_names_its_reading() -> None:
    for cell in ("3,000원 ~ 4,000원", "3,000원～4,000원"):
        shipping = parse_fields(document(page(rows=row("배송비", cell), body=BUY)))["shipping"]
        assert (shipping.value.kind, shipping.value.fee_krw) == (ShippingKind.FIXED, 4000)
        assert shipping.evidence[-1].normalized == "4000"


def test_a_site_without_channel_words_still_holds_a_channel_row() -> None:
    body = page(rows=row("판매가능플랫폼", "폐쇄몰"), body=BUY)
    assert parse_fields(document(body), UNWORDED)["sales_channels"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


@pytest.mark.parametrize("value", ["모든마켓 판매가능 (쿠팡 제외)", "모든마켓 판매가능 상품 아님"])
def test_all_allowed_is_read_only_from_a_whole_row(value: str) -> None:
    body = page(rows=row("판매가능플랫폼", value), body=BUY)
    assert parse_fields(document(body), WORDED)["sales_channels"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


def test_every_channel_row_must_be_read() -> None:
    body = page(
        rows=row("판매가능플랫폼", "모든마켓 판매가능")
        + row("판매가능플랫폼", "스마트스토어 판매금지"),
        body=BUY,
    )
    assert parse_fields(document(body), WORDED)["sales_channels"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


def test_a_channel_row_holding_only_an_image_is_held_for_review() -> None:
    body = page(
        rows='<tr><th>판매가능플랫폼</th><td><img src="/closed.png" alt="폐쇄몰"></td></tr>',
        body=BUY,
    )
    assert parse_fields(document(body), WORDED)["sales_channels"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


def test_the_page_s_own_words_are_the_policy_text() -> None:
    body = page(rows=row("판매가능플랫폼", "폐쇄몰 / 오픈마켓 판매불가"), body=BUY)
    fact = parse_fields(document(body), WORDED)["sales_channels"]
    assert fact.value.scope is SalesChannelScope.CLOSED_MALL_ONLY
    assert fact.value.policy_text == "폐쇄몰 / 오픈마켓 판매불가"


def test_a_restricting_row_with_words_no_phrase_reads_is_held() -> None:
    # cafe24-3 (ADR-0035 NR-03, applied to every row that is not wholly allowed): every word of a
    # restricting row must be read. cafe24-2 read this row CLOSED_MALL_ONLY with 전용 unread.
    body = page(rows=row("판매가능플랫폼", "폐쇄몰 전용/오픈마켓 판매불가"), body=BUY)
    assert parse_fields(document(body), WORDED)["sales_channels"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


def test_only_the_site_s_own_title_suffix_is_set_aside() -> None:
    suffixed = page(name="마그네슘 - U-PICK B2B", rows=row("상품명", "마그네슘"), body=BUY)
    assert parse_fields(document(suffixed), WORDED)["original_name"].value.text == "마그네슘"
    pack = page(name="마그네슘 90정 2개입", rows=row("상품명", "마그네슘"), body=BUY)
    assert parse_fields(document(pack), WORDED)["original_name"].status is (
        FieldStatus.REVIEW_REQUIRED
    )


@pytest.mark.parametrize(
    ("cell", "fee"),
    [
        ("3,000원 (도서산간 3,000원~5,000원 추가)", 3000),
        ("3,000원 ~ 50,000원 이상 무료", 3000),
        ("3,000원～4,000원", 4000),
        ("3,000원 ~ 4,000원", 4000),
    ],
)
def test_km_reads_a_range_only_when_the_cell_is_one(cell: str, fee: int) -> None:
    shipping = km_fields(document(page(rows=row("배송비", cell), body=BUY)))["shipping"]
    assert shipping.value.fee_krw == fee


def test_an_unrelocated_key_image_region_keeps_km_s_reading() -> None:
    assert UNWORDED.key_image == "keyImg"
