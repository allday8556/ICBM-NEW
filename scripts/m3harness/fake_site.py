"""A synthetic storefront for DRY rehearsals. Nothing here is real supplier data: the host, the
pages, the member name, the prices and the image bytes are invented, and the page carries the
kinds of material the reconnaissance must never let out (a member name, a price, a hidden token,
a signed image URL) so a rehearsal can prove they stay inside."""

import threading
from urllib.parse import urljoin

import httpx

from app.core.errors import AuthError
from integrations.suppliers.base import (
    Credentials,
    LoginFormSpec,
    ProbeResponse,
    ProtectedReadProbe,
    RequestKind,
    RequestPolicy,
    SupplierDefinition,
    SupplierProfile,
    Verdict,
)
from integrations.suppliers.collection import (
    ImageCandidate,
    ImageRole,
    ImageRoleRules,
)
from integrations.suppliers.transport.session_payload import decode_session, encode_session

HOST = "shop.recon.invalid"
IMAGE_HOSTS = ("img.recon.invalid", "cdn.recon-assets.invalid")
PRODUCT_PATH = "/product/sample-item/1234/category/56/"
PRODUCT_URL = f"https://{HOST}{PRODUCT_PATH}"
USERNAME = "recon-operator@example.invalid"
PASSWORD = "Recon pa$$ word/한글"
MEMBER_NAME = "리허설회원"
PRICE_TEXT = "12,900원"
HIDDEN_TOKEN = "hidden-token-rehearsal-7f3a"
SIGNATURE = "REHEARSAL-SIGNATURE-0001"
SESSION_COOKIE = "rehearsal-session-cookie-9d1e"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

PAGE = f"""<html><head>
<meta property="og:type" content="product">
<script type="application/ld+json">
{{"@type": "Product", "sku": "1234", "name": "합성 상품"}}</script>
</head><body>
<div class="xans-layout-statelogon">{MEMBER_NAME}님</div>
<form action="/exec/front/order/basket/" method="post">
  <input type="hidden" name="product_no" value="1234">
  <input type="hidden" name="page_token" value="{HIDDEN_TOKEN}">
  <select name="option1" class="ProductOption0"><option>1kg x 2</option></select>
</form>
<span id="span_product_price_text" class="price">{PRICE_TEXT}</span>
<p class="delivery">배송비 3,000원</p><a class="btnBuy buy">구매하기</a>
<div class="siteBanner"><img src="//{IMAGE_HOSTS[0]}/banner/site.png"></div>
<div class="thumbnail"><img src="//{IMAGE_HOSTS[0]}/p/1234.png"></div>
<div id="prdDetail"><img src="https://{IMAGE_HOSTS[1]}/d/1.jpg?X-Amz-Signature={SIGNATURE}"></div>
<footer><a href="/member/agreement.html">이용약관</a></footer>
</body></html>"""
ROBOTS = "User-agent: *\nDisallow: /member/\nAllow: /member/agreement.html\n"
TERMS = "<html><body><p>무단 수집을 금지합니다.</p></body></html>"


def _classify_images(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
    """The rehearsal storefront's own image-role knowledge: one detail image in its description
    block, one thumbnail, and a layout banner that must never consume a sample slot."""
    found: list[ImageCandidate] = []
    for order, (marker, role, rule) in enumerate(
        (
            ("thumbnail", ImageRole.THUMBNAIL, "fake.thumbnail"),
            ("prdDetail", ImageRole.DETAIL, "fake.detail"),
            ("siteBanner", ImageRole.UI_COMMON, "fake.banner"),
        )
    ):
        for block in body.split(marker)[1:]:
            source = block.split('src="', 1)[1].split('"', 1)[0]
            found.append(
                ImageCandidate(url=urljoin(product_url, source), role=role, order=order, rule=rule)
            )
    return tuple(found)


IMAGE_ROLES = ImageRoleRules(identity="reconfake-images-1", classify=_classify_images)


def definition() -> SupplierDefinition:
    def logged_off(response: ProbeResponse) -> Verdict:
        return ("state-logoff" in response.body, ("state_logoff",))

    def logged_on(response: ProbeResponse) -> Verdict:
        return ("state-logon" in response.body, ("state_logon",))

    return SupplierDefinition(
        profile=SupplierProfile(
            supplier_key="reconfake",
            display_name="Rehearsal storefront",
            base_url=f"https://{HOST}",
            auth_required=True,
            egress_hosts=frozenset({HOST}),
            request_policy=RequestPolicy(minimum_request_interval_s=0.0),
        ),
        probe=ProtectedReadProbe(
            target="/myshop/index.html",
            unauthenticated_expectation=logged_off,
            authenticated_predicate=logged_on,
        ),
        login=LoginFormSpec(
            path="/member/login.html",
            username_selector="#id",
            password_selector="#pw",
            submit_selector="#go",
        ),
    )


class FakeConnectGateway:
    """The CONNECT side of the rehearsal storefront: one accepted login, cookie sessions."""

    def __init__(self) -> None:
        self.logins = 0
        self._lock = threading.Lock()

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        signed_in = False
        if kind is RequestKind.PROTECTED_READ and session is not None:
            cookies, _ = decode_session(session)
            signed_in = any(cookie["value"] == SESSION_COOKIE for cookie in cookies)
        body = '<div class="state-logon"></div>' if signed_in else '<div class="state-logoff">'
        return ProbeResponse(status=200, path=definition.probe.target, location=None, body=body)

    def login(self, definition: SupplierDefinition, credentials: Credentials) -> bytes:
        with self._lock:
            self.logins += 1
        if credentials != Credentials(USERNAME, PASSWORD):
            raise AuthError("SUPPLIER_LOGIN_REJECTED", "the rehearsal storefront rejected it")
        return encode_session(
            [{"name": "SID", "value": SESSION_COOKIE, "domain": HOST, "path": "/"}],
            user_agent="Rehearsal/1",
            hosts={HOST},
        )


class FakeStorefront:
    """The COLLECT side: answers the collection gateway through an ``httpx.MockTransport``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == HOST and url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if url.host == HOST and url.path == PRODUCT_PATH:
            if SESSION_COOKIE not in request.headers.get("cookie", ""):
                return httpx.Response(
                    302, headers={"location": f"https://{HOST}/member/login.html"}
                )
            return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
        if url.host == HOST and url.path == "/member/agreement.html":
            return httpx.Response(200, text=TERMS, headers={"content-type": "text/html"})
        if url.host in IMAGE_HOSTS:
            etag = '"v1"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304, headers={"etag": etag})
            body, media = (PNG, "image/png") if url.path.endswith(".png") else (JPEG, "image/jpeg")
            return httpx.Response(200, content=body, headers={"content-type": media, "etag": etag})
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
