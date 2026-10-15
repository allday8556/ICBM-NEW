"""1688's collector over the five pinned captures (ADR-0040 §3, §4; slice C2).

Each fixture is exactly what the extension's own ``captureInPage`` cut in the owner's signed-in
browser on 2026-10-14: the product-scope DOM and the projected embedded-data member. The members
were re-derived once, exactly, for the policy without ``description.fields.detailUrl``: that member
was removed from each JSON (nothing else changes; JSON.stringify writes the same bytes). Nothing
here reaches a network.
"""

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.stages.collect.extension.embedded import projection_problem, strings_of
from app.stages.collect.extension.gate import embedded_data_gate
from app.stages.collect.extension.policy import CapturePolicySource
from app.stages.collect.facts import (
    Availability,
    CollectedFacts,
    EvidenceKind,
    FactsStatus,
    FieldStatus,
    ForeignPricesValue,
    ForeignShippingValue,
    OptionsValue,
    PriceRole,
    ShippingKind,
    StockValue,
    TextValue,
    evaluate,
)
from integrations.suppliers.ali1688.collect import facts
from integrations.suppliers.ali1688.collect.identity import (
    SourceIdentity,
    UnresolvedIdentity,
    path_number,
    resolve,
)
from integrations.suppliers.ali1688.collect.images import classify_images
from integrations.suppliers.ali1688.collection import COLLECTION
from integrations.suppliers.collection import (
    DocumentView,
    EmbeddedData,
    ImageRole,
    ReadKind,
)

REPO_ROOT = Path(__file__).resolve().parents[5]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "suppliers" / "ali1688"
NUMBERS = ("990041659965", "970942992948", "959613200255", "1051005117944", "1066237370938")
# The bytes the owner's browser produced, pinned (sha256 verified in the page and on disk).
PINNED = {
    "990041659965.html": "895af94fb85893b9b60513a9b2fc0db0148c8810f06320f0c8a891bd21b016e0",
    "990041659965.embedded.json": (
        "4a2591f9431b355469085962f9c7336ac05d20c4d412a2f561921fe71a056f74"
    ),
    "970942992948.html": "a98d64304ccab16521f49ecc6f2bee08544a9ee7e3c8e4c83902241954971615",
    "970942992948.embedded.json": (
        "30c134b488b9cc043089088f7401ff9bbf2490923d6dbe6089bd67746bd4a99b"
    ),
    "959613200255.html": "f2b3f8ac6b0426f06eb60a5f82bf6851eaaf238e9a997cfb7817178bd01a9ff5",
    "959613200255.embedded.json": (
        "6aa0350c1a7cf1d51a4cf1397d7a480099e859fc58739c5d830d48916646ba26"
    ),
    "1051005117944.html": "36e041d0577fc6a55bf6b78f971194d8641480138bc69f92c441350d1fb12f3f",
    "1051005117944.embedded.json": (
        "7cb1197804c9517358a24a6aac9faa988dde4bd7bb84103145172a2ddd6a808f"
    ),
    "1066237370938.html": "3000b5ce903957ef4e2d6dd52e839d3763368d04594323a994c92b361be4e03c",
    "1066237370938.embedded.json": (
        "be7fa5e8aee588e01c45496c7bd5fbd658ed532d02d74cc8655427026698a088"
    ),
}


def _url(number: str) -> str:
    return f"https://detail.1688.com/offer/{number}.html"


def _html(number: str) -> str:
    return (FIXTURES / f"{number}.html").read_text("utf-8")


def _projection(number: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{number}.embedded.json").read_text("utf-8"))
    return loaded


def _document(
    number: str,
    *,
    data: dict[str, Any] | None = None,
    refusal: str | None = None,
    html: str | None = None,
) -> DocumentView:
    embedded = (
        EmbeddedData(data=None, refusal=refusal)
        if refusal is not None
        else EmbeddedData(data=_projection(number) if data is None else data)
    )
    return DocumentView(
        kind=ReadKind.PRODUCT_READ,
        status=200,
        path=f"/offer/{number}.html",
        location=None,
        content_type="text/html",
        body=_html(number) if html is None else html,
        embedded=embedded,
    )


def _fields(number: str, **changes: Any) -> dict[str, Any]:
    return dict(COLLECTION.fields(_document(number, **changes)))


def _sku_model(data: dict[str, Any]) -> dict[str, Any]:
    model: dict[str, Any] = data["result"]["data"]["Root"]["fields"]["dataJson"]["skuModel"]
    return model


def _price_model(data: dict[str, Any]) -> dict[str, Any]:
    model: dict[str, Any] = data["result"]["data"]["mainPrice"]["fields"]["priceModel"]
    return model


# ---------------------------------------------------------------- the fixtures themselves


def test_the_fixtures_are_the_bytes_the_browser_cut() -> None:
    assert {path.name for path in FIXTURES.iterdir()} == set(PINNED)
    for name, digest in PINNED.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name


@pytest.mark.parametrize("number", NUMBERS)
def test_no_fixture_holds_member_or_account_data(number: str) -> None:
    # OC-05 and ADR-0019 AC-07: no receive address, no member identity, no account widget or
    # order count left the browser. The capture holds the product scope only.
    html = _html(number)
    projection = json.dumps(_projection(number), ensure_ascii=False)
    for text in (html, projection):
        for word in (
            "recieve",
            "receive",
            "Address",
            "address",
            "member",
            "loginId",
            "userId",
            "nick",
            "收货",
            "已售",
            "旺旺",
            "我的",
        ):
            assert word not in text, (number, word)
    # The member nick lives in the page header, which is outside the product scope.
    for outside in ('id="header"', 'id="headers"', "sell-point", "yx-data", "od-video-wrapper"):
        assert outside not in html, outside


@pytest.mark.parametrize("number", NUMBERS)
def test_every_projection_is_exactly_allowlisted_and_passes_the_gate(number: str) -> None:
    policy = CapturePolicySource(REPO_ROOT / "integrations" / "suppliers").load("ali1688")
    assert policy.embedded_data is not None
    projection = _projection(number)
    assert projection_problem(policy.embedded_data, projection) is None
    gate = embedded_data_gate(strings_of(policy.embedded_data, projection))
    assert gate.blocking == () and gate.notes == ()


# ---------------------------------------------------------------- identity


@pytest.mark.parametrize("number", NUMBERS)
def test_the_identity_is_the_offer_number_everywhere_the_page_states_it(number: str) -> None:
    identity = COLLECTION.identity(_document(number), _url(number))
    assert identity.source_product_id == number  # type: ignore[union-attr]
    resolved = resolve(_document(number), _url(number))
    assert isinstance(resolved, SourceIdentity)
    assert resolved.agreed == ("url", "link[canonical]", "embedded:offerId")


def test_an_identity_the_page_does_not_corroborate_is_unresolved() -> None:
    number = "990041659965"
    other = copy.deepcopy(_projection(number))
    other["result"]["data"]["gallery"]["fields"]["offerId"] = 990041659966
    disagree = resolve(_document(number, data=other), _url(number))
    assert isinstance(disagree, UnresolvedIdentity) and "disagree" in disagree.reason
    no_canonical = _html(number).replace('rel="canonical"', 'rel="alternate"')
    missing = resolve(_document(number, html=no_canonical), _url(number))
    assert isinstance(missing, UnresolvedIdentity)
    assert missing.missing == ("link[canonical]",)
    assert isinstance(
        resolve(_document(number), "https://detail.1688.com/x.html"), UnresolvedIdentity
    )
    # With the member refused, the URL and the canonical link still agree.
    refused = resolve(_document(number, refusal="EMBEDDED_NOT_JSON"), _url(number))
    assert isinstance(refused, SourceIdentity) and refused.agreed == ("url", "link[canonical]")
    assert path_number(_url(number)) == number
    assert path_number("https://detail.1688.com/offer/12.html?spm=1") == "12"
    assert path_number("https://m.1688.com/offer/12.html") is None


# ---------------------------------------------------------------- the profile


def test_the_profile_reads_captured_pages_only() -> None:
    profile = COLLECTION.profile
    assert profile.supplier.supplier_key == "ali1688"
    assert profile.supplier.display_name == "1688"
    assert profile.storefront_host == "detail.1688.com"
    assert profile.is_product_path("/offer/990041659965.html")
    assert not profile.is_product_path("/offer/990041659965.html/")
    assert not profile.is_product_path("/page/offerlist.htm")
    assert profile.direct_url_reads is False and profile.queue_limits is None
    # The gallery's host only. The server reads no description document (ADR-0040 OC-01).
    assert profile.image_hosts == {"cbu01.alicdn.com"}
    assert not hasattr(profile, "description_hosts")
    assert profile.supplier.auth_required is False


# ---------------------------------------------------------------- images


def test_the_gallery_states_the_representative_and_additional_images() -> None:
    candidates = classify_images(_html("990041659965"), _url("990041659965"))
    roles = [candidate.role for candidate in candidates]
    assert roles[0] is ImageRole.PRIMARY
    assert roles[1:] == [ImageRole.THUMBNAIL] * (len(roles) - 1)
    # 5 main images and 12 option images, all on the gallery host, in the page's own order.
    assert len(candidates) == 17
    assert {candidate.host for candidate in candidates} == {"cbu01.alicdn.com"}
    assert candidates[0].url.endswith("O1CN012VSoXf2CD7PebNNk0_!!678528439.jpg_.webp")
    # An image outside the gallery list is no product image.
    stray = '<div id="productTitle"><img src="https://img.alicdn.com/badge.png"></div>'
    (only,) = classify_images(stray, _url("990041659965"))
    assert only.role is ImageRole.UNKNOWN


# ---------------------------------------------------------------- golden facts


def _evaluated(number: str, fields: dict[str, Any]) -> Any:
    return evaluate(
        CollectedFacts(
            supplier_key="ali1688",
            source_product_id=number,
            source_url=_url(number),
            captured_at=datetime(2026, 10, 14, tzinfo=UTC),
            extractor_revision="ali1688-3",
            extractor_fingerprint="0" * 64,
            collection_run_id="run-1",
            correlation_id="corr-1",
            fields=fields,
        )
    )


GOLDEN: dict[str, dict[str, Any]] = {
    "990041659965": {
        "name": "老年人药盒便携分药盒器药分装盒一周七天吃药提醒分格老人药物",
        "purchase": ("SKU最低原价", "9.50"),
        "axes": [("颜色", 20), ("尺寸", 1)],
        "configurations": 20,
        "prices": {"9.50", "11.50"},
        "tiers": None,
        "shipping": ("FIXED", "3.00", "浙江省温州市 发货, 运费 3.00 CNY"),
        "brand": "金婷",
        "origin": "温州",
    },
    "970942992948": {
        "name": "鸡蛋收纳盒滚动冰箱侧门放鸡蛋架托厨房用保鲜自动滑梯式滚蛋神器",
        "purchase": ("现价 ≥1个", "5.80"),
        "axes": [("规格", 2)],
        "configurations": 2,
        "prices": None,
        "tiers": ["beginAmount 1个 price 5.80 CNY", "beginAmount 90个 price 5.30 CNY"],
        "shipping": ("FIXED", "3.00", "浙江省金华市 发货, 运费 3.00 CNY"),
        "brand": "自主",
        "origin": "浙江",
    },
    "959613200255": {
        "name": "跨境大容量一周七天30格药盒早中晚便携式密封防潮药品收纳盒",
        "purchase": ("现价 ≥1个", "14.00"),
        "axes": [("颜色", 11)],
        "configurations": 11,
        "prices": None,
        "tiers": ["beginAmount 1个 price 14.00 CNY", "beginAmount 5000个 price 13.50 CNY"],
        "shipping": ("FIXED", "3.50", "广东省东莞市 发货, 运费 3.50 CNY"),
        "brand": "四叶草",
        "origin": None,
    },
    "1051005117944": {
        "name": "滑梯滚蛋鸡蛋收纳盒多层家用冰箱侧门厨房取蛋神器斜口自动滚蛋架",
        "purchase": ("SKU最低原价", "3.80"),
        "axes": [("规格", 4)],
        "configurations": 4,
        "prices": {"3.80", "7.60"},
        "tiers": None,
        "shipping": ("FIXED", "3.00", "浙江省温州市 发货, 运费 3.00 CNY"),
        "brand": "其它",
        "origin": "浙江",
    },
    "1066237370938": {
        "name": "可旋转化妆品收纳盒桌面口红彩妆整理盒梳妆台护肤品化妆刷置物架",
        "purchase": ("SKU最低原价", "13.88"),
        "axes": [("颜色", 2)],
        "configurations": 2,
        "prices": {"13.88", "19.88"},
        "tiers": None,
        "shipping": ("FIXED", "2.50", "浙江省金华市 发货, 运费 2.50 CNY"),
        # The page states 品牌 无: there is no brand.
        "brand": None,
        "origin": "浙江台州",
    },
}


@pytest.mark.parametrize("number", NUMBERS)
def test_golden_facts(number: str) -> None:
    golden = GOLDEN[number]
    fields = _fields(number)
    assert isinstance(fields["original_name"].value, TextValue)
    assert fields["original_name"].value.text == golden["name"]

    prices = fields["prices"].value
    assert fields["prices"].status is FieldStatus.CONFIRMED
    assert isinstance(prices, ForeignPricesValue) and prices.currency == "CNY"
    purchase = prices.purchase()
    assert purchase is not None and purchase.role is PriceRole.PURCHASE
    assert (purchase.label, purchase.amount) == golden["purchase"]

    options = fields["options"].value
    assert isinstance(options, OptionsValue)
    assert [(axis.name, len(axis.values)) for axis in options.axes] == golden["axes"]
    assert len(options.configurations) == golden["configurations"]
    assert all(c.supplier_sku_id and c.supplier_sku_id.isdigit() for c in options.configurations)
    stated = {c.unit_price for c in options.configurations}
    if golden["prices"] is None:
        assert stated == {None}  # a tier-priced SKU states no price of its own
    else:
        assert stated == golden["prices"]
        assert {c.currency for c in options.configurations} == {"CNY"}

    stock = fields["stock"].value
    assert isinstance(stock, StockValue) and stock.availability is Availability.ON_SALE

    tiers = fields["quantity_tiers"]
    if golden["tiers"] is None:
        assert tiers.status is FieldStatus.ABSENT
    else:
        # A tier states a unit price; no total is ever made of it. Every tier is quoted verbatim.
        assert tiers.status is FieldStatus.REVIEW_REQUIRED and tiers.value is None
        assert [e.observed for e in tiers.evidence] == golden["tiers"]
        assert {e.normalized for e in tiers.evidence} == {
            f"{facts.TIER_UNIT_PRICE_NOT_TOTAL} currentPrices: current/shown"
        }
        # The same tiers are quoted on prices too, so each is recoverable from either field.
        quoted = [e.observed for e in fields["prices"].evidence if "Prices[" in e.locator]
        assert quoted == golden["tiers"]

    shipping = fields["shipping"].value
    assert isinstance(shipping, ForeignShippingValue)
    assert (shipping.kind.value, shipping.fee, shipping.policy_text) == golden["shipping"]

    for key in ("brand", "origin"):
        value = fields[key].value
        if golden[key] is None:
            assert fields[key].status is FieldStatus.ABSENT
        else:
            assert isinstance(value, TextValue) and value.text == golden[key]

    # The description is a separate document the page names, and it is not captured.
    detail = fields["detail_description"]
    assert detail.status is FieldStatus.REVIEW_REQUIRED and detail.value is None
    assert detail.evidence[0].normalized == facts.DESCRIPTION_NOT_CAPTURED
    for key in ("minimum_sale_price", "sales_channels", "manufacturer", "notice"):
        assert fields[key].status is FieldStatus.ABSENT
    # Every embedded fact cites its JSON path.
    for key in ("prices", "options", "stock", "shipping"):
        assert all(e.kind is EvidenceKind.EMBEDDED_JSON for e in fields[key].evidence)
        assert all(e.locator.startswith("embedded:result.data.") for e in fields[key].evidence)

    evaluated = _evaluated(number, fields)
    assert evaluated.source_currency == "CNY"
    statuses = {field.key: field.status for field in evaluated.fields}
    assert statuses["images"] is FieldStatus.ABSENT
    assert evaluated.facts_status is FactsStatus.REVIEW_REQUIRED
    review = {"detail_description"} | ({"quantity_tiers"} if golden["tiers"] else set())
    assert {k for k, s in statuses.items() if s is FieldStatus.REVIEW_REQUIRED} == review


def test_the_receive_address_is_never_in_the_shipping_text() -> None:
    for number in NUMBERS:
        shipping = _fields(number)["shipping"].value
        assert isinstance(shipping, ForeignShippingValue)
        assert "收货" not in shipping.policy_text and "address" not in shipping.policy_text


# ---------------------------------------------------------------- the owner's price basis


def test_a_promotion_price_is_recorded_and_never_the_cost() -> None:
    number = "1051005117944"
    data = copy.deepcopy(_projection(number))
    skus = _sku_model(data)["skuInfoMap"]
    key = "纯白色【1个装】可放约30个鸡蛋"
    skus[key]["discountPrice"] = "2.99"
    skus[key]["isPromotionSku"] = True
    fields = _fields(number, data=data)
    prices = fields["prices"].value
    assert isinstance(prices, ForeignPricesValue)
    assert prices.purchase() is not None and prices.purchase().amount == "3.80"  # type: ignore[union-attr]
    options = fields["options"].value
    assert isinstance(options, OptionsValue)
    promoted = next(c for c in options.configurations if c.selections == (key,))
    assert promoted.unit_price == "3.80"
    recorded = {
        e.locator.rsplit("skuInfoMap.", 1)[1]: e.observed
        for e in fields["options"].evidence
        if "skuInfoMap." in e.locator and not e.locator.endswith("skuInfoMap.*")
    }
    assert recorded[key] == "price 3.80, discountPrice 2.99, isPromotionSku true CNY"


def test_a_sku_with_only_a_shown_price_uses_it() -> None:
    number = "1066237370938"
    data = copy.deepcopy(_projection(number))
    for entry in _sku_model(data)["skuInfoMap"].values():
        del entry["price"]
    prices = _fields(number, data=data)["prices"].value
    assert isinstance(prices, ForeignPricesValue)
    assert prices.purchase().amount == "13.88"  # type: ignore[union-attr]


def test_a_tier_page_with_an_original_price_buys_at_the_original() -> None:
    number = "970942992948"
    data = copy.deepcopy(_projection(number))
    model = _price_model(data)
    model["originalPrices"] = [
        {"price": "6.20", "beginAmount": 1},
        {"price": "5.70", "beginAmount": 90},
    ]
    fields = _fields(number, data=data)
    prices = fields["prices"].value
    assert isinstance(prices, ForeignPricesValue)
    purchase = prices.purchase()
    assert purchase is not None and (purchase.label, purchase.amount) == ("原价 ≥1个", "6.20")
    # The current (promotion) first tier is kept beside it, as a fact without a role.
    assert [(p.label, p.amount, p.role) for p in prices.prices] == [
        ("原价 ≥1个", "6.20", PriceRole.PURCHASE),
        ("现价 ≥1个", "5.80", None),
    ]
    # Every tier of both lists is kept, the promotional ones included: none is dropped.
    expected = [
        ("originalPrices[0]", "beginAmount 1个 price 6.20 CNY", "originalPrices: original/line"),
        ("originalPrices[1]", "beginAmount 90个 price 5.70 CNY", "originalPrices: original/line"),
        (
            "currentPrices[0]",
            "beginAmount 1个 price 5.80 CNY",
            "currentPrices: current/promotional",
        ),
        (
            "currentPrices[1]",
            "beginAmount 90个 price 5.30 CNY",
            "currentPrices: current/promotional",
        ),
    ]
    quoted = [
        (e.locator.rsplit(".", 1)[1], e.observed, e.normalized)
        for e in fields["prices"].evidence
        if "Prices[" in e.locator
    ]
    assert quoted == expected
    tiers = fields["quantity_tiers"]
    assert tiers.status is FieldStatus.REVIEW_REQUIRED and tiers.value is None
    assert [(e.locator.rsplit(".", 1)[1], e.observed, e.normalized) for e in tiers.evidence] == [
        (where, said, f"{facts.TIER_UNIT_PRICE_NOT_TOTAL} {role}") for where, said, role in expected
    ]


@pytest.mark.parametrize("number", ["970942992948", "990041659965"])
def test_a_minimum_order_above_one_is_held_for_review(number: str) -> None:
    data = copy.deepcopy(_projection(number))
    for tier in _price_model(data)["currentPrices"]:
        tier["beginAmount"] = 2
    fact = _fields(number, data=data)["prices"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED and fact.value is None
    assert fact.evidence[0].normalized == facts.MINIMUM_ORDER_ABOVE_ONE


# ---------------------------------------------------------------- fail closed


@pytest.mark.parametrize("refusal", ["EMBEDDED_NOT_JSON", "EMBEDDED_PATH_NOT_ALLOWED"])
def test_a_refused_member_leaves_its_fields_under_review(refusal: str) -> None:
    number = "990041659965"
    fields = dict(COLLECTION.fields(_document(number, refusal=refusal)))
    for key in ("prices", "options", "stock", "quantity_tiers", "shipping"):
        assert fields[key].status is FieldStatus.REVIEW_REQUIRED, key
        assert fields[key].value is None
        assert fields[key].evidence[0].normalized == refusal
    # The DOM's own facts are still read; nothing is taken from the selected option.
    assert fields["original_name"].status is FieldStatus.CONFIRMED
    assert _evaluated(number, fields).facts_status is FactsStatus.REVIEW_REQUIRED


def test_a_sku_key_that_is_not_its_axes_is_held_for_review() -> None:
    number = "990041659965"
    data = copy.deepcopy(_projection(number))
    skus = _sku_model(data)["skuInfoMap"]
    key = next(iter(skus))
    skus[key.replace("&gt;", "/")] = skus.pop(key)
    fact = _fields(number, data=data)["options"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert fact.evidence[0].normalized == facts.SKU_KEY_UNREAD


def test_stock_is_as_stated_per_sku() -> None:
    number = "970942992948"
    data = copy.deepcopy(_projection(number))
    for entry in _sku_model(data)["skuInfoMap"].values():
        entry["canBookCount"] = 0
    fields = _fields(number, data=data)
    stock = fields["stock"].value
    assert isinstance(stock, StockValue) and stock.availability is Availability.SOLD_OUT
    options = fields["options"].value
    assert isinstance(options, OptionsValue)
    assert {c.sold_out for c in options.configurations} == {True}
    del _sku_model(data)["skuInfoMap"]["白色/可放约30个鸡蛋"]["canBookCount"]
    unread = _fields(number, data=data)["stock"]
    assert unread.status is FieldStatus.REVIEW_REQUIRED
    # The pinned page: one of its two SKUs cannot be booked, and the other can.
    pinned = _fields(number)["options"].value
    assert isinstance(pinned, OptionsValue)
    assert [c.sold_out for c in pinned.configurations] == [False, True]


def test_free_and_unstated_freight() -> None:
    number = "1066237370938"
    data = copy.deepcopy(_projection(number))
    data["result"]["data"]["shippingServices"]["fields"]["freightInfo"]["totalCost"] = 0
    shipping = _fields(number, data=data)["shipping"].value
    assert isinstance(shipping, ForeignShippingValue)
    assert shipping.kind is ShippingKind.FREE and shipping.fee is None
    del data["result"]["data"]["shippingServices"]
    assert _fields(number, data=data)["shipping"].status is FieldStatus.REVIEW_REQUIRED


def test_no_capture_carries_the_description_or_its_url() -> None:
    # The description is a separate document; nothing in a capture names or holds it, and the
    # policy allowlists no path to it, so the server never learns where it is.
    policy = CapturePolicySource(REPO_ROOT / "integrations" / "suppliers").load("ali1688")
    assert policy.embedded_data is not None
    assert not [e.path for e in policy.embedded_data.allowlist if "description" in e.path]
    for number in NUMBERS:
        assert "itemcdn" not in json.dumps(_projection(number))
        assert "detailUrl" not in json.dumps(_projection(number))


def test_an_unknown_price_display_is_held_for_review() -> None:
    number = "990041659965"
    data = copy.deepcopy(_projection(number))
    _price_model(data)["priceDisplayType"] = "bmPrice"
    fields = _fields(number, data=data)
    assert fields["prices"].status is FieldStatus.REVIEW_REQUIRED
    assert fields["quantity_tiers"].status is FieldStatus.REVIEW_REQUIRED


@pytest.mark.parametrize("number", NUMBERS)
def test_every_skus_stated_count_is_kept_verbatim(number: str) -> None:
    # ADR-0040 section 4: stock per SKU, as stated. The value says only whether the product can be
    # bought; each SKU's own count is one evidence entry under its own path, the page's integer.
    fields = _fields(number)
    skus = _sku_model(_projection(number))["skuInfoMap"]
    prefix = "embedded:result.data.Root.fields.dataJson.skuModel.skuInfoMap."
    kept = {
        e.locator.removeprefix(prefix).removesuffix(".canBookCount"): e
        for e in fields["stock"].evidence
        if e.locator.startswith(prefix) and e.locator.endswith(".canBookCount")
    }
    # The aggregate entry (the map's "*") stays beside them, derived as before.
    assert kept.pop("*").observed is not None
    assert set(kept) == set(skus)
    for key, entry in skus.items():
        evidence = kept[key]
        assert evidence.observed == str(entry["canBookCount"])
        assert evidence.status is FieldStatus.CONFIRMED
        assert evidence.normalized is not None
        assert evidence.normalized.startswith(f"skuId {entry['skuId']}: ")
    # Each configuration's sold-out state agrees with its own stated count.
    options = fields["options"].value
    assert isinstance(options, OptionsValue)
    by_id = {str(entry["skuId"]): entry["canBookCount"] for entry in skus.values()}
    for configuration in options.configurations:
        assert configuration.supplier_sku_id is not None
        assert configuration.sold_out is (by_id[configuration.supplier_sku_id] == 0)
    # The whole field still evaluates, every count inside the evidence bound.
    _evaluated(number, fields)


@pytest.mark.parametrize("number", NUMBERS)
def test_every_stated_price_is_recoverable(number: str) -> None:
    # No stated price is dropped: every tier of both price lists is quoted on prices, and every
    # SKU's price, discountPrice and promotion flag on options, exactly as the page wrote them.
    data = _projection(number)
    fields = _fields(number)
    model = _price_model(data)
    tiers = [
        (f"{name}[{index}]", f"beginAmount {tier['beginAmount']}", f"price {tier['price']}")
        for name in ("originalPrices", "currentPrices")
        for index, tier in enumerate(model.get(name, []))
    ]
    quoted = {e.locator.rsplit(".", 1)[1]: e.observed for e in fields["prices"].evidence}
    for where, begin, price in tiers:
        assert begin in quoted[where] and price in quoted[where], where
    skus = _sku_model(data)["skuInfoMap"]
    prefix = "embedded:result.data.Root.fields.dataJson.skuModel.skuInfoMap."
    kept = {
        e.locator.removeprefix(prefix): e.observed
        for e in fields["options"].evidence
        if e.locator.startswith(prefix) and e.locator != prefix + "*"
    }
    assert set(kept) == set(skus)
    for key, entry in skus.items():
        assert f"price {entry.get('price', '-')}," in kept[key]
        assert f"discountPrice {entry.get('discountPrice', '-')}," in kept[key]
        assert f"isPromotionSku {str(entry['isPromotionSku']).lower()}" in kept[key]
    _evaluated(number, fields)
