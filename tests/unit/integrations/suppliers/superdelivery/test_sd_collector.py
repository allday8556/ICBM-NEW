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
    TAX_INCLUDED_PRICE_NOT_STATED,
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


def test_a_disclosed_page_states_its_prices_tax_excluded_and_is_held_for_review() -> None:
    found = fields("11728767")
    assert statuses(found) == {
        "original_name": "CONFIRMED",
        "prices": "REVIEW_REQUIRED",
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
    # OC-07: the page states 価格（税抜） only. No 税込 price is computed; every stated price is
    # kept in the evidence as stated, and the field names why it holds no purchase price.
    prices = found["prices"]
    assert prices.value is None
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
        (".campaign-info", "CONFIRMED", "期間限定割引5%OFF2026/10/15まで", None),
        ("th:価格", "REVIEW_REQUIRED", "価格（税抜）", TAX_INCLUDED_PRICE_NOT_STATED),
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
            "送料全国一律 610円（税抜） / ご購入金額（税抜） 20,000円（未満） 610円"
            "・20,000円（以上） 無料 / 北海道 880円・無料 / 沖縄 ・離島 4,500円・4,500円"
        ),
    )
    assert found["brand"].value.text == "ハマダコンフェクト株式会社"  # type: ignore[union-attr]
    assert found["manufacturer"].value.text == "ハマダコンフェクト株式会社"  # type: ignore[union-attr]
    assert found["manufacturer"].evidence[0].observed == "発売元：ハマダコンフェクト株式会社"
    description = found["detail_description"].value.text  # type: ignore[union-attr]
    assert description.startswith("注意）商品名先頭に【※】マークは軽減税率対象品目です。")
    assert "一覧はこちら" not in description and len(description) == 200
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
            "ご購入金額（税抜） 25,000円（未満） 600円・25,000円（以上） 無料"
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
    assert found["prices"].evidence[-1].normalized == TAX_INCLUDED_PRICE_NOT_STATED


def test_a_tax_included_price_column_is_the_purchase_price_of_one_set() -> None:
    # Not seen on 2026-10-14: a page whose price column states 税込 states the price paid.
    html = body("11728767").replace("価格（税抜）", "価格（税込）")
    found = fields("11728767", html)
    prices = found["prices"]
    assert prices.status is FieldStatus.CONFIRMED
    assert isinstance(prices.value, ForeignPricesValue)
    assert [(p.label, p.currency, p.amount, p.role) for p in prices.value.prices] == [
        ("1セット（1点）", "JPY", "92", PriceRole.PURCHASE),
        ("卸単価（66%）", "JPY", "92", None),
        ("メーカー希望小売価格", "JPY", "140", PriceRole.LIST),
    ]
    assert prices.value.purchase() == prices.value.prices[0]


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
# fingerprints (ADR-0010 §6) for the captures with no image observed, under superdelivery-1.
SOURCE_FINGERPRINTS = {
    "11728767": "a4cf5e1b0e9f8c77bf5a6c298b09d4975fcd3839c85b470888689aeedc9879e3",
    "12064384": "10c294e70ffbac5c8a3b3fc05df793273408961f3352d0878446c16529f22358",
    "6554423": "b385e0f249b881e8ecb63d6a0ad3d79f2ffaf0feb01fa8686817d1b2c5260a2c",
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
