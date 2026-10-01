"""The extension's own code, run in a real browser (ADR-0019 §6; E1 specification §5, §11.2).

``ui/extension/lib/capture.js`` is the function the extension injects into the captured tab. Here
the same file is loaded into an ordinary page of a real browser and run against a synthetic
KM통상-shaped page. Every request the page makes is answered by the test itself, so nothing can
reach a network: the supplier's host exists only as the address of a fulfilled route.

What it proves:

- the C1 regression pair — the shared non-product anchor is absent because it lies outside the
  scope, and private material placed inside the scope still makes the server refuse;
- the cut is exactly what the reviewed policy allows, and the server accepts exactly that;
- the canonical KM extractor reads the same facts from the extension's ``DocumentView`` as from the
  direct one;
- the preconditions refuse before anything is cut, and a bound refuses the whole capture.
"""

import copy
import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.container import Container, _server_final_scan
from app.platform.core.clock import SystemClock
from app.platform.core.secrets import MemorySecretStore
from app.stages.collect.adaptive.engine.capture import CaptureRefused, capture_candidate
from app.stages.collect.extension.capture import CaptureEnvelope, measure, policy_violations
from app.stages.collect.extension.nonces import NonceCache
from app.stages.collect.extension.pairing import ExtensionPairing
from app.stages.collect.extension.policy import CapturePolicySource
from app.stages.collect.models import CollectionOutcome
from integrations.suppliers.collection import DocumentView, ReadKind, SourceIdentity
from integrations.suppliers.kmretail.collection import COLLECTION
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.extension_support import (
    CAPTURES,
    EXTENSION_ID,
    EXTENSION_ROOT,
    FIXTURE,
    ICBM_ORIGIN,
    KM_POLICY,
    ORIGIN,
    PRODUCT_NUMBER,
    PRODUCT_URL,
    REPO_ROOT,
    SUPPLIER,
    envelope,
)

pytestmark = pytest.mark.integration

MODULE_BASE = "https://kmretail.co.kr/__icbm_extension__/"
CUT = """async (policy) => {
  const module = await import("/__icbm_extension__/lib/capture.js");
  return module.captureInPage(policy);
}"""
PRODUCT_ROLES = {"PRIMARY", "THUMBNAIL", "DETAIL", "PRODUCT_AUX"}


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
    loaded: dict[str, Any] = json.loads(KM_POLICY.read_text("utf-8"))
    return loaded


@pytest.fixture(scope="module")
def fixture_html() -> str:
    return FIXTURE.read_text("utf-8")


Serve = Callable[[Route], None]


def _page(
    browser: Browser, html: str, *, url: str = PRODUCT_URL, serve: Serve | None = None
) -> Page:
    """A page at ``url`` whose every request the test answers. Nothing is ever sent anywhere."""

    def answer(route: Route) -> None:
        requested = route.request.url
        if requested.startswith(MODULE_BASE):
            source = EXTENSION_ROOT / requested.removeprefix(MODULE_BASE)
            route.fulfill(
                status=200, content_type="text/javascript", body=source.read_text("utf-8")
            )
        elif serve is not None and requested == url:
            serve(route)
        elif requested == url:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        else:
            route.abort()

    page = browser.new_page()
    page.route("**/*", answer)
    page.goto(url)
    return page


def _cut(
    browser: Browser, html: str, policy: dict[str, Any], **page_options: Any
) -> dict[str, Any]:
    page = _page(browser, html, **page_options)
    try:
        result: dict[str, Any] = page.evaluate(CUT, policy)
        return result
    finally:
        page.close()


def _server_policy() -> Any:
    return CapturePolicySource(REPO_ROOT / "integrations" / "suppliers").load(SUPPLIER)


# ---------------------------------------------------------------- the cut


def test_the_cut_is_exactly_what_the_reviewed_policy_allows(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    cut = _cut(browser, fixture_html, policy)
    assert cut["ok"] is True
    html = cut["html"]
    # The server finds nothing the policy does not allow, and measures what the browser bounded.
    assert policy_violations(html, _server_policy()) == ()
    measured = measure(html)
    assert measured.html_bytes <= policy["bounds"]["max_html_bytes"]
    # The key image, two additional images and two description images.
    assert measured.image_refs == 5
    # The product scope: the product module and the description, and nothing beside them.
    assert 'class="xans-element- xans-product xans-product-detail"' in html
    assert 'id="prdDetail"' in html and "합성 샘플 상품 1kg" in html and "12,000원" in html
    # From <head>, only the three identity declarations (ruling B-10).
    head = html.split("<head>", 1)[1].split("</head>", 1)[0]
    assert head.count("<") == 3
    assert 'property="product:productId"' in head and 'rel="canonical"' in head
    for left_out in ("og:title", "og:image", "product:price:amount", "<title", "stylesheet"):
        assert left_out not in html, left_out
    # What the browser observed of the navigation, and nothing it did not.
    assert cut["transport"] == {
        "url": PRODUCT_URL,
        "navigation_name": PRODUCT_URL,
        "response_status": 200,
        "redirect_count": 0,
        "content_type": "text/html",
        "character_set": "UTF-8",
    }
    assert set(cut) == {"ok", "html", "transport"}


def test_c1_regression_the_shared_non_product_anchor_is_outside_the_scope(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    # The stopped C1 cut a candidate from the whole page, and the layout's shared anchors refused
    # it. The scope is cut first here, so that material is simply not there (ADR-0019 §6).
    assert "help@synthetic.invalid" in fixture_html and "member-link" in fixture_html
    with pytest.raises(CaptureRefused):
        capture_candidate(fixture_html)  # the whole page is what C1 tried, and it still refuses
    html = _cut(browser, fixture_html, policy)["html"]
    for outside in (
        "help@synthetic.invalid",
        "cs-link",
        "member-link",
        "xans-layout-logotop",
        "addr",
    ):
        assert outside not in html, outside
    # No exception was made for an href or an anchor's text: there is no anchor at all.
    assert "<a" not in html and "href" not in html.split("</head>", 1)[1]
    capture_candidate(html)  # the server's own sanitizer and final scan accept the scope


def test_the_cut_drops_every_excluded_region_tag_and_attribute(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    html = _cut(browser, fixture_html, policy)["html"]
    for excluded in (
        "xans-product-review",
        "배송이 빨라요",
        "9,900원",
        "xans-product-relation",
        "연관상품 구매",
        "related-1.jpg",
        "<script",
        "trackDetail",
        "layoutState",
        "<!--",
        "onclick",
        "alt=",
        "data-member-token",
        "not-a-real-token",
        "value=",
        "name=",
        'scope="row"' if "scope" not in policy["allowed_attributes"].get("th", []) else "\u0000",
    ):
        assert excluded not in html, excluded


def test_the_member_benefit_box_never_leaves_the_browser(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    # The first real KM통상 attempts (EXTENSION-E1.md §5.1) were refused by the server's final
    # gate for a ``p.member`` and an image inside the product scope: the signed-in member's
    # benefit box (the member's name and grade, beside a profile image). Policy
    # kmretail-capture-2 cuts that box, with everything it holds, in the browser.
    benefit = (
        '<div class="xans-element- xans-myshop xans-myshop-asyncbenefit">'
        '<p><img src=""></p><div><p class="member">합성회원 님은 [합성등급] 회원이십니다.</p>'
        "</div></div>"
    )
    anchor = '<div class="xans-element- xans-product xans-product-action">'
    assert fixture_html.count(anchor) == 1
    html = _cut(browser, fixture_html.replace(anchor, benefit + anchor), policy)["html"]
    for gone in ("asyncbenefit", 'class="member"', "합성회원 님은", 'src=""'):
        assert gone not in html, gone
    assert _server_final_scan(html) == ()


def test_a_member_named_element_outside_the_box_is_refused_never_dropped(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    # Only the benefit box is cut. Any other element whose class or id names a member — a
    # supplier may mark a member price that way — is not cut silently: it is sent, and the
    # server's gate refuses the run and names it, so a price is never lost unnoticed. The words
    # "회원가" in a page's text are no finding at all.
    anchor = '<div class="xans-element- xans-product xans-product-action">'
    labelled = '<table><tr><th scope="row">회원가</th><td>10,000원</td></tr></table>'
    html = _cut(browser, fixture_html.replace(anchor, labelled + anchor), policy)["html"]
    assert "회원가" in html and _server_final_scan(html) == ()
    named = '<p class="member_price">10,000원</p>'
    html = _cut(browser, fixture_html.replace(anchor, named + anchor), policy)["html"]
    assert 'class="member_price"' in html
    assert _server_final_scan(html) == ("SANITIZER_EXCLUDED:PRIVATE@p#.member_price",)


def test_the_cut_never_modifies_the_page(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    page = _page(browser, fixture_html)
    try:
        before = page.evaluate("document.documentElement.outerHTML")
        assert page.evaluate(CUT, policy)["ok"] is True
        assert page.evaluate("document.documentElement.outerHTML") == before
    finally:
        page.close()


# ---------------------------------------------------------------- the same DocumentView


def _document(body: str) -> DocumentView:
    return DocumentView(
        kind=ReadKind.PRODUCT_READ,
        status=200,
        path="/product/synthetic-sample/9001/",
        location=None,
        content_type="text/html",
        body=body,
    )


def test_the_canonical_extractor_reads_the_same_facts_from_both_transports(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    direct, captured = (
        _document(fixture_html),
        _document(_cut(browser, fixture_html, policy)["html"]),
    )
    # The identity: the same product, agreed by the same four declarations.
    identities = [COLLECTION.identity(view, PRODUCT_URL) for view in (direct, captured)]
    assert identities[0] == identities[1]
    assert isinstance(identities[0], SourceIdentity)
    assert identities[0].source_product_id == PRODUCT_NUMBER
    # Every field: the same status and the same value.
    from_direct, from_capture = COLLECTION.fields(direct), COLLECTION.fields(captured)
    assert set(from_direct) == set(from_capture)
    for key, fact in from_direct.items():
        other = from_capture[key]
        assert (fact.status, fact.value) == (other.status, other.value), key
    # The evidence differs in exactly one, stated way: the head declarations the KM capture policy
    # does not keep (ruling B-10) are not there to be quoted. Nothing else differs, and nothing is
    # filled in for them. A later comparison reports this under the existing EVIDENCE_DRIFT rules.
    only_direct = {
        (key, evidence.locator)
        for key, fact in from_direct.items()
        for evidence in fact.evidence
        if evidence not in from_capture[key].evidence
    }
    only_capture = {
        (key, evidence.locator)
        for key, fact in from_capture.items()
        for evidence in fact.evidence
        if evidence not in from_direct[key].evidence
    }
    assert only_direct == {
        ("original_name", "meta[og:title]"),
        ("prices", "meta[product:price:amount]"),
        # The direct document's third purchase control is the related-products block's own,
        # outside the product scope. The capture never holds it.
        ("stock", "span.btnBuy"),
    }
    assert only_capture == set()

    # The product images: the same references under the same roles. The direct document also
    # carries the layout's own images, which are never product evidence on either transport.
    def product_images(view: DocumentView) -> set[tuple[str, str]]:
        return {
            (candidate.role.value, candidate.url)
            for candidate in COLLECTION.roles.classify(view.body, PRODUCT_URL)
            if candidate.role.value in PRODUCT_ROLES
        }

    assert product_images(captured) <= product_images(direct)
    assert {url for _, url in product_images(direct) - product_images(captured)} == set()


# ---------------------------------------------------------------- C1 regression, second half


def test_c1_regression_private_material_inside_the_scope_still_refuses(
    browser: Browser, policy: dict[str, Any], fixture_html: str, container: Container
) -> None:
    private = fixture_html.replace(
        '<tr><th scope="row">원산지</th><td>국산</td></tr>',
        '<tr><th scope="row">원산지</th><td>국산</td></tr>'
        '<tr><th scope="row">문의</th><td>010-0000-0000</td></tr>',
    )
    assert private != fixture_html
    cut = _cut(browser, private, policy)
    # The browser cuts topology; it makes no exception and hides nothing.
    assert cut["ok"] is True and "010-0000-0000" in cut["html"]
    with pytest.raises(CaptureRefused):
        capture_candidate(cut["html"])
    # Through the real ingest owner: accepted, then the server's final scan fails the run.
    accepted = container.extension_capture.ingest(
        CaptureEnvelope.model_validate(envelope(cut["html"], transport=cut["transport"]))
    )
    assert container.runner.run_next() is not None
    run = container.collection.run(accepted.collection_run_id)
    assert (run.outcome, run.detail) == (CollectionOutcome.FAILED, "EXTENSION_FINAL_SCAN_REFUSED")
    assert run.revision_id is None


def test_a_clean_capture_passes_the_real_ingest(
    browser: Browser, policy: dict[str, Any], fixture_html: str, container: Container
) -> None:
    cut = _cut(browser, fixture_html, policy)
    accepted = container.extension_capture.ingest(
        CaptureEnvelope.model_validate(envelope(cut["html"], transport=cut["transport"]))
    )
    assert container.runner.run_next() is not None
    run = container.collection.run(accepted.collection_run_id)
    assert (run.outcome, run.detail) == (CollectionOutcome.RECORDED, None)
    assert run.revision_id is not None and run.source_product_id == PRODUCT_NUMBER


# ---------------------------------------------------------------- preconditions (§11.2)


class _LocalShop(BaseHTTPRequestHandler):
    """A real loopback server: one redirecting path, one final page and the extension's module.
    A redirect has to be a real one — a route handler is never offered the follow-up request."""

    page_html = ""

    def do_GET(self) -> None:
        if self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        if self.path.startswith("/__icbm_extension__/"):
            body = (EXTENSION_ROOT / self.path.removeprefix("/__icbm_extension__/")).read_bytes()
            kind = "text/javascript"
        else:
            body, kind = self.page_html.encode("utf-8"), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture
def local_shop(fixture_html: str) -> Iterator[str]:
    handler = type("Shop", (_LocalShop,), {"page_html": fixture_html})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_redirected_navigation_refuses_before_anything_is_cut(
    browser: Browser, policy: dict[str, Any], local_shop: str
) -> None:
    page = browser.new_page()
    try:
        # Reached through a redirect: the browser says so, and nothing is read, cut or sent.
        page.goto(f"{local_shop}/hop")
        assert page.url == f"{local_shop}/final"
        assert page.evaluate(CUT, policy) == {"ok": False, "code": "EVIDENCE_REDIRECTED"}
        # The final URL loaded directly passes that check. (This loopback page is not the reviewed
        # supplier host, which is the next thing the function refuses.)
        page.goto(f"{local_shop}/final")
        assert page.evaluate(CUT, policy) == {"ok": False, "code": "HOST_NOT_REVIEWED"}
    finally:
        page.close()


def test_the_test_browser_can_reach_nothing_but_the_loopback(
    browser: Browser, local_shop: str
) -> None:
    # The guard of this whole module: no host name resolves, so no test here can send a request
    # off this machine — a redirect follow-up included. ``localhost`` would otherwise resolve.
    page = browser.new_page()
    try:
        with pytest.raises(PlaywrightError, match="ERR_NAME_NOT_RESOLVED"):
            page.goto(local_shop.replace("127.0.0.1", "localhost") + "/final")
        page.route(
            "**/*",
            lambda route: route.fulfill(
                status=302, headers={"Location": local_shop.replace("127.0.0.1", "localhost")}
            ),
        )
        with pytest.raises(PlaywrightError, match="ERR_NAME_NOT_RESOLVED"):
            page.goto("https://shop.blocked.invalid/hop")
        page.unroute("**/*")
        # A literal address is a host like any other: only 127.0.0.1 itself is reachable. Both
        # of these stay on this machine whatever the browser does with them.
        port = local_shop.rsplit(":", 1)[1]
        for literal in (f"http://127.0.0.2:{port}/final", f"http://[::1]:{port}/final"):
            with pytest.raises(PlaywrightError, match="ERR_NAME_NOT_RESOLVED"):
                page.goto(literal)
    finally:
        page.close()


def test_a_location_that_is_not_the_navigated_url_refuses(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    page = _page(browser, fixture_html)
    try:
        page.evaluate("history.pushState({}, '', '/product/synthetic-sample/9002/')")
        assert page.evaluate(CUT, policy) == {"ok": False, "code": "EVIDENCE_URL_NOT_NAVIGATED"}
    finally:
        page.close()


@pytest.mark.parametrize(
    ("status", "content_type", "code"),
    [
        (404, "text/html; charset=utf-8", "EVIDENCE_STATUS_NOT_200"),
        (500, "text/html; charset=utf-8", "EVIDENCE_STATUS_NOT_200"),
        (200, "application/xhtml+xml", "EVIDENCE_CONTENT_TYPE"),
    ],
)
def test_a_navigation_the_browser_did_not_observe_as_html_200_refuses(
    browser: Browser,
    policy: dict[str, Any],
    fixture_html: str,
    status: int,
    content_type: str,
    code: str,
) -> None:
    body = (
        fixture_html
        if "xhtml" not in content_type
        else '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title></head>'
        '<body><div class="xans-product-detail"></div></body></html>'
    )

    def serve(route: Route) -> None:
        route.fulfill(status=status, content_type=content_type, body=body)

    assert _cut(browser, fixture_html, policy, serve=serve) == {"ok": False, "code": code}


def test_a_host_the_policy_does_not_name_refuses(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    other = copy.deepcopy(policy)
    other["host"] = "shop.other.invalid"
    assert _cut(browser, fixture_html, other) == {"ok": False, "code": "HOST_NOT_REVIEWED"}


# ---------------------------------------------------------------- the scope and the bounds


def test_the_product_root_must_resolve_to_exactly_one_element(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    none = fixture_html.replace("xans-product-detail", "xans-product-other")
    two = fixture_html.replace(
        '<div id="footer" class="addr">', '<div class="xans-product-detail"></div><div id="footer">'
    )
    for html in (none, two):
        assert _cut(browser, html, policy) == {"ok": False, "code": "PRODUCT_ROOT_NOT_EXACTLY_ONE"}


@pytest.mark.parametrize(
    ("bound", "value", "code"),
    [
        ("max_nodes", 10, "CEILING_NODES"),
        ("max_image_refs", 4, "CEILING_IMAGE_REFS"),
        ("max_html_bytes", 600, "CEILING_HTML_BYTES"),
    ],
)
def test_a_capture_over_a_bound_is_refused_whole_never_truncated(
    browser: Browser, policy: dict[str, Any], fixture_html: str, bound: str, value: int, code: str
) -> None:
    tight = copy.deepcopy(policy)
    tight["bounds"][bound] = value
    refused = _cut(browser, fixture_html, tight)
    # A refusal carries a code and nothing of the page.
    assert refused == {"ok": False, "code": code}


def test_the_browser_counts_what_the_server_counts(
    browser: Browser, policy: dict[str, Any], fixture_html: str
) -> None:
    # A bound set to exactly what the capture holds is accepted; one less refuses. So the browser
    # and the server measure the same elements and image references.
    html = _cut(browser, fixture_html, policy)["html"]
    measured = measure(html)
    for bound, held, code in (
        ("max_nodes", measured.nodes, "CEILING_NODES"),
        ("max_image_refs", measured.image_refs, "CEILING_IMAGE_REFS"),
        ("max_html_bytes", measured.html_bytes, "CEILING_HTML_BYTES"),
    ):
        exact = copy.deepcopy(policy)
        exact["bounds"][bound] = held
        assert _cut(browser, fixture_html, exact)["ok"] is True, bound
        exact["bounds"][bound] = held - 1
        assert _cut(browser, fixture_html, exact) == {"ok": False, "code": code}, bound


# ---------------------------------------------------------------- the signature, on both sides


def test_the_extension_signature_is_the_one_the_server_verifies(
    browser: Browser, fixture_html: str
) -> None:
    clock = SystemClock()
    pairing = ExtensionPairing(MemorySecretStore(), clock, NonceCache(clock))
    issued = pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN)
    page = _page(browser, fixture_html)
    try:
        signed = page.evaluate(
            """async ([code, extensionId, path, body]) => {
              const signing = await import("/__icbm_extension__/lib/signing.js");
              const pairing = signing.parsePairingCode(code);
              const bytes = new TextEncoder().encode(body);
              const headers = await signing.signedHeaders({
                method: "POST", path, extensionId, pairing, body: bytes,
              });
              return { pairing, headers };
            }""",
            [issued.code, EXTENSION_ID, CAPTURES, '{"a":"가"}'],
        )
    finally:
        page.close()
    # The pairing code is parsed to exactly what the server issued, the loopback origin included.
    assert signed["pairing"] == {
        "origin": ICBM_ORIGIN,
        "pairing_id": issued.record.pairing_id,
        "generation": 1,
        "secret": issued.record.secret,
    }
    sender = pairing.verify(method="POST", path=CAPTURES, origin=ORIGIN, headers=signed["headers"])
    import hashlib

    assert sender.extension_id == EXTENSION_ID
    assert sender.body_sha256 == hashlib.sha256('{"a":"가"}'.encode()).hexdigest()
    assert signed["headers"]["X-ICBM-Client"]
    # Exactly the signed headers and the client header: no cookie, no authorization, nothing else.
    assert set(signed["headers"]) == {
        "X-ICBM-Client",
        "X-ICBM-Extension-Id",
        "X-ICBM-Pairing-Id",
        "X-ICBM-Pairing-Generation",
        "X-ICBM-Timestamp",
        "X-ICBM-Nonce",
        "X-ICBM-Body-SHA256",
        "X-ICBM-Signature",
    }


def test_a_pairing_code_for_anything_but_the_loopback_is_refused(
    browser: Browser, fixture_html: str
) -> None:
    clock = SystemClock()
    pairing = ExtensionPairing(MemorySecretStore(), clock, NonceCache(clock))
    codes = [
        pairing.pair(EXTENSION_ID, origin=origin).code
        for origin in (
            "https://kmretail.co.kr",
            "http://192.168.0.10:8790",
            "http://localhost:8790",
        )
        if pairing.revoke() or True
    ]
    page = _page(browser, fixture_html)
    try:
        parsed = page.evaluate(
            """async (codes) => {
              const signing = await import("/__icbm_extension__/lib/signing.js");
              return [...codes, "not-a-code", ""].map((code) => signing.parsePairingCode(code));
            }""",
            codes,
        )
    finally:
        page.close()
    assert parsed == [None] * 5
