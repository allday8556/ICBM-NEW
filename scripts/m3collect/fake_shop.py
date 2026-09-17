"""A synthetic supplier and transport for the production collection path (M3 Stage-B2).

Everything here is invented: the host, the page, the identity rule, the roles and the image bytes.
It stands in for a supplier so the tests exercise generic COLLECT core — the job, the order of the
steps, the budget, the asset recorder, the revision append and the read-back — and never a real
provider. Nothing in this module opens a connection.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.collect.facts import (
    Availability,
    CollectedFacts,
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    NoticeItem,
    NoticeValue,
    OptionsValue,
    PricesValue,
    ShippingKind,
    ShippingValue,
    SourcePrice,
    StockValue,
    TextValue,
)
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
    SupplierTransport,
    Verdict,
)
from integrations.suppliers.collection import (
    CollectionLimits,
    CollectionProfile,
    DocumentView,
    FetchIssue,
    ImageCandidate,
    ImageResponse,
    ImageRole,
    ImageRoleRules,
    ReadKind,
    SourceIdentity,
    SourceIdentityResult,
    SupplierCollection,
    UnresolvedIdentity,
)
from integrations.suppliers.transport.collection import ImageFetchRefused, RequestBudget
from integrations.suppliers.transport.session_payload import decode_session, encode_session

SUPPLIER_KEY = "fakeshop"
HOST = "shop.collect.invalid"
# A second shop, which numbers its own product 4242 as well. Two suppliers numbering a
# product alike are two products (ARCHITECTURE §14), and neither may pace the other.
OTHER_SUPPLIER_KEY = "othershop"
OTHER_HOST = "shop.other.invalid"
IMAGE_HOST = "img.collect.invalid"
PRODUCT_URL = f"https://{HOST}/product/sample/4242/"
OTHER_PRODUCT_URL = f"https://{OTHER_HOST}/product/sample/4242/"
# The other shop's listing form, which does state its product number. Two suppliers whose
# URLs both name a product 4242 are the case where a supplier-blind lookup goes wrong.
OTHER_LISTED_URL = f"https://{OTHER_HOST}/product/sample/4242/category/7/"
# The same product reached through this shop's listing. Only this form states the product
# number where a reader of the URL alone can see it, so the pair exercises the case ADR-0010
# §4 cares about: paced on the URL until a run proves the identity, on the product after.
LISTED_URL = f"https://{HOST}/product/sample/4242/category/7/"
EXTRACTOR_REVISION = "fakeshop-collect-1"
EXTRACTOR_FINGERPRINT = "c" * 64

PRIMARY_URL = f"https://{IMAGE_HOST}/p/primary.png"
DETAIL_URL = f"https://{IMAGE_HOST}/p/detail.png"
BANNER_URL = f"https://{IMAGE_HOST}/layout/banner.png"


# Two distinguishable PNG bodies. The decoder reads a real header, so these are real headers.
def png(width: int, height: int) -> bytes:
    import struct
    import zlib

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(header))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk))
    )


PRIMARY_BYTES = png(800, 600)
DETAIL_BYTES = png(1200, 900)
OTHER_BYTES = png(640, 480)


UNCLEAR_STOCK = '<span id="stock">the page does not say</span>'


def detail_url(index: int) -> str:
    """The Nth detail reference after the first. The first keeps its historical URL."""
    return DETAIL_URL if index == 0 else f"https://{IMAGE_HOST}/p/detail-{index}.png"


def page(
    *,
    product_id: str = "4242",
    images: bool = True,
    stock_unclear: bool = False,
    product_evidence: int = 2,
) -> str:
    """The product page. ``product_evidence`` is how many references its roles call product
    evidence — one primary and the rest detail — beside one layout banner that is never evidence.
    """
    references = ""
    if images:
        references = f'<img id="primary" src="{PRIMARY_URL}">' + "".join(
            f'<img class="detail" src="{detail_url(i)}">' for i in range(product_evidence - 1)
        )
        references += f'<img id="banner" src="{BANNER_URL}">'
    declared = f'<meta property="product:id" content="{product_id}">' if product_id else ""
    unclear = UNCLEAR_STOCK if stock_unclear else ""
    return f"<html><head>{declared}</head><body>{references}{unclear}</body></html>"


def document(body: str) -> DocumentView:
    return DocumentView(
        kind=ReadKind.PRODUCT_READ,
        status=200,
        path="/product/sample/4242/",
        location=None,
        content_type="text/html",
        body=body,
    )


def _identity(view: DocumentView, source_url: str) -> SourceIdentityResult:
    marker = 'property="product:id" content="'
    start = view.body.find(marker)
    if start < 0:
        return UnresolvedIdentity("the page declares no product number", missing=("meta",))
    value = view.body[start + len(marker) :].split('"', 1)[0]
    if not value.isdigit():
        return UnresolvedIdentity("the declared product number is not a digit string")
    return SourceIdentity(value, ("meta[product:id]",))


def _evidence(locator: str, status: FieldStatus = FieldStatus.CONFIRMED) -> Evidence:
    return Evidence(
        kind=EvidenceKind.DOM_TEXT,
        locator=locator,
        status=status,
        observed="observed" if status is FieldStatus.CONFIRMED else None,
    )


def _confirmed(value: object, locator: str) -> FieldFact:
    return FieldFact(FieldStatus.CONFIRMED, value, (_evidence(locator),))  # type: ignore[arg-type]


def _absent(locator: str) -> FieldFact:
    return FieldFact(FieldStatus.ABSENT, None, (_evidence(locator, FieldStatus.ABSENT),))


def base_fields() -> dict[str, FieldFact]:
    """What this invented page states: every core field read, some coverage genuinely absent."""
    return {
        "original_name": _confirmed(TextValue(text="합성 상품 350ml"), ".name"),
        "prices": _confirmed(
            PricesValue(prices=(SourcePrice(label="판매가", amount_krw=12900),)), ".price"
        ),
        "options": _confirmed(OptionsValue(axes=()), ".options"),
        "stock": _confirmed(StockValue(availability=Availability.ON_SALE), "button.buy"),
        "shipping": _confirmed(
            ShippingValue(
                kind=ShippingKind.CONDITIONAL,
                policy_text="3,000원 (50,000원 이상 무료)",
                fee_krw=3000,
                free_over_krw=50000,
            ),
            ".delivery",
        ),
        "minimum_sale_price": _absent(".minimum-price"),
        "quantity_tiers": _absent(".tiers"),
        "brand": _confirmed(TextValue(text="합성 브랜드"), ".brand"),
        "manufacturer": _absent(".maker"),
        "origin": _confirmed(TextValue(text="국산"), ".origin"),
        "notice": _confirmed(
            NoticeValue(items=(NoticeItem(label="용량", text="350ml"),)), ".notice"
        ),
        "detail_description": _confirmed(TextValue(text="상세 설명"), ".detail"),
    }


def _fields(view: DocumentView) -> Mapping[str, FieldFact]:
    """The supplier's own reading. A page that does not say plainly leaves a core field for
    review, which is what a collection must be able to record without inventing a value."""
    fields = dict(base_fields())
    if UNCLEAR_STOCK in view.body:
        fields["stock"] = FieldFact(
            FieldStatus.REVIEW_REQUIRED,
            None,
            (
                Evidence(
                    kind=EvidenceKind.CONTROL_STATE,
                    locator="#stock",
                    status=FieldStatus.REVIEW_REQUIRED,
                ),
            ),
        )
    return fields


def _url_product_hint(url: str) -> str | None:
    """Which product this URL points at, when its own form says so.

    This shop spells the number out only in its listing form. A plain product URL says nothing
    until the document has been read, which is exactly the transition the interval must survive.
    """
    parts = [segment for segment in url.split("/") if segment]
    if "category" not in parts:
        return None
    number = parts[parts.index("category") - 1]
    return number if number.isdigit() else None


def _classify(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
    """This shop's own image roles, in the page's own order."""
    found = []
    for order, url in enumerate(re.findall(r'src="([^"]+)"', body)):
        if url == PRIMARY_URL:
            found.append(
                ImageCandidate(url=url, role=ImageRole.PRIMARY, order=order, rule="fake.primary")
            )
        elif url.startswith(f"https://{IMAGE_HOST}/p/detail"):
            found.append(
                ImageCandidate(url=url, role=ImageRole.DETAIL, order=order, rule="fake.detail")
            )
        elif url == BANNER_URL:
            found.append(
                ImageCandidate(url=url, role=ImageRole.UI_COMMON, order=order, rule="fake.layout")
            )
    return tuple(found)


def _supplier(key: str, host: str, name: str) -> SupplierProfile:
    return SupplierProfile(
        supplier_key=key,
        display_name=name,
        base_url=f"https://{host}",
        auth_required=True,
        egress_hosts=frozenset({host}),
        request_policy=RequestPolicy(
            max_concurrency=1,
            minimum_request_interval_s=0.0,
            auth_retry_limit=1,
            request_timeout_s=5.0,
        ),
    )


PROFILE = _supplier(SUPPLIER_KEY, HOST, "Fake Shop")
OTHER_PROFILE = _supplier(OTHER_SUPPLIER_KEY, OTHER_HOST, "Other Shop")


def collection_profile(
    *,
    max_image_requests: int = 10,
    max_run_bytes: int = 4 * 1024 * 1024,
    max_image_bytes: int = 1024 * 1024,
    supplier: SupplierProfile = PROFILE,
) -> CollectionProfile:
    return CollectionProfile(
        supplier=supplier,
        product_path=r"/product/[^/]+/\d+(?:/category/\d+)?/?",
        policy_paths=frozenset({"/robots.txt"}),
        image_hosts=frozenset({IMAGE_HOST}),
        safe_query_keys={},
        limits=CollectionLimits(
            max_image_refs=30,
            max_image_bytes=max_image_bytes,
            max_image_requests_per_run=max_image_requests,
            max_new_image_bytes_per_run=max_run_bytes,
            same_product_interval_s=60.0,
        ),
        transport=SupplierTransport.HTTP,
    )


def collection(
    *,
    max_image_requests: int = 10,
    max_run_bytes: int = 4 * 1024 * 1024,
    max_image_bytes: int = 1024 * 1024,
    supplier: SupplierProfile = PROFILE,
) -> SupplierCollection:
    return SupplierCollection(
        profile=collection_profile(
            max_image_requests=max_image_requests,
            max_run_bytes=max_run_bytes,
            max_image_bytes=max_image_bytes,
            supplier=supplier,
        ),
        roles=ImageRoleRules(identity=EXTRACTOR_REVISION, classify=_classify),
        identity=_identity,
        fields=_fields,
        url_product_hint=_url_product_hint,
    )


def collected_for_run(collection_run_id: str, *, source_product_id: str = "4242") -> CollectedFacts:
    """One collection's facts, addressed to a run that already has a revision. No images, so it
    asks nothing of the asset store: only the run's own uniqueness is under test."""
    return CollectedFacts(
        supplier_key=SUPPLIER_KEY,
        source_product_id=source_product_id,
        source_url=PRODUCT_URL,
        captured_at=datetime(2026, 9, 17, tzinfo=UTC),
        extractor_revision=EXTRACTOR_REVISION,
        extractor_fingerprint=EXTRACTOR_FINGERPRINT,
        collection_run_id=collection_run_id,
        correlation_id="rehearsal-correlation",
        fields=base_fields(),
    )


class StubSessions:
    """CONNECT's part of a collection, without CONNECT: one opaque payload, used and dropped."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def collection_session(self, supplier_key: str, *, operator_initiated: bool = False) -> bytes:
        self.asked.append(supplier_key)
        return b"fake-session-payload"


@dataclass
class FakeGateway:
    """The transport, replaced by a script. It sends nothing and knows no host.

    ``documents`` is answered in order, so a second attempt of a job can be served a different
    page; ``images`` maps a URL to what the provider would answer, including a refusal.
    """

    documents: list[str] = field(default_factory=list)
    images: dict[str, object] = field(default_factory=dict)
    document_reads: int = 0
    image_reads: list[str] = field(default_factory=list)
    sessions: list[bytes | None] = field(default_factory=list)

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        budget.reserve(kind, url)
        self.sessions.append(session)
        body = self.documents[min(self.document_reads, len(self.documents) - 1)]
        self.document_reads += 1
        return document(body)

    def read_image(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        budget: RequestBudget,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
    ) -> ImageResponse:
        budget.reserve(ReadKind.IMAGE_REQUEST, url)
        self.image_reads.append(url)
        answer = self.images.get(url)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, ImageResponse):
            return answer
        content = answer if isinstance(answer, bytes) else OTHER_BYTES
        if max_bytes is not None and len(content) > max_bytes:
            # The real gateway refuses a body over the bound as it arrives, so this one does too:
            # the bytes never reach the caller and never reach the store.
            raise ImageFetchRefused(FetchIssue.OVERSIZE, "body over the size bound")
        return ImageResponse(200, "image/png", f'"etag-{len(content)}"', None, content)


def refusal(issue: FetchIssue = FetchIssue.BAD_CONTENT_TYPE) -> ImageFetchRefused:
    return ImageFetchRefused(issue, "the fake provider refused this image")


# ---------------------------------------------------------------- CONNECT

MEMBER_ID = "rehearsal-member@example.invalid"
MEMBER_PASSWORD = "Rehearsal pa$$word/한글"
SESSION_COOKIE = "fakeshop-session-cookie-3c9e"
MYSHOP = "/myshop/index.html"


def connect_definition() -> SupplierDefinition:
    """What CONNECT knows of this shop: a protected page, and how to tell signed in from out."""

    def logged_off(response: ProbeResponse) -> Verdict:
        return ("state-logoff" in response.body, ("state_logoff",))

    def logged_on(response: ProbeResponse) -> Verdict:
        return ("state-logon" in response.body, ("state_logon",))

    return SupplierDefinition(
        profile=PROFILE,
        probe=ProtectedReadProbe(
            target=MYSHOP, unauthenticated_expectation=logged_off, authenticated_predicate=logged_on
        ),
        login=LoginFormSpec(
            path="/member/login.html",
            username_selector="#id",
            password_selector="#pw",
            submit_selector="#go",
        ),
    )


@dataclass
class FakeConnect:
    """The shop's CONNECT transport, counted. It knows one member and one session cookie."""

    fetches: list[str] = field(default_factory=list)
    logins: int = 0

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        self.fetches.append(kind.value)
        signed_in = False
        if kind is RequestKind.PROTECTED_READ and session is not None:
            cookies, _ = decode_session(session)
            signed_in = any(cookie["value"] == SESSION_COOKIE for cookie in cookies)
        body = '<div class="state-logon"></div>' if signed_in else '<div class="state-logoff">'
        return ProbeResponse(status=200, path=definition.probe.target, location=None, body=body)

    def login(self, definition: SupplierDefinition, credentials: Credentials) -> bytes:
        self.logins += 1
        if credentials != Credentials(MEMBER_ID, MEMBER_PASSWORD):
            raise AuthError("SUPPLIER_LOGIN_REJECTED", "the rehearsal shop rejected the login")
        return encode_session(
            [{"name": "SID", "value": SESSION_COOKIE, "domain": HOST, "path": "/"}],
            user_agent="Rehearsal/1",
            hosts={HOST},
        )
