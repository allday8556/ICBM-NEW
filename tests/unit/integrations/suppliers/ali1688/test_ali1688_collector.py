"""1688's collector over the five pinned captures (ADR-0040 §3, §4; slice C2).

Each fixture is exactly what the extension's own ``captureInPage`` cut in the owner's signed-in
browser on 2026-10-14 — the product-scope DOM, the projected embedded-data member — and the public
description document the page named, as the server would read it. Nothing here reaches a network.
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
    ForeignQuantityTiersValue,
    ForeignShippingValue,
    OptionsValue,
    PriceRole,
    ShippingKind,
    StockValue,
    TextValue,
    evaluate,
)
from integrations.suppliers.ali1688.collect import description as desc
from integrations.suppliers.ali1688.collect import facts
from integrations.suppliers.ali1688.collect.identity import (
    SourceIdentity,
    UnresolvedIdentity,
    path_number,
    resolve,
)
from integrations.suppliers.ali1688.collect.images import classify_images
from integrations.suppliers.ali1688.collection import COLLECTION, DESCRIPTION_HOSTS
from integrations.suppliers.collection import (
    DescriptionRead,
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
        "12f9683ffc272eb9ed58e5d832fe612c4e0601f9836a63e11186b1afe2e00cb6"
    ),
    "990041659965.description.txt": (
        "54fa5ddf25668358720d8093085690f92a0ee608edfd40dc83a0663429ba07d1"
    ),
    "970942992948.html": "a98d64304ccab16521f49ecc6f2bee08544a9ee7e3c8e4c83902241954971615",
    "970942992948.embedded.json": (
        "1c38c6c1ce9a0ba2bc435dc05eb79211a1fc570db8271ff4b5b09e084fe063f5"
    ),
    "970942992948.description.txt": (
        "b0a14d33c4d890821d9d1f5e94cea41c04c21ded6e22bc86c4823b044283459d"
    ),
    "959613200255.html": "f2b3f8ac6b0426f06eb60a5f82bf6851eaaf238e9a997cfb7817178bd01a9ff5",
    "959613200255.embedded.json": (
        "d85074e500effd0549d9ddf8accc336087ce2436d8b573f57eff038fe274d8cd"
    ),
    "959613200255.description.txt": (
        "bc47a2dbd43ed57c15db04bfab74fc938ef83e5a4563ff16c109f5cf7c82bd5c"
    ),
    "1051005117944.html": "36e041d0577fc6a55bf6b78f971194d8641480138bc69f92c441350d1fb12f3f",
    "1051005117944.embedded.json": (
        "2ac0c5bfc18d831a4ffa32077dd56ac362c0ae2dfc4d4c01989623150b55f84c"
    ),
    "1051005117944.description.txt": (
        "0ca2639915e823b4e56c3762f5b0f5ee81b3317dc9a01658e7ea1b21ecade57c"
    ),
    "1066237370938.html": "3000b5ce903957ef4e2d6dd52e839d3763368d04594323a994c92b361be4e03c",
    "1066237370938.embedded.json": (
        "e5b7b23263c017657f73865de07929bf57ee50a6267100704556ca420d234803"
    ),
    "1066237370938.description.txt": (
        "a5292c225e526e63fc214a1fcdef254e895302a130307c50d9675adc18b04414"
    ),
}


def _url(number: str) -> str:
    return f"https://detail.1688.com/offer/{number}.html"


def _html(number: str) -> str:
    return (FIXTURES / f"{number}.html").read_text("utf-8")


def _projection(number: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{number}.embedded.json").read_text("utf-8"))
    return loaded


def _description(number: str) -> str:
    return (FIXTURES / f"{number}.description.txt").read_text("utf-8")


def _document(
    number: str,
    *,
    data: dict[str, Any] | None = None,
    refusal: str | None = None,
    read: DescriptionRead | None = None,
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
        description=read,
    )


def _read(number: str) -> DescriptionRead:
    found = desc.classify(_description(number), desc.locate(_document(number)) or "")
    return DescriptionRead(stated=True, refusal=found.refusal, images=len(found.candidates))


def _fields(number: str, **changes: Any) -> dict[str, Any]:
    return dict(COLLECTION.fields(_document(number, read=_read(number), **changes)))


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
    assert profile.image_hosts == {"cbu01.alicdn.com", "img.alicdn.com"}
    assert profile.description_hosts == DESCRIPTION_HOSTS == {"itemcdn.tmall.com"}
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


# ---------------------------------------------------------------- the description document


@pytest.mark.parametrize(
    ("number", "images", "hosts"),
    [
        ("990041659965", 9, {"img.alicdn.com"}),
        ("970942992948", 15, {"img.alicdn.com", "cbu01.alicdn.com"}),
        ("959613200255", 10, {"cbu01.alicdn.com"}),
        ("1051005117944", 13, {"cbu01.alicdn.com"}),
        ("1066237370938", 13, {"cbu01.alicdn.com"}),
    ],
)
def test_the_description_document_states_the_detail_images(
    number: str, images: int, hosts: set[str]
) -> None:
    url = desc.locate(_document(number))
    assert url is not None and url.startswith("https://itemcdn.tmall.com/1688offer/icoss")
    found = desc.classify(_description(number), url)
    assert found.refusal is None and len(found.candidates) == images
    assert {candidate.role for candidate in found.candidates} == {ImageRole.DETAIL}
    assert {candidate.host for candidate in found.candidates} == hosts
    assert [candidate.order for candidate in found.candidates] == list(range(images))


@pytest.mark.parametrize(
    ("body", "refusal"),
    [
        (
            'offer_details={"content":"<img src=\\"https://cbu01.alicdn.com/a.jpg\\">"};',
            "NOT_OFFER",
        ),
        ('var offer_details={"content":"<img>"}', "NOT_OFFER"),
        ('var offer_details={"content":"x"};alert(1);', "NOT_JSON"),
        ('var offer_details={"content":"x"', "NOT_OFFER"),
        ("var offer_details={content:'x'};", "NOT_JSON"),
        ('var offer_details={"content":NaN};', "NOT_JSON"),
        ('var offer_details=["content"];', "NO_CONTENT"),
        ('var offer_details={"content":7};', "NO_CONTENT"),
        (
            'var offer_details={"html":"<img src=\\"https://cbu01.alicdn.com/a.jpg\\">"};',
            "NO_CONTENT",
        ),
        ("", "NOT_OFFER"),
    ],
)
def test_a_document_that_is_not_exactly_the_statement_states_no_image(
    body: str, refusal: str
) -> None:
    found = desc.classify(body, "https://itemcdn.tmall.com/1688offer/icoss1")
    assert found.candidates == ()
    assert found.refusal is not None and refusal in found.refusal


def test_only_a_declared_image_host_is_a_detail_image() -> None:
    body = (
        'var offer_details={"content":"<img src=\\"https://cbu01.alicdn.com/a.jpg\\">'
        '<img src=\\"//img.alicdn.com/b.jpg\\"><img src=\\"https://evil.test/c.jpg\\">'
        '<img src=\\"data:image/png;base64,AA\\"><img src=\\"\\">"};'
    )
    found = desc.classify(body, "https://itemcdn.tmall.com/1688offer/icoss1")
    assert [(c.role, c.host) for c in found.candidates] == [
        (ImageRole.DETAIL, "cbu01.alicdn.com"),
        (ImageRole.DETAIL, "img.alicdn.com"),
        (ImageRole.UNKNOWN, "evil.test"),
    ]
    assert desc.classify(body, "http://itemcdn.tmall.com/x").refusal == "DESCRIPTION_URL_NOT_HTTPS"


# ---------------------------------------------------------------- golden facts


def _evaluated(number: str, fields: dict[str, Any]) -> Any:
    return evaluate(
        CollectedFacts(
            supplier_key="ali1688",
            source_product_id=number,
            source_url=_url(number),
            captured_at=datetime(2026, 10, 14, tzinfo=UTC),
            extractor_revision="ali1688-1",
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
        "detail_images": 9,
    },
    "970942992948": {
        "name": "鸡蛋收纳盒滚动冰箱侧门放鸡蛋架托厨房用保鲜自动滑梯式滚蛋神器",
        "purchase": ("现价 ≥1个", "5.80"),
        "axes": [("规格", 2)],
        "configurations": 2,
        "prices": None,
        "tiers": [(1, "5.80", "≥1个 单价 5.80"), (90, "477.00", "≥90个 单价 5.30")],
        "shipping": ("FIXED", "3.00", "浙江省金华市 发货, 运费 3.00 CNY"),
        "brand": "自主",
        "origin": "浙江",
        "detail_images": 15,
    },
    "959613200255": {
        "name": "跨境大容量一周七天30格药盒早中晚便携式密封防潮药品收纳盒",
        "purchase": ("现价 ≥1个", "14.00"),
        "axes": [("颜色", 11)],
        "configurations": 11,
        "prices": None,
        "tiers": [(1, "14.00", "≥1个 单价 14.00"), (5000, "67500.00", "≥5000个 单价 13.50")],
        "shipping": ("FIXED", "3.50", "广东省东莞市 发货, 运费 3.50 CNY"),
        "brand": "四叶草",
        "origin": None,
        "detail_images": 10,
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
        "detail_images": 13,
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
        "detail_images": 13,
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
        assert isinstance(tiers.value, ForeignQuantityTiersValue)
        assert [(t.quantity, t.total_price, t.label) for t in tiers.value.tiers] == golden["tiers"]

    shipping = fields["shipping"].value
    assert isinstance(shipping, ForeignShippingValue)
    assert (shipping.kind.value, shipping.fee, shipping.policy_text) == golden["shipping"]

    for key in ("brand", "origin"):
        value = fields[key].value
        if golden[key] is None:
            assert fields[key].status is FieldStatus.ABSENT
        else:
            assert isinstance(value, TextValue) and value.text == golden[key]

    detail = fields["detail_description"]
    assert detail.status is FieldStatus.ABSENT
    assert detail.evidence[0].normalized == f"description:DETAIL_IMAGES:{golden['detail_images']}"
    for key in ("minimum_sale_price", "sales_channels", "manufacturer", "notice"):
        assert fields[key].status is FieldStatus.ABSENT
    # Every embedded fact cites its JSON path.
    for key in ("prices", "options", "stock", "shipping"):
        assert all(e.kind is EvidenceKind.EMBEDDED_JSON for e in fields[key].evidence)
        assert all(e.locator.startswith("embedded:result.data.") for e in fields[key].evidence)

    evaluated = _evaluated(number, fields)
    assert evaluated.source_currency == "CNY"
    # No image was fetched here, so the core images field alone keeps the revision under review.
    statuses = {field.key: field.status for field in evaluated.fields}
    assert statuses["images"] is FieldStatus.ABSENT
    assert evaluated.facts_status is FactsStatus.REVIEW_REQUIRED
    assert {k for k, s in statuses.items() if s is FieldStatus.REVIEW_REQUIRED} == set()


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
    recorded = [e for e in fields["options"].evidence if "discountPrice" in e.locator]
    assert len(recorded) == 1 and recorded[0].observed is not None
    assert "discountPrice 2.99" in recorded[0].observed and "promotion" in recorded[0].observed


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
    tiers = fields["quantity_tiers"].value
    assert isinstance(tiers, ForeignQuantityTiersValue)
    assert [(t.quantity, t.total_price) for t in tiers.tiers] == [(1, "6.20"), (90, "513.00")]


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
    fields = dict(
        COLLECTION.fields(
            _document(
                number,
                refusal=refusal,
                read=DescriptionRead(stated=False, refusal="DESCRIPTION_NOT_STATED"),
            )
        )
    )
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


def test_an_unread_description_is_recorded_as_its_reason() -> None:
    number = "959613200255"
    read = DescriptionRead(stated=True, refusal="DESCRIPTION_NOT_JSON")
    fact = COLLECTION.fields(_document(number, read=read))["detail_description"]
    assert fact.status is FieldStatus.REVIEW_REQUIRED
    assert fact.evidence[0].normalized == "description:DESCRIPTION_NOT_JSON"
    assert fact.evidence[0].locator == "embedded:result.data.description.fields.detailUrl"


def test_an_unknown_price_display_is_held_for_review() -> None:
    number = "990041659965"
    data = copy.deepcopy(_projection(number))
    _price_model(data)["priceDisplayType"] = "bmPrice"
    fields = _fields(number, data=data)
    assert fields["prices"].status is FieldStatus.REVIEW_REQUIRED
    assert fields["quantity_tiers"].status is FieldStatus.REVIEW_REQUIRED
