"""The Cafe24 template on U-PICK's reconnaissance captures (ADR-0030 §8.1, ADR-0031, ADR-0032).

The two captures are the owner-approved reconnaissance of 2026-10-10 (Issue #219 `6086058056`),
reduced to the product module and scrubbed of member text: 4954 on sale, 4082 sold out. The
vocabulary is the one U-PICK's site configuration will declare. No supplier is contacted.
"""

from pathlib import Path

import pytest

from app.stages.collect.facts import (
    Availability,
    FieldStatus,
    PriceRole,
    PricesValue,
    SalesChannelScope,
    ShippingKind,
    SourcePrice,
)
from integrations.suppliers.collection import DocumentView, ImageRole, ReadKind
from integrations.suppliers.platforms.cafe24 import vocabulary
from integrations.suppliers.platforms.cafe24.collect import classify_images, parse_fields, resolve
from integrations.suppliers.platforms.cafe24.collect.identity import SourceIdentity
from integrations.suppliers.site_config import SiteRegion
from tests.unit.integrations.suppliers.test_site_config import site

CAPTURES = Path(__file__).resolve().parents[4] / "fixtures" / "suppliers" / "upick"
URL = "https://upickb2b.com/product/{name}/{number}/category/1/display/4/"

WORDS = vocabulary(
    site(
        label_overrides={
            "purchase_price": ["회원가"],
            "list_price": ["소비자가"],
            "minimum_price": ["폐쇄몰 최저판매가"],
            "sales_channel_row": ["판매가능플랫폼"],
            "channel_all_allowed": ["모든마켓 판매가능"],
            "channel_closed_only": ["폐쇄몰", "오픈마켓 판매불가"],
            "channel_forbid_coupang": ["쿠팡판매 불가"],
            "title_suffix": ["- U-PICK B2B"],
        },
        region_overrides={"key_image": SiteRegion("class", "prdImg")},
    )
)


def capture(number: str) -> tuple[DocumentView, str]:
    body = (CAPTURES / f"{number}.html").read_text("utf-8")
    view = DocumentView(
        ReadKind.PRODUCT_READ, 200, f"/product/x/{number}/", None, "text/html", body
    )
    return view, URL.format(name="x", number=number)


def test_the_identity_is_the_page_s_product_number() -> None:
    view, url = capture("4954")
    identity = resolve(view, url)
    assert isinstance(identity, SourceIdentity)
    assert identity.source_product_id == "4954"


def test_the_name_is_the_row_not_the_title_with_the_shop_s_suffix() -> None:
    view, _ = capture("4954")
    name = parse_fields(view, WORDS)["original_name"]
    assert name.status is FieldStatus.CONFIRMED
    assert name.value.text == "웰러스 비타민C 500 2g x 20포"


def test_the_member_price_is_the_purchase_price_and_the_consumer_price_a_list_price() -> None:
    view, _ = capture("4082")
    prices = parse_fields(view, WORDS)["prices"]
    assert prices.status is FieldStatus.CONFIRMED
    assert prices.value.prices == (
        SourcePrice(label="소비자가", amount_krw=16900, role=PriceRole.LIST),
        SourcePrice(label="회원가", amount_krw=5000, role=PriceRole.PURCHASE),
    )
    assert prices.value.purchase() == SourcePrice(
        label="회원가", amount_krw=5000, role=PriceRole.PURCHASE
    )


def test_the_minimum_price_row_is_read() -> None:
    view, _ = capture("4954")
    minimum = parse_fields(view, WORDS)["minimum_sale_price"]
    assert minimum.status is FieldStatus.CONFIRMED
    assert (minimum.value.label, minimum.value.amount_krw) == ("최저판매가", 7900)


def test_a_shipping_range_is_priced_at_its_highest_amount() -> None:
    view, _ = capture("4082")
    shipping = parse_fields(view, WORDS)["shipping"]
    assert shipping.status is FieldStatus.CONFIRMED
    assert (shipping.value.kind, shipping.value.fee_krw) == (ShippingKind.FIXED, 4000)
    assert "3,000원 ~" in shipping.value.policy_text


def test_a_single_shipping_fee_is_unchanged() -> None:
    view, _ = capture("4954")
    shipping = parse_fields(view, WORDS)["shipping"]
    assert (shipping.value.kind, shipping.value.fee_krw) == (ShippingKind.FIXED, 3000)


@pytest.mark.parametrize(
    ("number", "availability"), [("4954", Availability.ON_SALE), ("4082", Availability.SOLD_OUT)]
)
def test_the_smart_design_controls_state_the_stock(number: str, availability: Availability) -> None:
    view, _ = capture(number)
    stock = parse_fields(view, WORDS)["stock"]
    assert stock.status is FieldStatus.CONFIRMED
    assert stock.value.availability is availability


@pytest.mark.parametrize(
    ("number", "scope"),
    [("4954", SalesChannelScope.ALL_ALLOWED), ("4082", SalesChannelScope.CLOSED_MALL_ONLY)],
)
def test_the_sales_channel_row_is_read(number: str, scope: SalesChannelScope) -> None:
    view, _ = capture(number)
    channels = parse_fields(view, WORDS)["sales_channels"]
    assert channels.status is FieldStatus.CONFIRMED
    assert channels.value.scope is scope


def test_a_channel_row_without_the_site_s_words_stays_under_review() -> None:
    view, _ = capture("4082")
    unworded = vocabulary(site(label_overrides={"sales_channel_row": ["판매가능플랫폼"]}))
    assert parse_fields(view, unworded)["sales_channels"].status is FieldStatus.REVIEW_REQUIRED


def test_the_representative_image_is_the_prd_img_big_image() -> None:
    view, url = capture("4954")
    roles = {
        candidate.url: candidate.role
        for candidate in classify_images(view.body, url, WORDS)
        if candidate.role is not ImageRole.UNKNOWN
    }
    big = "https://upickb2b.com/web/product/big/202607/f8b7e1d7d835845504d87ebd6d7be9a1.png"
    assert roles[big] is ImageRole.PRIMARY
    assert list(roles.values()).count(ImageRole.THUMBNAIL) == 2
    assert ImageRole.DETAIL in roles.values()


def test_an_empty_option_container_states_no_options() -> None:
    # ADR-0013 ruling B: ABSENT is the Product DB's proof of "no options".
    view, _ = capture("4954")
    assert parse_fields(view, WORDS)["options"].status is FieldStatus.ABSENT


@pytest.mark.parametrize(
    ("prices", "chosen"),
    [
        ((SourcePrice(label="판매가", amount_krw=10),), 10),
        ((SourcePrice(label="정가", amount_krw=10, role=PriceRole.LIST),), None),
        (
            (
                SourcePrice(label="정가", amount_krw=20, role=PriceRole.LIST),
                SourcePrice(label="판매가", amount_krw=10),
            ),
            10,
        ),
        (
            (
                SourcePrice(label="소비자가", amount_krw=20, role=PriceRole.LIST),
                SourcePrice(label="회원가", amount_krw=10, role=PriceRole.PURCHASE),
            ),
            10,
        ),
        (
            (
                SourcePrice(label="a", amount_krw=20, role=PriceRole.PURCHASE),
                SourcePrice(label="b", amount_krw=10, role=PriceRole.PURCHASE),
            ),
            None,
        ),
        ((SourcePrice(label="a", amount_krw=20), SourcePrice(label="b", amount_krw=10)), None),
    ],
)
def test_the_purchase_price_follows_adr_0032(
    prices: tuple[SourcePrice, ...], chosen: int | None
) -> None:
    picked = PricesValue(prices=prices).purchase()
    assert (picked.amount_krw if picked else None) == chosen


def test_a_price_without_a_role_serializes_as_before() -> None:
    assert SourcePrice(label="판매가", amount_krw=1).model_dump_json() == (
        '{"label":"판매가","amount_krw":1}'
    )
