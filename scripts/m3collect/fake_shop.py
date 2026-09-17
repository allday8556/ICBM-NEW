"""A synthetic supplier and transport for the production collection path (M3 Stage-B2).

Everything here is invented: the host, the page, the identity rule, the roles and the image bytes.
It stands in for a supplier so the tests exercise generic COLLECT core — the job, the order of the
steps, the budget, the asset recorder, the revision append and the read-back — and never a real
provider. Nothing in this module opens a connection.
"""

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
from integrations.suppliers.base import RequestPolicy, SupplierProfile, SupplierTransport
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

SUPPLIER_KEY = "fakeshop"
HOST = "shop.collect.invalid"
IMAGE_HOST = "img.collect.invalid"
PRODUCT_URL = f"https://{HOST}/product/sample/4242/"
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


def page(*, product_id: str = "4242", images: bool = True, stock_unclear: bool = False) -> str:
    references = (
        f'<img id="primary" src="{PRIMARY_URL}">'
        f'<img id="detail" src="{DETAIL_URL}">'
        f'<img id="banner" src="{BANNER_URL}">'
        if images
        else ""
    )
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


def _classify(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
    found = []
    for order, (url, role, rule) in enumerate(
        (
            (PRIMARY_URL, ImageRole.PRIMARY, "fake.primary"),
            (DETAIL_URL, ImageRole.DETAIL, "fake.detail"),
            (BANNER_URL, ImageRole.UI_COMMON, "fake.layout"),
        )
    ):
        if url in body:
            found.append(ImageCandidate(url=url, role=role, order=order, rule=rule))
    return tuple(found)


PROFILE = SupplierProfile(
    supplier_key=SUPPLIER_KEY,
    display_name="Fake Shop",
    base_url=f"https://{HOST}",
    auth_required=True,
    egress_hosts=frozenset({HOST}),
    request_policy=RequestPolicy(
        max_concurrency=1,
        minimum_request_interval_s=0.0,
        auth_retry_limit=1,
        request_timeout_s=5.0,
    ),
)


def collection_profile(
    *, max_image_requests: int = 10, max_run_bytes: int = 4 * 1024 * 1024
) -> CollectionProfile:
    return CollectionProfile(
        supplier=PROFILE,
        product_path=r"/product/[^/]+/\d+/?",
        policy_paths=frozenset({"/robots.txt"}),
        image_hosts=frozenset({IMAGE_HOST}),
        safe_query_keys={},
        limits=CollectionLimits(
            max_image_refs=30,
            max_image_bytes=1024 * 1024,
            max_image_requests_per_run=max_image_requests,
            max_new_image_bytes_per_run=max_run_bytes,
            same_product_interval_s=60.0,
        ),
        transport=SupplierTransport.HTTP,
    )


def collection(
    *, max_image_requests: int = 10, max_run_bytes: int = 4 * 1024 * 1024
) -> SupplierCollection:
    return SupplierCollection(
        profile=collection_profile(
            max_image_requests=max_image_requests, max_run_bytes=max_run_bytes
        ),
        roles=ImageRoleRules(identity=EXTRACTOR_REVISION, classify=_classify),
        identity=_identity,
        fields=_fields,
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
