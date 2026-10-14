"""슈퍼딜리버리's collector over three captured pages (ADR-0040 §1, §2, §5; slice C1).

The fixtures are the product scope the reviewed capture policy cut from three product pages the
owner's signed-in browser showed on 2026-10-14, by the extension's own ``captureInPage``: two
pages of one company with the wholesale price disclosed (``11728767``, ``6554423``) and one page of
another company whose price is undisclosed, with two sets (``12064384``). Nothing is contacted.

Each capture is pinned byte for byte, and each reading is pinned whole: a fresh run over the same
capture must yield exactly these facts and this source fingerprint (ADR-0040 §6, zero diffs).
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.container import _server_final_scan
from app.stages.collect.extension.capture import measure, policy_violations
from app.stages.collect.extension.policy import CapturePolicySource
from app.stages.collect.facts import (
    Availability,
    CollectedFacts,
    EvaluatedFacts,
    FactsStatus,
    FieldFact,
    FieldStatus,
    ForeignPricesValue,
    ForeignShippingValue,
    OptionsValue,
    PriceRole,
    ShippingKind,
    evaluate,
)
from app.stages.products.pricing import (
    SOURCE_CURRENCY_UNSUPPORTED,
    source_currency_reasons,
    source_inputs,
)
from integrations.suppliers.collection import (
    DocumentView,
    ImageRole,
    ReadKind,
    SourceIdentity,
    UnresolvedIdentity,
)
from integrations.suppliers.extraction import supplier_manifest
from integrations.suppliers.superdelivery import PROFILE, STATUS
from integrations.suppliers.superdelivery.collect.facts import (
    PRICE_NOT_DISCLOSED,
    REGULAR_PRICE_NOT_STATED,
)
from integrations.suppliers.superdelivery.collect.tax import (
    REDUCED_RATE_PERCENT,
    STANDARD_RATE_PERCENT,
    TAX_REVISION,
    tax_included,
)
from integrations.suppliers.superdelivery.collection import COLLECTION, IMAGE_HOSTS
from integrations.suppliers.transport.collection import CollectionTargetRefused, check_target

REPO_ROOT = Path(__file__).resolve().parents[5]
CAPTURES = REPO_ROOT / "tests" / "fixtures" / "suppliers" / "superdelivery"
SUPPLIERS = REPO_ROOT / "integrations" / "suppliers"
URL = "https://www.superdelivery.com/p/r/pd_p/{number}/"
IMAGE = "https://c.superdelivery.com/ip/n/sa/600/600/www.superdelivery.com/product_image/{path}"
# The captures, byte for byte: what the reviewed policy cut, with one neutralisation — the
# whitespace the storefront's template writes before the signed-in member's own region row of the
# shipping table is made that of every other row (the row's own ``my-area`` class is already dropped
# by the policy), so no capture says where the member is.
PINNED = {
    "11728767": (32586, "33d3620e04cb7b15ce09241c02051d28cf62db834a1ab66771c50aef571210ed"),
    "12064384": (39827, "ba7d5495d8ad440a993872d60295d3034e442ea3583e307ab5be353f393574e3"),
    "6554423": (31692, "d269ef4a83c4f45c6cfa5929310de507eaa96c6e2a65c43da75a9f5610e18728"),
}
POLICY_REVISION = "superdelivery-capture-1"
POLICY_DIGEST = "b2c75617449d7ead5a7bd4ba6aa3dcc4f4398dd5da80854c66ac54969023d07f"


def body(number: str) -> str:
    return (CAPTURES / f"{number}.html").read_text("utf-8")


def view(number: str, html: str | None = None) -> DocumentView:
    return DocumentView(
        ReadKind.PRODUCT_READ,
        200,
        f"/p/r/pd_p/{number}/",
        None,
        "text/html",
        body(number) if html is None else html,
    )


def fields(number: str, html: str | None = None) -> dict[str, FieldFact]:
    return dict(COLLECTION.fields(view(number, html)))


def statuses(found: dict[str, FieldFact]) -> dict[str, str]:
    return {key: fact.status.value for key, fact in found.items()}


def evaluated(number: str, html: str | None = None) -> EvaluatedFacts:
    manifest = supplier_manifest("superdelivery")
    return evaluate(
        CollectedFacts(
            supplier_key="superdelivery",
            source_product_id=number,
            source_url=URL.format(number=number),
            captured_at=datetime(2026, 10, 14, 9, 0, tzinfo=UTC),
            extractor_revision=manifest.revision,
            extractor_fingerprint=manifest.fingerprint,
            collection_run_id="run-1",
            correlation_id="corr-1",
            fields=fields(number, html),
        )
    )


# ---------------------------------------------------------------- the captures


@pytest.mark.parametrize("number", sorted(PINNED))
def test_each_capture_is_pinned_and_is_exactly_what_the_policy_allows(number: str) -> None:
    raw = (CAPTURES / f"{number}.html").read_bytes()
    assert (len(raw), hashlib.sha256(raw).hexdigest()) == PINNED[number]
    html = raw.decode("utf-8")
    policy = CapturePolicySource(SUPPLIERS).load("superdelivery")
    # The server re-checks the browser's cut (ADR-0019 §6) and the security gate (§6.1): nothing
    # the policy does not allow, nothing that must never be collected, not even a note.
    assert policy_violations(html, policy) == ()
    assert _server_final_scan(html).blocking == ()
    measured = measure(html)
    assert measured.html_bytes == PINNED[number][0]
    assert measured.image_refs <= policy.bounds.max_image_refs
    assert measured.nodes <= policy.bounds.max_nodes
    # The cut keeps no member state: no wishlist, cart quantity, coupon, favourite or region mark.
    for absent in (
        "add-to-wishlist-set",
        "td-quantities",
        "cart-quantity-info",
        "product-coupon",
        "favorite",
        "my-area",
        "<input",
        "<script",
        "<svg",
    ):
        assert absent not in html, absent


def test_the_policy_is_the_reviewed_one_for_the_profile_host() -> None:
    policy = CapturePolicySource(SUPPLIERS).load("superdelivery")
    assert (policy.revision, policy.digest) == (POLICY_REVISION, POLICY_DIGEST)
    assert policy.host == COLLECTION.profile.storefront_host == "www.superdelivery.com"
    assert (policy.product_root.by, policy.product_root.token) == ("id", "product-detail-wrap")
    assert [(r.by, r.token) for r in policy.allowed_regions] == [
        ("id", "dealing_consideration_box"),
        ("class", "dl-trade-tbl"),
        ("id", "dealer-carriage"),
    ]
    # ADR-0019's no-script rule, unchanged, and the member's own inputs and controls left out.
    assert {"script", "noscript", "style", "iframe", "input", "select", "button", "svg"} <= (
        policy.excluded_tags
    )
    # A table row keeps no class: the shipping table marks the member's own region on its row.
    assert not policy.keeps_attribute("tr", "class")
    assert policy.keeps_attribute("td", "class") and policy.keeps_attribute("img", "src")
    assert not policy.keeps_attribute("img", "srcset")
    assert not policy.keeps_attribute("tr", "data-stock")


@pytest.mark.parametrize(
    ("inserted", "violation"),
    [
        ("<script>window.x = 1</script>", "TAG_EXCLUDED:script"),
        ('<input type="text" value="3">', "TAG_EXCLUDED:input"),
        ('<div class="add-to-wishlist-set"></div>', "REGION_EXCLUDED:div"),
        ('<div id="product-coupon"></div>', "REGION_EXCLUDED:div"),
        ('<table><tbody><tr class="my-area"><th>九州</th></tr></tbody></table>', None),
        ('<span data-stock="95061">x</span>', "ATTRIBUTE_NOT_ALLOWED:span[data-stock]"),
    ],
)
def test_what_the_policy_cuts_is_refused_when_it_arrives(inserted: str, violation: str) -> None:
    policy = CapturePolicySource(SUPPLIERS).load("superdelivery")
    html = body("11728767").replace("</body>", f"{inserted}</body>")
    found = policy_violations(html, policy)
    assert (violation or "ATTRIBUTE_NOT_ALLOWED:tr[class]") in found


# ---------------------------------------------------------------- identity and profile


@pytest.mark.parametrize("number", sorted(PINNED))
def test_the_identity_is_the_page_number_corroborated_everywhere(number: str) -> None:
    identity = COLLECTION.identity(view(number), URL.format(number=number))
    assert isinstance(identity, SourceIdentity)
    assert identity.source_product_id == number
    assert identity.agreed == ("url", "link[canonical]", "SD品番", "SD品番 of a set")
    assert COLLECTION.url_product_hint(URL.format(number=number)) == number


def test_an_identity_the_page_does_not_corroborate_is_unresolved() -> None:
    other = COLLECTION.identity(view("11728767"), URL.format(number="11728768"))
    assert isinstance(other, UnresolvedIdentity) and other.seen == ("11728767", "11728768")
    no_canonical = body("11728767").replace('rel="canonical"', 'rel="alternate"')
    unresolved = COLLECTION.identity(view("11728767", no_canonical), URL.format(number="11728767"))
    assert isinstance(unresolved, UnresolvedIdentity)
    assert unresolved.missing == ("link[canonical]",)
    listing = COLLECTION.identity(view("11728767"), "https://www.superdelivery.com/p/do/dpsl/1/")
    assert isinstance(listing, UnresolvedIdentity) and listing.missing == ("url",)
    assert COLLECTION.url_product_hint("https://www.superdelivery.com/p/r/pd_p/１２/") is None


def test_the_profile_is_capture_only_recon_and_jpy() -> None:
    profile = COLLECTION.profile
    assert profile.supplier is PROFILE
    assert (PROFILE.supplier_key, PROFILE.display_name) == ("superdelivery", "슈퍼딜리버리")
    assert PROFILE.auth_required is False
    assert STATUS.value == "RECON"
    # OC-01, ADR-0019 AC-28: no direct-URL read and no list queue.
    assert profile.direct_url_reads is False and profile.queue_limits is None
    assert profile.policy_paths == frozenset() and dict(profile.safe_query_keys) == {}
    assert profile.image_hosts == IMAGE_HOSTS == {"c.superdelivery.com"}
    limits = profile.limits
    assert (limits.max_image_bytes, limits.max_detail_image_bytes) == (5_000_000, 30_000_000)
    assert limits.max_new_image_bytes_per_run == 120_000_000
    assert check_target(profile, URL.format(number="12064384"), ReadKind.PRODUCT_READ)
    for refused in (
        "https://www.superdelivery.com/p/r/pd_p/12064384/?ref=top",
        "https://www.superdelivery.com/p/do/dpsl/1000007/",
        "https://superdelivery.com/p/r/pd_p/12064384/",
    ):
        with pytest.raises(CollectionTargetRefused):
            check_target(profile, refused, ReadKind.PRODUCT_READ)


def test_the_extension_may_reach_the_product_host() -> None:
    manifest = json.loads((REPO_ROOT / "ui" / "extension" / "manifest.json").read_text("utf-8"))
    assert f"https://{COLLECTION.profile.storefront_host}/*" in manifest["host_permissions"]


# ---------------------------------------------------------------- the facts


def test_a_disclosed_page_is_priced_at_its_regular_price_with_consumption_tax() -> None:
    found = fields("11728767")
    assert statuses(found) == {
        "original_name": "CONFIRMED",
        "prices": "CONFIRMED",
        "options": "ABSENT",
        "stock": "CONFIRMED",
        "shipping": "CONFIRMED",
        "minimum_sale_price": "ABSENT",
        "quantity_tiers": "ABSENT",
        "brand": "CONFIRMED",
        "manufacturer": "CONFIRMED",
        "origin": "ABSENT",
        "notice": "ABSENT",
        "detail_description": "CONFIRMED",
        "sales_channels": "ABSENT",
    }
    # The ※ mark is the page's own (a reduced-tax item) and is kept.
    assert found["original_name"].value.text == (  # type: ignore[union-attr]
        "※ヘルシークラブ バランスパワービッグ ナッツグラノーラ 2袋(4本)入"
    )
    # The owner's rule (Issue #219 6164685916): the regular (struck) 税抜 price ¥97, not the
    # campaign price ¥92, plus the reduced 8 % consumption tax the page marks
    # (「（軽減税率対象商品）」), rounded up: 97 × 1.08 = 104.76 → ¥105.
    prices = found["prices"]
    assert isinstance(prices.value, ForeignPricesValue)
    assert [(p.label, p.currency, p.amount, p.role) for p in prices.value.prices] == [
        (
            "1セット（1点） 税込（税抜97円＋消費税8%、円未満切上げ）",
            "JPY",
            "105",
            PriceRole.PURCHASE,
        ),
        ("1セット（1点） 税抜", "JPY", "97", None),
        ("1セット（1点） 税抜 期間限定割引5%OFF（2026/10/15まで）", "JPY", "92", None),
        ("卸単価（66%）", "JPY", "92", None),
        ("メーカー希望小売価格", "JPY", "140", PriceRole.LIST),
    ]
    assert prices.value.purchase() == prices.value.prices[0]
    assert [(e.locator, e.status.value, e.observed, e.normalized) for e in prices.evidence] == [
        ("th:価格", "CONFIRMED", "価格（税抜）", "税抜"),
        (
            "table.set-list S1 td.maker-wholesale-set-price .cmp-price",
            "CONFIRMED",
            "¥92",
            "JPY 92",
        ),
        ("table.set-list S1 td.td-price02 .cmp-price", "CONFIRMED", "¥97 ¥92", "JPY 92"),
        ("table.set-list S1 td.td-price02 .list-price", "CONFIRMED", "¥97 ¥92", "JPY 97"),
        ("table.set-list S1 td.td-price01", "CONFIRMED", "¥140", "JPY 140"),
        (".campaign-info", "CONFIRMED", "期間限定割引5%OFF（2026/10/15まで）", None),
        (".reduce-tax", "CONFIRMED", "（軽減税率対象商品）", "消費税8% (jp-consumption-tax-1)"),
        (
            "prices:purchase",
            "CONFIRMED",
            None,
            "JPY 97 税抜 × 108/100 → JPY 105 (円未満切上げ, jp-consumption-tax-1)",
        ),
    ]
    # One set (S1 単品1個) is no choice: ADR-0013 ruling B.
    assert found["options"].evidence[0].observed == "S1 単品1個 JAN：4902621005517"
    # The company keeps no stock column; the page offers the product to the reader.
    stock = found["stock"]
    assert stock.value.availability is Availability.ON_SALE  # type: ignore[union-attr]
    assert [(e.kind.value, e.locator, e.observed) for e in stock.evidence] == [
        ("CONTROL_STATE", ".addCartItem_dummy", "カートに入れる")
    ]
    shipping = found["shipping"].value
    assert shipping == ForeignShippingValue(
        kind=ShippingKind.CONDITIONAL,
        currency="JPY",
        fee="610",
        free_over="20000",
        policy_text=(
            "送料全国一律 610円（税抜） / ご購入金額（税抜） 20,000円（未満） 610円（税抜）"
            "・20,000円（以上） 無料 / 北海道 880円・無料 / 沖縄 ・離島 4,500円・4,500円"
        ),
    )
    assert found["brand"].value.text == "ハマダコンフェクト株式会社"  # type: ignore[union-attr]
    assert found["manufacturer"].value.text == "ハマダコンフェクト株式会社"  # type: ignore[union-attr]
    assert found["manufacturer"].evidence[0].observed == "発売元：ハマダコンフェクト株式会社"
    # The whole description, longer than the 200 characters its evidence quotes: a value is never
    # a cut of the statement (the cafe24 and godomall rule).
    description = found["detail_description"].value.text  # type: ignore[union-attr]
    assert description.startswith("注意）商品名先頭に【※】マークは軽減税率対象品目です。")
    assert description.endswith(
        "栄養素等表示基準値(18歳以上、基準熱量2200kcaL)に占める割合：カルシウム33％、鉄33％"
    )
    assert "一覧はこちら" not in description and len(description) > 200
    evidence = found["detail_description"].evidence
    assert [(e.status, e.observed) for e in evidence] == [
        (FieldStatus.CONFIRMED, description[:200])
    ]
    # OC-08: the company's terms are kept as its words, and are no gate.
    terms = found["sales_channels"]
    assert terms.value is None and {e.status for e in terms.evidence} == {FieldStatus.ABSENT}
    assert [e.locator for e in terms.evidence] == [
        "table.dl-trade-tbl",
        "#dealing_consideration_box",
    ]
    assert terms.evidence[0].observed == (
        "消費者への直送 × / 仕入れ前の販売 × / 画像転載 ○ / ネット販売 ○ / 代金引換 ×"
    )
    assert "◆弊社サイトは日本国内の小売店様向けの販売となります。" in (
        terms.evidence[1].observed or ""
    )


def test_an_undisclosed_page_names_why_and_records_each_set() -> None:
    found = fields("12064384")
    assert statuses(found) == {
        "original_name": "CONFIRMED",
        "prices": "REVIEW_REQUIRED",
        "options": "CONFIRMED",
        "stock": "CONFIRMED",
        "shipping": "CONFIRMED",
        "minimum_sale_price": "ABSENT",
        "quantity_tiers": "ABSENT",
        "brand": "CONFIRMED",
        "manufacturer": "CONFIRMED",
        "origin": "CONFIRMED",
        "notice": "ABSENT",
        "detail_description": "CONFIRMED",
        "sales_channels": "ABSENT",
    }
    prices = found["prices"]
    assert prices.value is None
    assert [
        (e.kind.value, e.locator, e.status.value, e.observed, e.normalized) for e in prices.evidence
    ] == [
        ("DOM_TEXT", "th:価格", "CONFIRMED", "価格（税抜）", "税抜"),
        ("DOM_TEXT", ".campaign-info", "CONFIRMED", "初回限定割引5%OFF", None),
        (
            "DOM_TEXT",
            "table.set-list S1 td.maker-product-price",
            "CONFIRMED",
            "参考上代 オープンプライス",
            None,
        ),
        (
            "CONTROL_STATE",
            ".pdtl-set-apply",
            "REVIEW_REQUIRED",
            "卸価格を見る（申請）",
            PRICE_NOT_DISCLOSED,
        ),
    ]
    options = found["options"].value
    assert isinstance(options, OptionsValue)
    s1 = "【王子ネピア】ネピアソフトパックティシュ200W5P【ティッシュ】"
    s2 = "【王子ネピア】ネピアソフトパックティシュ200W5P×18点セット 【ティッシュ】"
    assert [(axis.name, axis.values) for axis in options.axes] == [("内訳", (s1, s2))]
    assert [
        (c.selections, c.supplier_sku_id, c.sold_out, c.unit_price, c.currency)
        for c in options.configurations
    ] == [((s1,), "12064384S1", False, None, None), ((s2,), "12064384S2", False, None, None)]
    # Each set's own words — its number, its 内訳 and its JAN — and its SD item number.
    assert [(e.observed, e.normalized) for e in found["options"].evidence] == [
        (f"S1 {s1} JAN：4901121635071", "12064384S1"),
        (f"S2 {s2} JAN：4901121635071", "12064384S2"),
    ]
    assert found["stock"].value.availability is Availability.ON_SALE  # type: ignore[union-attr]
    assert found["shipping"].value == ForeignShippingValue(
        kind=ShippingKind.CONDITIONAL,
        currency="JPY",
        fee="600",
        free_over="25000",
        policy_text=(
            "ご購入金額（税抜） 25,000円（未満） 600円（税抜）・25,000円（以上） 無料"
            " / 北海道 1,680円・1,680円 / 沖縄県 ・離島 5,050円・5,050円"
        ),
    )
    assert found["brand"].value.text == "王子ネピア"  # type: ignore[union-attr]
    assert found["manufacturer"].value.text == "王子ネピア株式会社"  # type: ignore[union-attr]
    assert found["origin"].value.text == "中国"  # type: ignore[union-attr]
    terms = found["sales_channels"].evidence
    assert terms[0].observed == (
        "消費者への直送 △（一部可） / 仕入れ前の販売 × / 画像転載 ○ / ネット販売 ○ / 代金引換 ×"
    )
    assert len((terms[1].observed or "").encode("utf-8")) <= 4000


def test_the_second_disclosed_page_reads_like_the_first() -> None:
    found = fields("6554423")
    assert statuses(found) == statuses(fields("11728767"))
    assert found["original_name"].value.text == (  # type: ignore[union-attr]
        "※ヘルシークラブ バランスパワービッグ 北海道バター 2袋(4本)入【食品・サプリメント】"
    )
    # The same regular ¥97 税抜 and the same marked reduced rate: ¥105.
    assert found["prices"].value == fields("11728767")["prices"].value


def test_the_consumption_tax_is_ceil_whole_yen() -> None:
    assert tax_included(97, 8) == 105  # 104.76
    assert tax_included(92, 8) == 100  # 99.36
    assert tax_included(97, 10) == 107  # 106.7
    assert tax_included(100, 10) == 110  # exact: nothing is added by the rounding
    assert (TAX_REVISION, STANDARD_RATE_PERCENT, REDUCED_RATE_PERCENT) == (
        "jp-consumption-tax-1",
        10,
        8,
    )


def _purchase_of(html: str) -> tuple[str, str, tuple[str | None, ...]]:
    prices = fields("11728767", html)["prices"]
    assert isinstance(prices.value, ForeignPricesValue)
    purchase = prices.value.purchase()
    assert purchase is not None
    rate = next(e for e in prices.evidence if e.normalized and e.normalized.startswith("消費税"))
    return purchase.amount, purchase.label, (rate.locator, rate.observed)


def test_the_reduced_rate_is_read_from_the_page_mark_or_the_explained_name_mark() -> None:
    html = body("11728767")
    no_block_mark = html.replace("（軽減税率対象商品）", "")
    # The name's ※, which the company's description explains, still marks the reduced rate.
    amount, _, (locator, observed) = _purchase_of(no_block_mark)
    assert (amount, locator) == ("105", ".product-comment")
    assert observed is not None and "【※】マークは軽減税率対象" in observed
    # Neither mark: the standard rate, 97 × 1.10 = 106.7 → ¥107.
    unmarked = no_block_mark.replace("【※】マークは軽減税率対象", "【※】マークは在庫限り")
    amount, label, (locator, observed) = _purchase_of(unmarked)
    assert (amount, locator, observed) == ("107", ".reduce-tax", None)
    assert label == "1セット（1点） 税込（税抜97円＋消費税10%、円未満切上げ）"


def _one_price(html: str) -> str:
    """The capture with its struck regular price ¥97 removed: one price, ¥92, shown."""
    return html.replace(
        '<p class="list-price"><span class="price-unit">¥</span>97</p>', ""
    ).replace('<span class="list-price"><span class="price-unit">¥</span>97</span>', "")


def _unmarked(html: str) -> str:
    """The capture without the page's discount mark (期間限定割引5%OFF … まで)."""
    unmarked = re.sub(r'<div class="campaign-info">.*?</div>', "", html, count=1, flags=re.S)
    assert "期間限定割引" not in unmarked
    return unmarked


def test_one_price_under_a_discount_mark_is_never_the_purchase_basis() -> None:
    # The owner's rule: the purchase cost is the regular price. With the page's 期間限定割引 mark
    # still shown and no struck regular price, the ¥92 shown is the discounted price: held.
    found = fields("11728767", _one_price(body("11728767")))
    prices = found["prices"]
    assert prices.status is FieldStatus.REVIEW_REQUIRED and prices.value is None
    assert prices.evidence[-1].normalized == REGULAR_PRICE_NOT_STATED
    assert (".campaign-info", "期間限定割引5%OFF（2026/10/15まで）") in [
        (e.locator, e.observed) for e in prices.evidence
    ]


def test_one_price_with_no_discount_mark_is_the_regular_price() -> None:
    # No discount marked anywhere: the set's one 税抜 price ¥92 is its regular price,
    # 92 × 1.08 = 99.36 → ¥100.
    found = fields("11728767", _unmarked(_one_price(body("11728767"))))
    prices = found["prices"]
    assert prices.status is FieldStatus.CONFIRMED
    assert isinstance(prices.value, ForeignPricesValue)
    assert [(p.label, p.amount, p.role) for p in prices.value.prices] == [
        ("1セット（1点） 税込（税抜92円＋消費税8%、円未満切上げ）", "100", PriceRole.PURCHASE),
        ("1セット（1点） 税抜", "92", None),
        ("卸単価（66%）", "92", None),
        ("メーカー希望小売価格", "140", PriceRole.LIST),
    ]


def test_sets_under_a_discount_mark_without_a_struck_price_hold_the_options() -> None:
    # Two sets, each showing one price and no struck price, under the page's 期間限定割引 mark:
    # neither states its regular price, so prices and options are held naming both sets.
    one = '<span class="cmp-price"><span class="price-unit">¥</span>276</span>'
    html = _one_price(_second_set(body("11728767"), one))
    marked = fields("11728767", html)
    assert marked["prices"].status is FieldStatus.REVIEW_REQUIRED
    options = marked["options"]
    assert options.status is FieldStatus.REVIEW_REQUIRED and options.value is None
    assert [(e.locator, e.normalized) for e in options.evidence[-2:]] == [
        ("table.set-list S1 td.maker-wholesale-set-price", REGULAR_PRICE_NOT_STATED),
        ("table.set-list S2 td.maker-wholesale-set-price", REGULAR_PRICE_NOT_STATED),
    ]
    # Without the mark, each one price is its set's regular price: ¥92 → ¥100, ¥276 → 298.08
    # → ¥299.
    unmarked = fields("11728767", _unmarked(html))
    configurations = unmarked["options"].value.configurations  # type: ignore[union-attr]
    assert [(c.supplier_sku_id, c.unit_price) for c in configurations] == [
        ("11728767S1", "100"),
        ("11728767S2", "299"),
    ]


def _second_set(html: str, set_cell: str) -> str:
    """The capture with a second set, S2 of three units, whose set cell is ``set_cell``."""
    start = html.index('<td rowspan="3" class=" co-align-center')
    start = html.rindex("<tr>", 0, start)
    # The set table's own body ends last inside the cart form (its rows nest tables of their own).
    end = html.rindex("</tbody>", start, html.index("</form>", start))
    group = html[start:end]
    # The set cell's own price is the one .cmp-price span written without whitespace inside.
    cell = '<span class="cmp-price"><span class="price-unit">¥</span>92</span>'
    assert group.count(cell) == 1
    second = (
        group.replace("S1", "S2")
        .replace("単品1個", "単品3個")
        .replace("（1点）", "（3点）")
        .replace(cell, set_cell)
    )
    return html[:end] + second + html[end:]


def test_each_set_carries_its_own_regular_price_with_tax() -> None:
    struck = (
        '<p class="list-price"><span class="price-unit">¥</span>291</p>'
        '<p class="cmp-price"><span class="price-unit">¥</span>276</p>'
    )
    found = fields("11728767", _second_set(body("11728767"), struck))
    options = found["options"].value
    assert isinstance(options, OptionsValue)
    # S1 ¥97 → ¥105; S2's own struck regular ¥291 → 314.28 → ¥315.
    assert [(c.supplier_sku_id, c.unit_price, c.currency) for c in options.configurations] == [
        ("11728767S1", "105", "JPY"),
        ("11728767S2", "315", "JPY"),
    ]


def test_a_later_campaign_set_without_its_regular_price_holds_the_options() -> None:
    # Set 2 is three units under the campaign, its own cell showing the campaign price alone: it
    # states no regular price. Every set is checked, not only the first: the options are held for
    # review naming set 2, and no configuration is recorded without its price.
    alone = '<span class="cmp-price"><span class="price-unit">¥</span>276</span>'
    found = fields("11728767", _second_set(body("11728767"), alone))
    options = found["options"]
    assert options.status is FieldStatus.REVIEW_REQUIRED and options.value is None
    assert [(e.locator, e.status.value, e.normalized) for e in options.evidence][-1] == (
        "table.set-list S2 td.maker-wholesale-set-price",
        "REVIEW_REQUIRED",
        REGULAR_PRICE_NOT_STATED,
    )
    # The purchase price derives from set 1, whose regular price is stated: it is unchanged.
    assert found["prices"].status is FieldStatus.CONFIRMED
    assert found["prices"].value.purchase().amount == "105"  # type: ignore[union-attr]
    # With set 1 as the campaign set of three without its regular price, both are held.
    first = _second_set(body("11728767"), alone).replace("（1点）", "（3点）")
    both = fields("11728767", first)
    assert both["prices"].status is FieldStatus.REVIEW_REQUIRED
    assert both["prices"].evidence[-1].normalized == REGULAR_PRICE_NOT_STATED
    assert both["options"].status is FieldStatus.REVIEW_REQUIRED


def test_a_description_is_whole_or_held() -> None:
    # A description far longer than the evidence quote stays whole; one carrying URL material is
    # held by its locator alone and never persisted.
    html = body("12064384")
    long_words = "長い説明文です。" * 60
    longer = html.replace("関連ワード： ティッシュ", f"関連ワード： ティッシュ{long_words}")
    found = fields("12064384", longer)["detail_description"]
    assert found.status is FieldStatus.CONFIRMED
    assert found.value.text.endswith(long_words)  # type: ignore[union-attr]
    assert len(found.value.text) > 500  # type: ignore[union-attr]
    assert len(found.evidence[0].observed or "") == 200
    url = html.replace("関連ワード： ティッシュ", "関連ワード： https://example.test/x?id=1")
    held = fields("12064384", url)["detail_description"]
    assert held.status is FieldStatus.REVIEW_REQUIRED and held.value is None
    assert held.evidence[0].observed is None


def test_a_campaign_set_without_its_regular_price_is_held() -> None:
    # One set of three units under a campaign, its own cell showing the campaign price alone.
    html = body("11728767").replace("（1点）", "（3点）")
    prices = fields("11728767", html)["prices"]
    assert prices.status is FieldStatus.REVIEW_REQUIRED and prices.value is None
    assert prices.evidence[-1].normalized == REGULAR_PRICE_NOT_STATED


def test_a_tax_included_price_column_is_the_purchase_price_itself() -> None:
    # Not seen on 2026-10-14: a column stated 税込 states the price paid; no tax is added.
    html = body("11728767").replace("価格（税抜）", "価格（税込）")
    prices = fields("11728767", html)["prices"]
    assert prices.status is FieldStatus.CONFIRMED
    assert isinstance(prices.value, ForeignPricesValue)
    assert [(p.label, p.currency, p.amount, p.role) for p in prices.value.prices] == [
        ("1セット（1点） 税込", "JPY", "97", PriceRole.PURCHASE),
        ("1セット（1点） 税込 期間限定割引5%OFF（2026/10/15まで）", "JPY", "92", None),
        ("卸単価（66%）", "JPY", "92", None),
        ("メーカー希望小売価格", "JPY", "140", PriceRole.LIST),
    ]
    neither = body("11728767").replace("価格（税抜）", "価格")
    held = fields("11728767", neither)["prices"]
    assert held.status is FieldStatus.REVIEW_REQUIRED
    assert held.evidence[-1].normalized == "PRICE_BASIS_NOT_STATED"


def test_stock_words_are_read_as_stated_or_held() -> None:
    html = body("12064384")
    sold_out = fields("12064384", html.replace("在庫有り", "在庫なし"))
    assert sold_out["stock"].value.availability is Availability.SOLD_OUT  # type: ignore[union-attr]
    configurations = sold_out["options"].value.configurations  # type: ignore[union-attr]
    assert [c.sold_out for c in configurations] == [True, True]
    one = html[::-1].replace("在庫有り"[::-1], "品切れ"[::-1], 1)[::-1]
    mixed = fields("12064384", one)
    assert mixed["stock"].value.availability is Availability.ON_SALE  # type: ignore[union-attr]
    unknown = fields("12064384", html.replace("在庫有り", "残りわずか", 1))
    assert unknown["stock"].status is FieldStatus.REVIEW_REQUIRED


def test_a_shipping_table_that_states_no_single_flat_fee_is_held() -> None:
    html = body("11728767").replace("送料全国一律 610円", "送料 610円")
    held = fields("11728767", html)["shipping"]
    # 北海道 is free over the threshold at 880円 and the others at 610円: no one flat fee is stated.
    assert held.status is FieldStatus.REVIEW_REQUIRED and held.value is None
    other = body("12064384").replace("25,000円（以上）", "30,000円（以上）")
    assert fields("12064384", other)["shipping"].status is FieldStatus.REVIEW_REQUIRED


def test_sets_the_page_words_alike_are_held() -> None:
    html = body("12064384").replace("×18点セット 【ティッシュ】", "【ティッシュ】")
    options = fields("12064384", html)["options"]
    assert options.status is FieldStatus.REVIEW_REQUIRED and options.value is None


def test_the_images_take_the_roles_the_product_block_gives_them() -> None:
    url = URL.format(number="11728767")
    candidates = COLLECTION.roles.classify(body("11728767"), url)
    assert [(c.role, c.rule, c.url) for c in candidates] == [
        (
            ImageRole.PRIMARY,
            "sd.primary.large_image",
            IMAGE.format(path="011/728/767/11728767_1002.jpg"),
        ),
        (
            ImageRole.THUMBNAIL,
            "sd.thumbnail.image_list",
            IMAGE.format(path="011/728/767/11728767_1002.jpg"),
        ),
        (
            ImageRole.THUMBNAIL,
            "sd.thumbnail.image_list",
            IMAGE.format(path="011/728/767/11728767_1003.jpg"),
        ),
        (
            ImageRole.UI_COMMON,
            "sd.ui.movie_mark",
            "https://c.superdelivery.com/img/mark/promark_movie.gif",
        ),
        (
            ImageRole.UI_COMMON,
            "sd.ui.cart_loading",
            "https://c.superdelivery.com/img/loading/circular.gif",
        ),
    ]
    assert all(c.host in IMAGE_HOSTS for c in candidates)


# ---------------------------------------------------------------- the revision and pricing

# ADR-0040 §6: a fresh run over the same capture yields zero diffs. These are the revisions' source
# fingerprints (ADR-0010 §6) for the captures with no image observed, under superdelivery-4.
SOURCE_FINGERPRINTS = {
    "11728767": "5bc7cef6a9467f30f034f30e969d4c02c99bddf76b31c9e0820c7c957dca829d",
    "12064384": "210afb21cf1fdc06dc7cb79f28f2b3b855ce08b46f77faae0b5792c2b321104c",
    "6554423": "3a78480dd16143e434713b2fac1a055933105b16d9eebf573d4560a680cafefa",
}


@pytest.mark.parametrize("number", sorted(PINNED))
def test_a_revision_states_jpy_and_is_not_priced(number: str) -> None:
    result = evaluated(number)
    # ADR-0040 §5, ADR-0038 §1: the revision states one currency, JPY.
    assert result.source_currency == "JPY"
    assert result.facts_status is FactsStatus.REVIEW_REQUIRED
    assert result.source_fingerprint == SOURCE_FINGERPRINTS[number]
    # Until O2, a JPY revision is not priced, and no JPY amount is ever read as won.
    assert [r.code for r in source_currency_reasons(result.source_currency)] == [
        SOURCE_CURRENCY_UNSUPPORTED
    ]
    reasons = source_inputs(fields(number))
    assert isinstance(reasons, tuple) and reasons


def test_even_a_confirmed_jpy_price_is_never_read_as_won() -> None:
    html = body("11728767").replace("価格（税抜）", "価格（税込）")
    result = evaluated("11728767", html)
    assert result.source_currency == "JPY"
    assert [r.code for r in source_currency_reasons(result.source_currency)] == [
        "PRICING_SOURCE_CURRENCY_UNSUPPORTED"
    ]
    codes = {r.code for r in source_inputs(fields("11728767", html))}  # type: ignore[union-attr]
    assert "PRICING_PURCHASE_PRICE_AMBIGUOUS" in codes
