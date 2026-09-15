"""Sanitized reconnaissance inventory (ADR-0010 §5): structure out, never values."""

import json

import pytest

from scripts.m3harness.inventory import (
    assert_sanitized,
    document_inventory,
    image_urls,
    parse_robots,
    policy_links,
    robots_disallows,
    terms_signals,
)

URL = "https://supplier.test/product/sample/1234/category/56/"
MEMBER = "홍길동"
PRICE = "12,900원"
SIGNATURE = "SECRETSIGNATURE0001"
PAGE = f"""<html><head>
<meta property="og:title" content="샘플 상품명">
<meta property="product:price:amount" content="12900">
<script type="application/ld+json">{{"@type": "Product", "name": "샘플 상품명", "sku": "1234",
 "offers": {{"price": "12900", "priceCurrency": "KRW"}}}}</script>
<script src="https://cdn.analytics.test/a.js"></script>
</head><body>
<div id="member" class="xans-layout-statelogon">{MEMBER}님 환영합니다</div>
<form action="/exec/front/order/basket/" method="post">
  <input type="hidden" name="product_no" value="1234">
  <input type="hidden" name="member_token" value="tok-abc">
  <select name="option1" class="ProductOption0"><option>1kg x 2</option></select>
</form>
<div class="infoArea"><span id="span_product_price_text" class="price">{PRICE}</span></div>
<p class="delivery">배송비 3,000원</p>
<a href="#" class="btnSubmit buy">구매하기</a> <a class="btnBasket cart">장바구니</a>
<div class="thumbnail"><img src="//img.supplier.test/p/1234.jpg"></div>
<div id="prdDetail"><img src="https://cdn.supplier-images.test/d/1.jpg?X-Amz-Signature={SIGNATURE}&w=800"></div>
<footer><a href="/member/agreement.html">이용약관</a>
<a href="/member/privacy.html">개인정보처리방침</a>
<a href="https://other.test/terms">terms elsewhere</a></footer>
</body></html>"""


def test_the_inventory_carries_structure_and_no_value() -> None:
    inventory = document_inventory(PAGE, URL)
    rendered = json.dumps(inventory, ensure_ascii=False)
    for value in (MEMBER, PRICE, "12900", "샘플 상품명", "tok-abc", SIGNATURE, "1234", "1kg x 2"):
        assert value not in rendered, value
    assert inventory["path_form"] == "/product/sample/{n}/category/{n}/"
    assert "input:product_no" in {c["source"] for c in inventory["identity_candidates"]}
    assert {"attr:jsonld:sku", "input:product_no"} <= {
        c["source"] for c in inventory["identity_candidates"]
    }
    assert inventory["jsonld"][0]["type"] == "Product"
    assert "offers" in inventory["jsonld"][0]["keys"]
    assert "span#span_product_price_text.price" in inventory["selectors"]["price"]
    assert inventory["images"]["hosts"] == {"cdn.supplier-images.test": 1, "img.supplier.test": 1}
    assert inventory["images"]["secret_looking_query_keys"] == {"cdn.supplier-images.test": 1}
    assert inventory["images"]["plain_query_keys"] == {"cdn.supplier-images.test": ["w"]}
    assert inventory["text_signals"]["buy_label"] and inventory["text_signals"]["shipping_label"]
    assert not inventory["text_signals"]["sold_out_label"]
    assert inventory["policy_links"]["terms"] == ["/member/agreement.html"]
    assert "member_token" in inventory["input_names"]  # names only


def test_links_and_images_stay_in_memory_helpers() -> None:
    assert policy_links(PAGE, URL)["terms"] == ["/member/agreement.html"]
    assert image_urls(PAGE, URL)[0] == "https://img.supplier.test/p/1234.jpg"


def test_robots_rules_decide_by_the_longest_match() -> None:
    groups = parse_robots(
        "User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /member/\n"
        "Disallow: /product/*/private\nAllow: /member/agreement.html\n"
    )
    assert robots_disallows(groups, "/member/login.html")
    assert not robots_disallows(groups, "/member/agreement.html")
    assert not robots_disallows(groups, "/product/sample/1234/")
    assert robots_disallows(groups, "/product/sample/private")
    assert not robots_disallows(parse_robots("User-agent: *\nDisallow:\n"), "/product/x")


def test_terms_signals_are_booleans_of_public_labels() -> None:
    signals = terms_signals("<html><body><p>무단 수집 및 크롤링을 금지합니다</p></body></html>")
    assert signals["mentions"] == ["무단", "수집", "크롤링"]


def test_findings_naming_a_secret_are_refused() -> None:
    with pytest.raises(ValueError, match="nothing was written"):
        assert_sanitized({"note": f"x {SIGNATURE} y"}, [SIGNATURE])
    assert_sanitized({"note": "clean"}, [SIGNATURE, ""])
