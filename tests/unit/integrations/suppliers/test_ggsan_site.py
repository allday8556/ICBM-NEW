"""건강산's site configuration as this build binds it (ADR-0030 §8.2: the provider-zero proof).

The site is read through the registry, exactly as COLLECT and CONNECT will use it, on the two
reduced reconnaissance captures (Issue #219 `6086058056`). No supplier is contacted.
"""

from pathlib import Path

from app.stages.collect.facts import (
    Availability,
    FieldStatus,
    PriceRole,
    SalesChannelScope,
    ShippingKind,
)
from app.stages.products.pricing import SourceInputs, source_inputs
from integrations.suppliers.collection import DocumentView, ImageRole, ReadKind, SourceIdentity
from integrations.suppliers.registry import COLLECTIONS, SITE_PROBLEMS, SITES, SUPPLIERS
from integrations.suppliers.site_config import SiteStatus

CAPTURES = Path(__file__).resolve().parents[3] / "fixtures" / "suppliers" / "ggsan"
URL = "https://www.ggsan.com/goods/goods_view.php?goodsNo={number}"
STORAGE = "https://godomall-storage.cdn-nhncommerce.com/c738c7fa81eebdb2e242f3d46a1183a8"


def capture(number: str) -> DocumentView:
    body = (CAPTURES / f"{number}.html").read_text("utf-8")
    return DocumentView(
        ReadKind.PRODUCT_READ, 200, "/goods/goods_view.php", None, "text/html", body
    )


def test_ggsan_is_bound_in_recon_and_offered_to_connect_and_collect() -> None:
    assert SITE_PROBLEMS == ()
    site = SITES["ggsan"]
    assert site.config.status is SiteStatus.RECON
    assert site.template.platform == "godomall"
    assert site.extractor_revision == f"{site.template.revision}+ggsan-1"
    assert site.collection in COLLECTIONS
    assert site.definition in SUPPLIERS
    assert site.definition.profile.egress_hosts == {"www.ggsan.com"}
    assert site.collection.profile.image_hosts == {
        "godomall-storage.cdn-nhncommerce.com",
        "cdn-saas-web-216-59.cdn-nhncommerce.com",
    }
    assert site.collection.url_product_hint(URL.format(number="1000004918")) == "1000004918"


def test_the_site_reads_its_on_sale_capture() -> None:
    collection = SITES["ggsan"].collection
    view = capture("1000004918")
    identity = collection.identity(view, URL.format(number="1000004918"))
    assert isinstance(identity, SourceIdentity)
    assert identity.source_product_id == "1000004918"
    fields = collection.fields(view)
    assert (
        fields["original_name"].value.text
        == "네추럴라이즈 슈퍼 그린 멀티비타민&미네랄 1445mg x 180정"
    )
    prices = {price.label: price for price in fields["prices"].value.prices}
    assert (prices["판매가"].amount_krw, prices["판매가"].role) == (15000, PriceRole.PURCHASE)
    assert (prices["정가"].amount_krw, prices["정가"].role) == (33000, PriceRole.LIST)
    assert fields["prices"].value.purchase().amount_krw == 15000
    # ADR-0034 §1: the per-unit sentence, not the bundle or the shipping-inclusive amount.
    assert fields["minimum_sale_price"].value.amount_krw == 21000
    shipping = fields["shipping"].value
    assert (shipping.kind, shipping.fee_krw, shipping.free_over_krw) == (
        ShippingKind.CONDITIONAL,
        3000,
        200000,
    )
    assert fields["stock"].value.availability is Availability.ON_SALE
    assert fields["options"].status is FieldStatus.ABSENT
    channels = fields["sales_channels"].value
    assert (channels.scope, channels.forbidden) == (SalesChannelScope.LISTED, ("coupang",))


def test_the_site_reads_its_sold_out_capture() -> None:
    collection = SITES["ggsan"].collection
    view = capture("1000002412")
    identity = collection.identity(view, URL.format(number="1000002412"))
    assert isinstance(identity, SourceIdentity)
    assert identity.source_product_id == "1000002412"
    fields = collection.fields(view)
    assert fields["prices"].value.purchase().amount_krw == 2500
    assert fields["minimum_sale_price"].value.amount_krw == 4400
    assert fields["stock"].value.availability is Availability.SOLD_OUT
    # A product whose description names no restriction states none.
    assert fields["sales_channels"].status is FieldStatus.ABSENT


def test_the_free_over_policy_is_priced_at_its_base_fee() -> None:
    # ADR-0034 §2 (Issue #219 6088081139), on the capture's own facts.
    fields = SITES["ggsan"].collection.fields(capture("1000004918"))
    result = source_inputs(fields)
    assert result == SourceInputs(
        purchase_cost_krw=15000, supplier_shipping_krw=3000, minimum_sale_price_krw=21000
    )


def test_the_site_s_images_take_their_roles() -> None:
    collection = SITES["ggsan"].collection
    view = capture("1000004918")
    candidates = collection.roles.classify(view.body, URL.format(number="1000004918"))
    by_role: dict[ImageRole, set[str]] = {}
    for candidate in candidates:
        by_role.setdefault(candidate.role, set()).add(candidate.url)
    assert by_role[ImageRole.PRIMARY] == {
        f"{STORAGE}/goods/1000004918/image/detail/1000004918_detail_062.png"
    }
    assert len(by_role[ImageRole.THUMBNAIL]) == 4
    assert all("/image/detail/thumb/" in url for url in by_role[ImageRole.THUMBNAIL])
    assert len(by_role[ImageRole.DETAIL]) == 1
    assert all("/img/" in url or "/magnify/" in url for url in by_role[ImageRole.UI_COMMON])
