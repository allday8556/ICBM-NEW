"""The extension's own embedded-data cut, run in a real browser (ADR-0040 §3; slice C2).

``ui/extension/lib/capture.js`` is loaded into an ordinary page of a real browser and run against a
synthetic 1688-shaped page under the reviewed ``ali1688`` policy. Every request the page makes is
answered by the test itself: the supplier's host exists only as the address of a fulfilled route.

The synthetic page carries what a real one does around the data the policy allowlists — the
member's receive address and identity, SKU spec ids, object keys written as bare integers, scripts
with a ``src`` — so the test proves that only the allowlisted projection leaves the page, that the
server accepts exactly that, and that every mismatch sends no data, only its code.
"""

import copy
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.stages.collect.extension.capture import policy_violations
from app.stages.collect.extension.embedded import projection_problem, strings_of
from app.stages.collect.extension.gate import embedded_data_gate
from app.stages.collect.extension.policy import CapturePolicySource
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.extension_support import EXTENSION_ROOT, REPO_ROOT

pytestmark = pytest.mark.integration

URL = "https://detail.1688.com/offer/9001.html"
MODULE_BASE = "https://detail.1688.com/__icbm_extension__/"
POLICY_FILE = REPO_ROOT / "integrations" / "suppliers" / "ali1688" / "browser_capture_policy.json"
CUT = """async (policy) => {
  const module = await import("/__icbm_extension__/lib/capture.js");
  return module.captureInPage(policy);
}"""
CUT_JSON = """async (policy) => {
  const module = await import("/__icbm_extension__/lib/capture.js");
  return JSON.stringify(await module.captureInPage(policy));
}"""
DESCRIPTION_URL = "https://itemcdn.tmall.com/1688offer/icoss5072039755d496fa52ec17612"
PREFIX = 'window.contextPath = "/default";\nwindow.context=(function(b,d){return d})'
MARKER = "(window.contextPath,"

# The page's own data, as 1688 writes it: far more than the allowlist, some of it the member's.
CONTEXT = """{"result":{"data":{
 "gallery":{"fields":{"offerId":9001,"offerImgList":["https://cbu01.alicdn.com/x.jpg"]}},
 "mainPrice":{"fields":{"unit":"个","channel":"dsc","priceModel":{"priceDisplayType":"skuPrice",
   "currentPrices":[{"beginAmount":1,"price":"9.50"},{"beginAmount":1,"price":"11.50"}],
   "priceLabel":""}}},
 "Root":{"fields":{"dataJson":{"skuModel":{
   "skuProps":[{"fid":1,"prop":"颜色","value":[{"name":"红","imageUrl":"https://cbu01.alicdn.com/r.jpg"},{"name":"蓝"}]},
               {"fid":2,"prop":"尺寸","value":[{"name":"大"}]}],
   "skuInfoMap":{"红&gt;大":{"skuId":5960515164202,"specId":"93ccf20e4942667efa75ecc9dcc9b421","price":"9.50","discountPrice":"8.80","canBookCount":12,"saleCount":3,"isPromotionSku":true,"specAttrs":"红&gt;大"},
                 "蓝&gt;大":{"skuId":5960515164203,"specId":"0b6c52fe142d9e54407a3e91447bd018","price":"11.50","discountPrice":null,"canBookCount":0,"isPromotionSku":false}}},
   "frontSellerMemberModel":{"memberId":"b2b-221234567890"}}}},
 "shippingServices":{"fields":{"freightInfo":{"location":"浙江省金华市","totalCost":3.00,
   "skuWeight":{5960515164202:0.23,5960515164203 :0.3},
   "recieveAddress":"某省某市某区某路 1 号","receiveAddressId":778899,"receiveAddressRegion":"某区",
   "recieveAddressCode":"330782","receiveAddressAreaCodeList":["33","3307"]}}},
 "description":{"fields":{"detailUrl":"https://itemcdn.tmall.com/1688offer/icoss5072039755d496fa52ec17612","label":"详情描述"}},
 "userRights":{"isLogin":true,"loginId":"member-nick"}},
 "global":{"userId":"2212345678"}},"version":"0.26.18"}"""

EXPECTED = {
    "result": {
        "data": {
            "gallery": {"fields": {"offerId": 9001}},
            "mainPrice": {
                "fields": {
                    "unit": "个",
                    "priceModel": {
                        "priceDisplayType": "skuPrice",
                        "currentPrices": [
                            {"price": "9.50", "beginAmount": 1},
                            {"price": "11.50", "beginAmount": 1},
                        ],
                    },
                }
            },
            "Root": {
                "fields": {
                    "dataJson": {
                        "skuModel": {
                            "skuProps": [
                                {"prop": "颜色", "value": [{"name": "红"}, {"name": "蓝"}]},
                                {"prop": "尺寸", "value": [{"name": "大"}]},
                            ],
                            "skuInfoMap": {
                                "红&gt;大": {
                                    "skuId": 5960515164202,
                                    "price": "9.50",
                                    "discountPrice": "8.80",
                                    "canBookCount": 12,
                                    "isPromotionSku": True,
                                },
                                "蓝&gt;大": {
                                    "skuId": 5960515164203,
                                    "price": "11.50",
                                    "canBookCount": 0,
                                    "isPromotionSku": False,
                                },
                            },
                        }
                    }
                }
            },
            "shippingServices": {
                "fields": {"freightInfo": {"location": "浙江省金华市", "totalCost": 3}}
            },
        }
    }
}

BODY = """<div id="headers"><div class="member-nick">member-nick 我的阿里</div></div>
<div id="content"><div id="screen"><div id="product">
<div id="gallery"><div class="od-gallery-preview"><ul class="od-gallery-list">
<li><div class="od-video-wrapper"><video src="https://cloud.video.taobao.com/v.mp4"></video></div></li>
<li><img class="ant-image-img preview-img" src="https://cbu01.alicdn.com/img/ibank/a.jpg_.webp"></li>
<li><img class="ant-image-img preview-img" src="https://cbu01.alicdn.com/img/ibank/b.jpg_.webp"></li>
</ul></div><div class="yx-data-component">近期复购200+次</div></div>
<div id="productAttributes"><table><tr><th><span>品牌</span></th>
<td><span class="field-value">金婷</span></td></tr></table></div>
</div><div id="cart"><div id="productTitle"><div class="title-content"><h1>合成 1688 商品</h1></div>
<div class="sell-point-wrapper">100+人已加购</div></div></div></div></div>"""


def _page_html(scripts: list[str]) -> str:
    inline = "".join(scripts)
    return (
        "<!doctype html><html><head>"
        f'<link rel="canonical" href="{URL}">'
        '<script src="https://g.alicdn.com/od/main.js"></script>'
        '<script type="application/ld+json">{"@type":"Product"}</script>'
        f"</head><body>{BODY}{inline}</body></html>"
    )


def _context_script(argument: str = CONTEXT, *, prefix: str = PREFIX, tail: str = ");") -> str:
    return f"<script>\n{prefix}{MARKER}{argument}{tail}\n</script>"


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            launched = launch_browser(playwright)
        except PlaywrightError as exc:
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({type(exc).__name__})")
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture(scope="module")
def policy() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(POLICY_FILE.read_text("utf-8"))
    return loaded


# The description document the synthetic page names, as itemcdn.tmall.com shapes it: images on the
# declared host (one twice, one written protocol-relative, one in data-src), on other hosts, and a
# script and an onerror handler that must never run.
DESCRIBED = [
    "https://cbu01.alicdn.com/img/ibank/d1.jpg",
    "https://cbu01.alicdn.com/img/ibank/d2.jpg",
    "https://cbu01.alicdn.com/img/ibank/d3.jpg",
]
CONTENT = (
    '<p>商品详情</p><img src="https://cbu01.alicdn.com/img/ibank/d1.jpg">'
    '<img src="https://img.alicdn.com/tfs/icon-64-64.png">'
    '<img src="//cbu01.alicdn.com/img/ibank/d2.jpg" usemap="#m">'
    '<img data-src="https://cbu01.alicdn.com/img/ibank/d3.jpg">'
    '<img src="https://cbu01.alicdn.com/img/ibank/d1.jpg">'
    '<img src="https://evil.test/x.jpg">'
    '<script>window.__pwned = 1</script><img src="x" onerror="window.__pwned = 2">'
)


def _document(content: str = CONTENT) -> str:
    return "var offer_details=" + json.dumps({"content": content}, ensure_ascii=False) + ";"


Answer = tuple[int, str, dict[str, str]]


def _cut(
    browser: Browser,
    html: str,
    policy: dict[str, Any],
    *,
    script: str = CUT,
    description: Answer | None = None,
) -> dict[str, Any]:
    served = description or (200, _document(), {"content-type": "text/plain; charset=UTF-8"})

    def answer(route: Route) -> None:
        requested = route.request.url
        if requested == DESCRIPTION_URL:
            # The description read sends no cookie and no credentials of the page.
            assert "cookie" not in {name.lower() for name in route.request.headers}
            status, body, headers = served
            route.fulfill(status=status, body=body, headers=headers)
        elif requested.startswith(MODULE_BASE):
            source = EXTENSION_ROOT / requested.removeprefix(MODULE_BASE)
            route.fulfill(
                status=200, content_type="text/javascript", body=source.read_text("utf-8")
            )
        elif requested == URL:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        else:
            route.abort()

    page: Page = browser.new_page()
    try:
        page.route("**/*", answer)
        page.goto(URL)
        page.context.add_cookies([{"name": "member", "value": "secret", "url": URL}])
        evaluated = page.evaluate(script, policy)
        result: dict[str, Any] = json.loads(evaluated) if isinstance(evaluated, str) else evaluated
        # Nothing of the description ran in the page: it was parsed into an inert document.
        assert page.evaluate("window.__pwned") is None
        return result
    finally:
        page.close()


def _server_rule() -> Any:
    return CapturePolicySource(REPO_ROOT / "integrations" / "suppliers").load("ali1688")


def test_only_the_allowlisted_projection_leaves_the_page(
    browser: Browser, policy: dict[str, Any]
) -> None:
    cut = _cut(browser, _page_html([_context_script()]), policy)
    assert cut["ok"] is True
    assert cut["embedded_data"] == {"projection": EXPECTED}
    assert cut["description_images"] == {"images": DESCRIBED, "total": len(DESCRIBED)}
    server = _server_rule()
    # The server accepts exactly that, and its gate finds nothing.
    assert projection_problem(server.embedded_data, cut["embedded_data"]["projection"]) is None
    gate = embedded_data_gate(strings_of(server.embedded_data, cut["embedded_data"]["projection"]))
    assert gate.blocking == () and gate.notes == ()
    # Nothing of the member, the script or the decoys left the page, in either member.
    sent = json.dumps(cut, ensure_ascii=False)
    for absent in (
        "recieveAddress",
        "receiveAddress",
        "某省某市",
        "778899",
        "member-nick",
        "2212345678",
        "b2b-221234567890",
        "specId",
        "93ccf20e",
        "skuWeight",
        "saleCount",
        "window.context",
        "function(b,d)",
        "近期复购",
        "已加购",
        "video",
        # ADR-0040 §3.2: nothing of the description document leaves the page but its image
        # references on the declared host: not its text, its HTML or another host's images.
        "offer_details",
        "商品详情",
        "evil.test",
        "img.alicdn.com",
        "window.__pwned",
        # OC-09: the description's URL is read in the page and never sent.
        "detailUrl",
        "itemcdn",
        "icoss5072039755",
        "详情描述",
    ):
        assert absent not in sent, absent
    # The DOM capture is the reviewed product scope, and the server finds nothing it disallows.
    assert policy_violations(cut["html"], server) == ()
    assert "合成 1688 商品" in cut["html"] and "金婷" in cut["html"]


@pytest.mark.parametrize(
    ("scripts", "code"),
    [
        ([], "EMBEDDED_SCRIPT_NOT_EXACTLY_ONE"),
        ([_context_script(), _context_script()], "EMBEDDED_SCRIPT_NOT_EXACTLY_ONE"),
        ([_context_script(CONTEXT + "})" + MARKER + "1")], "EMBEDDED_MARKER_NOT_EXACTLY_ONE"),
        ([_context_script(tail=");window.x=1;")], "EMBEDDED_TERMINATOR"),
        ([_context_script(CONTEXT.replace('"unit":"个"', "unit:'个'"))], "EMBEDDED_NOT_JSON"),
        ([_context_script(CONTEXT + "+1")], "EMBEDDED_NOT_JSON"),
        (
            [
                _context_script(
                    CONTEXT.replace('"price":"9.50","discountPrice"', '"price":9.5,"discountPrice"')
                )
            ],
            "EMBEDDED_WRONG_SHAPE",
        ),
        (
            [_context_script(CONTEXT.replace('"offerId":9001', '"offerId":"9001"'))],
            "EMBEDDED_WRONG_SHAPE",
        ),
        (
            [
                _context_script(
                    CONTEXT.replace('"skuProps":[', '"skuProps":{"x":[').replace("]}],", "]}]},", 1)
                )
            ],
            "EMBEDDED_WRONG_SHAPE",
        ),
    ],
)
def test_any_mismatch_sends_no_data_only_its_code(
    browser: Browser, policy: dict[str, Any], scripts: list[str], code: str
) -> None:
    cut = _cut(browser, _page_html(scripts), policy)
    # The DOM capture goes on; the member says why it carries nothing.
    assert cut["ok"] is True and "html" in cut
    assert cut["embedded_data"] == {"refused": code}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda rule: rule.update(max_bytes=1000), "EMBEDDED_OVER_BYTES"),
        (lambda rule: rule.update(max_depth=5), "EMBEDDED_OVER_DEPTH"),
        (lambda rule: rule.update(bare_integer_keys=False), "EMBEDDED_NOT_JSON"),
    ],
)
def test_the_bounds_and_the_integer_key_rule_are_the_policys(
    browser: Browser,
    policy: dict[str, Any],
    change: Callable[[dict[str, Any]], None],
    code: str,
) -> None:
    changed = copy.deepcopy(policy)
    change(changed["embedded_data"])
    cut = _cut(browser, _page_html([_context_script()]), changed)
    assert cut["embedded_data"] == {"refused": code}


def test_a_script_with_a_source_or_another_prefix_is_never_read(
    browser: Browser, policy: dict[str, Any]
) -> None:
    other = f"<script>\nwindow.contextPath = '/other';{MARKER}{CONTEXT});\n</script>"
    cut = _cut(browser, _page_html([other]), policy)
    assert cut["embedded_data"] == {"refused": "EMBEDDED_SCRIPT_NOT_EXACTLY_ONE"}


def test_a_map_key_is_data_even_when_it_is_named_like_a_property(
    browser: Browser, policy: dict[str, Any]
) -> None:
    hostile = CONTEXT.replace('"红&gt;大":{', '"__proto__":{', 1)
    # Read as the client sends it (client.js serializes the capture with JSON.stringify).
    cut = _cut(browser, _page_html([_context_script(hostile)]), policy, script=CUT_JSON)
    skus = cut["embedded_data"]["projection"]["result"]["data"]["Root"]["fields"]["dataJson"][
        "skuModel"
    ]["skuInfoMap"]
    assert set(skus) == {"__proto__", "蓝&gt;大"}
    assert (
        projection_problem(_server_rule().embedded_data, cut["embedded_data"]["projection"]) is None
    )


def test_a_policy_without_embedded_data_cuts_no_member(
    browser: Browser, policy: dict[str, Any]
) -> None:
    plain = {
        key: value for key, value in policy.items() if key not in ("embedded_data", "description")
    }
    cut = _cut(browser, _page_html([_context_script()]), plain)
    assert set(cut) == {"ok", "html", "transport"}


# ---------------------------------------------------------------- the description images (§3.2)


def _with(policy: dict[str, Any], **changes: Any) -> dict[str, Any]:
    changed = copy.deepcopy(policy)
    changed["description"].update(changes)
    return changed


@pytest.mark.parametrize(
    ("description", "code"),
    [
        ((404, "", {}), "DESCRIPTION_STATUS"),
        # A redirect is never followed (redirect: "error"); its target cannot even resolve.
        ((302, "", {"location": "https://redirected.invalid/doc"}), "DESCRIPTION_FETCH_FAILED"),
        ((200, '{"content":"<img>"};', {}), "DESCRIPTION_NOT_STATEMENT"),
        ((200, 'var offer_details={"content":"x"}', {}), "DESCRIPTION_NOT_STATEMENT"),
        ((200, "var offer_details={content:'x'};", {}), "DESCRIPTION_NOT_JSON"),
        ((200, 'var offer_details={"content":"x"};alert(1);', {}), "DESCRIPTION_NOT_JSON"),
        ((200, 'var offer_details={"html":"x"};', {}), "DESCRIPTION_NO_CONTENT"),
        ((200, 'var offer_details=["x"];', {}), "DESCRIPTION_NO_CONTENT"),
        (
            (200, _document('<img src="https://cbu01.alicdn.com/a.jpg?token=abc">'), {}),
            "DESCRIPTION_IMAGE_NOT_PLAIN",
        ),
        (
            (200, _document('<img src="http://cbu01.alicdn.com/a.jpg">'), {}),
            "DESCRIPTION_IMAGE_NOT_PLAIN",
        ),
    ],
)
def test_a_description_that_is_not_exactly_the_statement_sends_only_its_code(
    browser: Browser, policy: dict[str, Any], description: Answer, code: str
) -> None:
    cut = _cut(browser, _page_html([_context_script()]), policy, description=description)
    # The capture and its embedded data go on; the description member says why it is empty.
    assert cut["ok"] is True and "projection" in cut["embedded_data"]
    assert cut["description_images"] == {"refused": code}


@pytest.mark.parametrize(
    ("detail_url", "code"),
    [
        ("https://other.test/1688offer/icoss1", "DESCRIPTION_URL_NOT_ALLOWED"),
        ("http://itemcdn.tmall.com/1688offer/icoss1", "DESCRIPTION_URL_NOT_ALLOWED"),
        ("https://itemcdn.tmall.com/1688offer/icoss1?token=abc", "DESCRIPTION_URL_NOT_ALLOWED"),
        ("https://user:pw@itemcdn.tmall.com/1688offer/icoss1", "DESCRIPTION_URL_NOT_ALLOWED"),
        ("https://itemcdn.tmall.com:8443/1688offer/icoss1", "DESCRIPTION_URL_NOT_ALLOWED"),
        ("not a url", "DESCRIPTION_URL_NOT_ALLOWED"),
    ],
)
def test_a_description_url_off_the_declared_host_is_never_read(
    browser: Browser, policy: dict[str, Any], detail_url: str, code: str
) -> None:
    context = CONTEXT.replace(DESCRIPTION_URL, detail_url)
    cut = _cut(browser, _page_html([_context_script(context)]), policy)
    assert cut["description_images"] == {"refused": code}
    # OC-09: the refused URL, its credential and its secret query never leave the page either;
    # the projection is exactly the allowlisted one.
    assert cut["embedded_data"] == {"projection": EXPECTED}
    sent = json.dumps(cut, ensure_ascii=False)
    for absent in (detail_url, "detailUrl", "itemcdn", "token=abc", "user:pw", "icoss1"):
        assert absent not in sent, absent


def test_a_page_whose_member_holds_no_url_reads_nothing(
    browser: Browser, policy: dict[str, Any]
) -> None:
    cut = _cut(browser, _page_html([]), policy)
    assert cut["embedded_data"] == {"refused": "EMBEDDED_SCRIPT_NOT_EXACTLY_ONE"}
    assert cut["description_images"] == {"refused": "DESCRIPTION_URL_ABSENT"}


@pytest.mark.parametrize(
    ("changes", "description", "expected"),
    [
        ({"max_bytes": 64}, None, {"refused": "DESCRIPTION_OVER_BYTES"}),
        (
            {"max_depth": 1},
            (200, 'var offer_details={"content":"x","a":{"b":1}};', {}),
            {"refused": "DESCRIPTION_OVER_DEPTH"},
        ),
        # The cap keeps the first references in document order and says how many there were.
        ({"max_image_refs": 2}, None, {"images": DESCRIBED[:2], "total": 3}),
    ],
)
def test_the_bounds_and_the_cap_are_the_policys(
    browser: Browser,
    policy: dict[str, Any],
    changes: dict[str, Any],
    description: Answer | None,
    expected: dict[str, Any],
) -> None:
    cut = _cut(
        browser, _page_html([_context_script()]), _with(policy, **changes), description=description
    )
    assert cut["description_images"] == expected


def test_the_server_accepts_exactly_what_the_extension_sent(
    browser: Browser, policy: dict[str, Any]
) -> None:
    from app.stages.collect.extension.embedded import description_images_problem

    member = _cut(browser, _page_html([_context_script()]), policy)["description_images"]
    rule = _server_rule().description
    assert description_images_problem(rule, member["images"], member["total"]) is None
