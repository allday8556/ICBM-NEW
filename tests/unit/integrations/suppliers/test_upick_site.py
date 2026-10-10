"""U-PICK's site configuration as this build binds it (ADR-0030 §8.2: the provider-zero proof).

The site is read through the registry, exactly as COLLECT and CONNECT will use it, on the two
reduced reconnaissance captures (Issue #219 `6090189930`), and on minimal synthetic pages in the
same shape that state what acceptance run 1 found on products 4992, 4072 and 4837 (ADR-0035,
Issue #219 `6097181737`). No supplier is contacted.
"""

from pathlib import Path

import pytest

from app.stages.collect.facts import (
    Availability,
    FieldStatus,
    PriceRole,
    SalesChannelScope,
    ShippingKind,
)
from integrations.suppliers.collection import DocumentView, ImageRole, ReadKind, SourceIdentity
from integrations.suppliers.registry import COLLECTIONS, SITE_PROBLEMS, SITES, SUPPLIERS
from integrations.suppliers.site_config import SiteStatus

CAPTURES = Path(__file__).resolve().parents[3] / "fixtures" / "suppliers" / "upick"
URL = "https://upickb2b.com/product/x/{number}/category/1/display/4/"


def capture(number: str) -> DocumentView:
    body = (CAPTURES / f"{number}.html").read_text("utf-8")
    return DocumentView(
        ReadKind.PRODUCT_READ, 200, f"/product/x/{number}/", None, "text/html", body
    )


def test_upick_is_bound_in_recon_and_offered_to_connect_and_collect() -> None:
    assert SITE_PROBLEMS == ()
    site = SITES["upick"]
    assert site.config.status is SiteStatus.RECON
    assert site.template.revision == "cafe24-3"
    assert site.extractor_revision == f"{site.template.revision}+upick-2"
    assert site.collection in COLLECTIONS
    assert site.definition in SUPPLIERS
    assert site.definition.profile.egress_hosts == {"upickb2b.com", "login2.cafe24ssl.com"}
    assert site.collection.profile.image_hosts == {"upickb2b.com", "thepath.cafe24.com"}


def test_the_site_reads_its_on_sale_capture() -> None:
    collection = SITES["upick"].collection
    view = capture("4954")
    identity = collection.identity(view, URL.format(number="4954"))
    assert isinstance(identity, SourceIdentity)
    assert identity.source_product_id == "4954"
    fields = collection.fields(view)
    assert fields["original_name"].value.text == "웰러스 비타민C 500 2g x 20포"
    assert fields["prices"].value.purchase().amount_krw == 2900
    assert fields["minimum_sale_price"].value.amount_krw == 7900
    assert (fields["shipping"].value.kind, fields["shipping"].value.fee_krw) == (
        ShippingKind.FIXED,
        3000,
    )
    # ADR-0035 §2: the capture's region surcharge is kept as words, never priced.
    assert fields["shipping"].value.policy_text == (
        "배송비 3,000원 / 추가배송비 제주도 4,000원/도서산간 5,000원 추가"
    )
    assert fields["stock"].value.availability is Availability.ON_SALE
    assert fields["sales_channels"].value.scope is SalesChannelScope.ALL_ALLOWED
    assert fields["options"].status is FieldStatus.ABSENT


def test_the_site_reads_its_sold_out_closed_mall_capture() -> None:
    collection = SITES["upick"].collection
    fields = collection.fields(capture("4082"))
    prices = {price.label: price for price in fields["prices"].value.prices}
    assert prices["회원가"].role is PriceRole.PURCHASE
    assert prices["소비자가"].role is PriceRole.LIST
    assert fields["shipping"].value.fee_krw == 4000
    assert fields["stock"].value.availability is Availability.SOLD_OUT
    assert fields["sales_channels"].value.scope is SalesChannelScope.CLOSED_MALL_ONLY


def test_the_site_s_representative_image_is_its_big_image() -> None:
    collection = SITES["upick"].collection
    view = capture("4954")
    primary = [
        candidate
        for candidate in collection.roles.classify(view.body, URL.format(number="4954"))
        if candidate.role is ImageRole.PRIMARY
    ]
    assert {candidate.url for candidate in primary} == {
        "https://upickb2b.com/web/product/big/202607/f8b7e1d7d835845504d87ebd6d7be9a1.png"
    }


# ---------------------------------------------------------------- acceptance run 1 (ADR-0035)


def synthetic(
    number: str, *, title: str, rows: tuple[tuple[str, str], ...], description: str
) -> DocumentView:
    """A minimal product page in the captures' shape: the declared title, the 기본 정보 rows, the
    action area with the smart-design purchase control, and the closed description block."""
    cells = "".join(
        f'<tr class=" xans-record-"><th><span class="">{label}</span></th>'
        f'<td><span class="">{value}</span></td></tr>'
        for label, value in rows
    )
    body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta property="og:title" content="{title}">'
        f'<meta property="product:productId" content="{number}"></head><body>'
        '<div class="xans-element- xans-product xans-product-detaildesign">'
        f"<table><caption> 기본 정보</caption><tbody>{cells}</tbody></table></div>"
        '<table class="xans-element- xans-product xans-product-option xans-record-"></table>'
        '<div class="xans-element- xans-product xans-product-action productAction">'
        '<a href="#none" class="btnSubmit sizeL "><span id="actionBuy">BUY IT NOW</span></a>'
        "</div>"
        f'<div id="prdDetail" class="productDetail">{description}</div>'
        "</body></html>"
    )
    return DocumentView(
        ReadKind.PRODUCT_READ, 200, f"/product/x/{number}/", None, "text/html", body
    )


NAME_4992 = "네이처그랜드 이너포에버 갱년기 앤 유산균 500mg x 60캡슐"
MINIMUM_4072 = (
    "1개 13,900원 이상/ 2개 27,500원 이상 / 3개 40,800원 이상4개 53,900원 이상 / "
    "5개 66,700원 이상 / 6개 79,200원 이상"
)


def test_4992_a_coupang_ban_in_the_name_beside_an_all_allowed_row_is_held() -> None:
    view = synthetic(
        "4992",
        title=f"{NAME_4992}&nbsp; - 쿠팡 등록 불가 - U-PICK B2B",
        rows=(
            ("상품명", f"{NAME_4992} - 쿠팡 등록 불가"),
            ("판매가능플랫폼", "모든마켓 판매 가능"),
            ("배송비", "3,000원"),
        ),
        description="<p>상세 설명</p>",
    )
    fields = SITES["upick"].collection.fields(view)
    # Run 1 read ALL_ALLOWED here: confidently wrong. NR-02 holds it.
    assert fields["sales_channels"].status is FieldStatus.REVIEW_REQUIRED
    # Run 1 held the name: the doubled space in the title is one space now.
    assert fields["original_name"].status is FieldStatus.CONFIRMED
    assert fields["original_name"].value.text == f"{NAME_4992} - 쿠팡 등록 불가"


def test_4072_a_listed_row_and_a_name_ban_forbid_coupang_and_the_minimum_is_one_unit() -> None:
    view = synthetic(
        "4072",
        title="데일리 유산균 - 쿠팡 판매 금지 - U-PICK B2B",
        rows=(
            ("상품명", "데일리 유산균 - 쿠팡 판매 금지"),
            ("판매가능플랫폼", "모든마켓 판매가능/쿠팡,토스 판매금지"),
            ("최저판매가", MINIMUM_4072),
            ("배송비", "3,000원"),
        ),
        description="<p>상세 설명</p>",
    )
    fields = SITES["upick"].collection.fields(view)
    channels = fields["sales_channels"]
    # ADR-0035 §1 NR-03: one row stating an allowed phrase and restrictions reads as its
    # restrictions. "쿠팡,토스 판매금지" restricts Coupang; 토스 is no marketplace ICBM lists, so
    # nothing else is forbidden, but its words stay in the policy text for a later marketplace.
    assert channels.status is FieldStatus.CONFIRMED
    assert (channels.value.scope, channels.value.forbidden) == (
        SalesChannelScope.LISTED,
        ("coupang",),
    )
    assert "쿠팡,토스 판매금지" in channels.value.policy_text
    minimum = fields["minimum_sale_price"]
    assert minimum.status is FieldStatus.CONFIRMED
    assert minimum.value.amount_krw == 13900
    assert minimum.evidence[0].normalized == "13900"


def test_4837_an_invisible_description_is_never_confirmed_and_the_surcharge_is_kept() -> None:
    view = synthetic(
        "4837",
        title="마그네슘 - U-PICK B2B",
        rows=(
            ("상품명", "마그네슘"),
            ("판매가능플랫폼", "모든마켓 판매가능"),
            ("배송비", "3,000원"),
            ("추가배송비", "제주도 3,000원/도서산간 5,000원 추가"),
        ),
        description="\ufeff\ufeff",
    )
    fields = SITES["upick"].collection.fields(view)
    # Run 1 CONFIRMED the text "\ufeff\ufeff": a description made only of invisible characters.
    assert fields["detail_description"].status is FieldStatus.REVIEW_REQUIRED
    shipping = fields["shipping"]
    assert shipping.status is FieldStatus.CONFIRMED
    assert (shipping.value.kind, shipping.value.fee_krw) == (ShippingKind.FIXED, 3000)
    assert "추가배송비 제주도 3,000원/도서산간 5,000원 추가" in shipping.value.policy_text
    assert fields["sales_channels"].value.scope is SalesChannelScope.ALL_ALLOWED


# Every channel row value U-PICK showed in reconnaissance and in acceptance run 1. Under ADR-0035
# NR-03 every word of a row that is not wholly allowed must be read, so each must stay readable.
OBSERVED_CHANNEL_ROWS = {
    "모든마켓 판매가능": (SalesChannelScope.ALL_ALLOWED, ()),
    "모든마켓 판매 가능": (SalesChannelScope.ALL_ALLOWED, ()),
    "폐쇄몰": (SalesChannelScope.CLOSED_MALL_ONLY, ()),
    "폐쇄몰 전용/오픈마켓 판매불가": (SalesChannelScope.CLOSED_MALL_ONLY, ()),
    "폐쇄몰 전용 / 오픈마켓 판매 금지": (SalesChannelScope.CLOSED_MALL_ONLY, ()),
    "모든마켓 판매가능/쿠팡,토스 판매금지": (SalesChannelScope.LISTED, ("coupang",)),
}


@pytest.mark.parametrize("row_value", OBSERVED_CHANNEL_ROWS, ids=list(OBSERVED_CHANNEL_ROWS))
def test_every_observed_channel_row_is_read(row_value: str) -> None:
    view = synthetic(
        "4954",
        title="마그네슘 - U-PICK B2B",
        rows=(("상품명", "마그네슘"), ("판매가능플랫폼", row_value), ("배송비", "3,000원")),
        description="<p>상세 설명</p>",
    )
    channels = SITES["upick"].collection.fields(view)["sales_channels"]
    assert channels.status is FieldStatus.CONFIRMED, row_value
    assert (channels.value.scope, channels.value.forbidden) == OBSERVED_CHANNEL_ROWS[row_value]
