"""U-PICK's site configuration as this build binds it (ADR-0030 §8.2: the provider-zero proof).

The site is read through the registry, exactly as COLLECT and CONNECT will use it, on the two
reduced reconnaissance captures (Issue #219 `6090189930`). No supplier is contacted.
"""

from pathlib import Path

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
    assert site.extractor_revision == f"{site.template.revision}+upick-1"
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
