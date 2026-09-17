"""What the source wrote, what it resolves to, and why a target is refused (Issue #52 ruling
5716978033, PR-1).

The transport's target check is the one judge of fetchability. It refuses a URL for exactly one
reason from a closed vocabulary, always the same reason for the same URL, and nothing is reserved or
sent for a refused URL. The written form of a reference is read from the source text alone. No test
here reaches a provider: the gateway runs over a mock transport.
"""

from dataclasses import replace

import httpx
import pytest

from app.collect.facts import (
    FetchTargetRefusal,
    FieldStatus,
    ImageIssue,
    ImageReference,
    ImageRole,
    LocatorForm,
    evaluate,
)
from app.core.errors import InputValidationError
from integrations.suppliers.collection import (
    CollectionLimits,
    CollectionProfile,
    ImageCandidate,
    ReadKind,
)
from integrations.suppliers.collection import ImageRole as SourceRole
from integrations.suppliers.transport.collection import (
    CollectionTargetRefused,
    PolicedCollectionGateway,
    check_discovered_policy,
    check_target,
    image_fetch_target,
)
from tests.collect_support import REPRESENTATIVE, collected
from tests.suppliers import fake_definition

IMAGE_HOST = "img.supplier.test"
IMAGE = f"https://{IMAGE_HOST}/p/1234.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic"


def _profile() -> CollectionProfile:
    return CollectionProfile(
        supplier=fake_definition().profile,
        product_path=r"/products/[0-9]+",
        policy_paths=frozenset({"/robots.txt"}),
        image_hosts=frozenset({IMAGE_HOST}),
        safe_query_keys={"supplier.test": frozenset({"variant"})},
        limits=CollectionLimits(
            max_image_refs=20,
            max_image_bytes=512,
            max_image_requests_per_run=10,
            max_new_image_bytes_per_run=4096,
            same_product_interval_s=60.0,
        ),
    )


class Budget:
    def __init__(self) -> None:
        self.reserved: list[tuple[ReadKind, str]] = []

    def reserve(self, kind: ReadKind, subject: str) -> None:
        self.reserved.append((kind, subject))


class Site:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)


# ---------------------------------------------------------------- the image target check

REFUSED_IMAGE_TARGETS = [
    ("relative", "p/1234.png", FetchTargetRefusal.NOT_ABSOLUTE),
    ("root-relative", "/p/1234.png", FetchTargetRefusal.NOT_ABSOLUTE),
    ("protocol-relative", f"//{IMAGE_HOST}/p/1234.png", FetchTargetRefusal.NOT_ABSOLUTE),
    ("http", f"http://{IMAGE_HOST}/p/1234.png", FetchTargetRefusal.NON_HTTPS),
    ("leading whitespace", f" {IMAGE}", FetchTargetRefusal.WHITESPACE),
    ("trailing whitespace", f"{IMAGE}\n", FetchTargetRefusal.WHITESPACE),
    ("internal whitespace", f"https://{IMAGE_HOST}/p/12 34.png", FetchTargetRefusal.WHITESPACE),
    ("internal tab", f"https://{IMAGE_HOST}/p/12\t34.png", FetchTargetRefusal.WHITESPACE),
    ("fragment", f"{IMAGE}#top", FetchTargetRefusal.FRAGMENT),
    ("empty fragment", f"{IMAGE}#", FetchTargetRefusal.FRAGMENT),
    (
        "user and password",
        f"https://u:p@{IMAGE_HOST}/p/1234.png",
        FetchTargetRefusal.CREDENTIALS_PRESENT,
    ),
    ("user only", f"https://u@{IMAGE_HOST}/p/1234.png", FetchTargetRefusal.CREDENTIALS_PRESENT),
    ("empty userinfo", f"https://@{IMAGE_HOST}/p/1234.png", FetchTargetRefusal.CREDENTIALS_PRESENT),
    ("port 8443", f"https://{IMAGE_HOST}:8443/p/1234.png", FetchTargetRefusal.NON_STANDARD_PORT),
    ("port 80", f"https://{IMAGE_HOST}:80/p/1234.png", FetchTargetRefusal.NON_STANDARD_PORT),
    ("ftp", f"ftp://{IMAGE_HOST}/p/1234.png", FetchTargetRefusal.UNSUPPORTED_SCHEME),
    ("javascript", "javascript:void(0)", FetchTargetRefusal.UNSUPPORTED_SCHEME),
    ("data", "data:image/png;base64,AAAA", FetchTargetRefusal.UNSUPPORTED_SCHEME),
    ("foreign host", "https://cdn.other.test/p/1234.png", FetchTargetRefusal.HOST_NOT_ALLOWLISTED),
    (
        "storefront host",
        "https://supplier.test/p/1234.png",
        FetchTargetRefusal.HOST_NOT_ALLOWLISTED,
    ),
    ("unclosed bracket", "https://[::1/p/1234.png", FetchTargetRefusal.UNPARSEABLE),
    ("port out of range", f"https://{IMAGE_HOST}:99999/p/1234.png", FetchTargetRefusal.UNPARSEABLE),
    ("port not a number", f"https://{IMAGE_HOST}:abc/p/1234.png", FetchTargetRefusal.UNPARSEABLE),
    ("no host", "https:///p/1234.png", FetchTargetRefusal.UNPARSEABLE),
]


@pytest.mark.parametrize(
    ("url", "reason"),
    [(url, reason) for _, url, reason in REFUSED_IMAGE_TARGETS],
    ids=[name for name, _, _ in REFUSED_IMAGE_TARGETS],
)
def test_an_image_target_is_refused_for_one_named_reason_before_anything_is_sent(
    url: str, reason: FetchTargetRefusal
) -> None:
    profile = _profile()
    with pytest.raises(CollectionTargetRefused) as judged:
        image_fetch_target(profile, url)
    assert judged.value.reason is reason
    assert url not in judged.value.message, "a refusal never echoes the URL"

    # The gateway asks the same judge, and a refused URL reserves nothing and sends nothing.
    site, budget = Site(), Budget()
    gateway = PolicedCollectionGateway(http_transport=httpx.MockTransport(site))
    with pytest.raises(CollectionTargetRefused) as refused:
        gateway.read_image(profile, url, budget=budget)
    assert refused.value.reason is reason
    assert (budget.reserved, site.requests) == ([], [])
    with pytest.raises(CollectionTargetRefused) as checked:
        check_target(profile, url, ReadKind.IMAGE_REQUEST)
    assert checked.value.reason is reason


@pytest.mark.parametrize(
    ("url", "locator"),
    [
        (IMAGE, IMAGE),
        ("https://IMG.Supplier.Test/p/1234.png", IMAGE),  # the host as parsed
        (f"HTTPS://{IMAGE_HOST}/p/1234.png", IMAGE),  # the scheme as parsed
        (f"https://{IMAGE_HOST}:443/p/1234.png", IMAGE),  # the https port, named
        (f"https://{IMAGE_HOST}/P/1234.PNG", f"https://{IMAGE_HOST}/P/1234.PNG"),  # path as written
        (f"{IMAGE}?X-Signature=SECRET", None),  # a query may be a token: never persisted
    ],
)
def test_an_allowed_image_target_reads_back_as_its_canonical_form(
    url: str, locator: str | None
) -> None:
    target = image_fetch_target(_profile(), url)
    assert (target.host, target.locator) == (IMAGE_HOST, locator)
    assert check_target(_profile(), url, ReadKind.IMAGE_REQUEST) == IMAGE_HOST


def test_the_request_goes_to_the_url_as_written_never_a_rewrite() -> None:
    site, budget = Site(), Budget()
    gateway = PolicedCollectionGateway(http_transport=httpx.MockTransport(site))
    gateway.read_image(_profile(), f"{IMAGE}?v=2", budget=budget)
    assert [str(request.url) for request in site.requests] == [f"{IMAGE}?v=2"]
    assert budget.reserved == [(ReadKind.IMAGE_REQUEST, IMAGE_HOST)]


def test_a_url_with_several_defects_is_refused_for_the_first_in_a_fixed_order() -> None:
    ladder = [
        (f"http://u:p@{IMAGE_HOST}:8443/p/12 34.png#top", FetchTargetRefusal.WHITESPACE),
        (f"http://u:p@{IMAGE_HOST}:8443/p/1234.png#top", FetchTargetRefusal.FRAGMENT),
        (f"http://u:p@{IMAGE_HOST}:99999/p/1234.png", FetchTargetRefusal.UNPARSEABLE),
        (f"//u:p@{IMAGE_HOST}:8443/p/1234.png", FetchTargetRefusal.NOT_ABSOLUTE),
        (f"http://u:p@{IMAGE_HOST}:8443/p/1234.png", FetchTargetRefusal.NON_HTTPS),
        (f"ftp://u:p@{IMAGE_HOST}:8443/p/1234.png", FetchTargetRefusal.UNSUPPORTED_SCHEME),
        ("https://u:p@:8443/p/1234.png", FetchTargetRefusal.UNPARSEABLE),
        (f"https://u:p@{IMAGE_HOST}:8443/p/1234.png", FetchTargetRefusal.CREDENTIALS_PRESENT),
        (f"https://{IMAGE_HOST}:8443/p/1234.png", FetchTargetRefusal.NON_STANDARD_PORT),
        ("https://cdn.other.test:8443/p/1234.png", FetchTargetRefusal.NON_STANDARD_PORT),
        ("https://cdn.other.test/p/1234.png", FetchTargetRefusal.HOST_NOT_ALLOWLISTED),
    ]
    for url, reason in ladder:
        reasons = set()
        for _ in range(3):
            with pytest.raises(CollectionTargetRefused) as refused:
                image_fetch_target(_profile(), url)
            reasons.add(refused.value.reason)
        assert reasons == {reason}, url


# ---------------------------------------------------------------- documents and policies


@pytest.mark.parametrize(
    ("kind", "url", "reason"),
    [
        (
            ReadKind.PRODUCT_READ,
            "https://other.test/products/1234",
            FetchTargetRefusal.HOST_NOT_ALLOWLISTED,
        ),
        (
            ReadKind.PRODUCT_READ,
            "https://supplier.test/member/index.html",
            FetchTargetRefusal.PATH_NOT_ALLOWED,
        ),
        (
            ReadKind.PRODUCT_READ,
            "https://supplier.test/products/1234?token=abc",
            FetchTargetRefusal.QUERY_NOT_ALLOWED,
        ),
        (ReadKind.PRODUCT_READ, "http://supplier.test/products/1234", FetchTargetRefusal.NON_HTTPS),
        (
            ReadKind.PRODUCT_READ,
            "https://supplier.test/products/1234#top",
            FetchTargetRefusal.FRAGMENT,
        ),
        (
            ReadKind.POLICY_READ,
            "https://supplier.test/terms.html",
            FetchTargetRefusal.PATH_NOT_ALLOWED,
        ),
        (
            ReadKind.POLICY_READ,
            "https://supplier.test/robots.txt?x=1",
            FetchTargetRefusal.QUERY_NOT_ALLOWED,
        ),
        (
            ReadKind.POLICY_READ,
            f"https://{IMAGE_HOST}/terms.html",
            FetchTargetRefusal.PATH_NOT_ALLOWED,
        ),
        (
            ReadKind.POLICY_READ,
            f"https://{IMAGE_HOST}/robots.txt?x=1",
            FetchTargetRefusal.QUERY_NOT_ALLOWED,
        ),
        (
            ReadKind.POLICY_READ,
            "https://other.test/robots.txt",
            FetchTargetRefusal.HOST_NOT_ALLOWLISTED,
        ),
    ],
)
def test_a_document_target_is_refused_for_one_named_reason(
    kind: ReadKind, url: str, reason: FetchTargetRefusal
) -> None:
    with pytest.raises(CollectionTargetRefused) as refused:
        check_target(_profile(), url, kind)
    assert refused.value.reason is reason


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (f"https://{IMAGE_HOST}/terms.html", FetchTargetRefusal.HOST_NOT_ALLOWLISTED),
        ("https://supplier.test/terms.html?x=1", FetchTargetRefusal.QUERY_NOT_ALLOWED),
        ("https://supplier.test/robots.txt", FetchTargetRefusal.PATH_NOT_ALLOWED),
        ("https://supplier.test/products/1234", FetchTargetRefusal.PATH_NOT_ALLOWED),
        ("http://supplier.test/terms.html", FetchTargetRefusal.NON_HTTPS),
    ],
)
def test_a_discovered_policy_target_is_refused_for_one_named_reason(
    url: str, reason: FetchTargetRefusal
) -> None:
    with pytest.raises(CollectionTargetRefused) as refused:
        check_discovered_policy(_profile(), url)
    assert refused.value.reason is reason


def test_the_refusal_vocabulary_is_closed_and_has_no_catch_all() -> None:
    assert {member.value for member in FetchTargetRefusal} == {
        "UNPARSEABLE",
        "WHITESPACE",
        "FRAGMENT",
        "NOT_ABSOLUTE",
        "NON_HTTPS",
        "UNSUPPORTED_SCHEME",
        "CREDENTIALS_PRESENT",
        "NON_STANDARD_PORT",
        "HOST_NOT_ALLOWLISTED",
        "PATH_NOT_ALLOWED",
        "QUERY_NOT_ALLOWED",
    }
    assert not {"OTHER", "UNKNOWN", "ERROR"} & {m.value for m in FetchTargetRefusal}
    with pytest.raises(TypeError):
        CollectionTargetRefused("a message only")  # type: ignore[call-arg]


# ---------------------------------------------------------------- how the source wrote it


@pytest.mark.parametrize(
    ("written", "form", "trimmed"),
    [
        (IMAGE, LocatorForm.ABSOLUTE, False),
        (f"HTTP://{IMAGE_HOST}/p/1234.png", LocatorForm.ABSOLUTE, False),
        (f"ftp://{IMAGE_HOST}/p/1234.png", LocatorForm.ABSOLUTE, False),
        ("javascript:void(0)", LocatorForm.ABSOLUTE, False),
        (f"//{IMAGE_HOST}/p/1234.png", LocatorForm.PROTOCOL_RELATIVE, False),
        ("/p/1234.png", LocatorForm.RELATIVE, False),
        ("p/1234.png", LocatorForm.RELATIVE, False),
        ("../p/1234.png", LocatorForm.RELATIVE, False),
        ("?v=2", LocatorForm.RELATIVE, False),
        (f"  {IMAGE}\n", LocatorForm.ABSOLUTE, True),
        (f"\t//{IMAGE_HOST}/p/1234.png ", LocatorForm.PROTOCOL_RELATIVE, True),
        (" /p/12 34.png", LocatorForm.RELATIVE, True),
        ("/p/12 34.png", LocatorForm.RELATIVE, False),  # internal whitespace is not trimmed
    ],
)
def test_the_written_form_is_read_from_the_source_text_alone(
    written: str, form: LocatorForm, trimmed: bool
) -> None:
    candidate = ImageCandidate(
        url="https://unrelated.test/x.png",
        role=SourceRole.DETAIL,
        order=0,
        rule="r",
        source=written,
    )
    assert candidate.source_form == (form, trimmed)


def test_a_supplier_that_reports_no_source_text_has_no_written_form() -> None:
    candidate = ImageCandidate(url=IMAGE, role=SourceRole.DETAIL, order=0, rule="r")
    assert candidate.source_form is None


def test_an_unparseable_reference_has_no_host_rather_than_raising() -> None:
    candidate = ImageCandidate(url="https://[::1/p.png", role=SourceRole.DETAIL, order=0, rule="r")
    assert candidate.host == ""


# ---------------------------------------------------------------- what a revision may hold

REFUSED = ImageReference(
    role=ImageRole.DETAIL,
    ordinal=3,
    host=IMAGE_HOST,
    provenance="km.detail.prd_detail",
    status=FieldStatus.REVIEW_REQUIRED,
    issue=ImageIssue.FETCH_FAILED,
    source_form=LocatorForm.ABSOLUTE,
    source_trimmed=False,
    target_refusal=FetchTargetRefusal.NON_HTTPS,
)


def test_a_refused_target_reaches_the_revision_with_its_form_and_reason() -> None:
    facts = evaluate(collected(images=(REPRESENTATIVE, REFUSED)))
    stored = next(ref for ref in facts.images if ref.ordinal == 3)
    assert (stored.source_form, stored.source_trimmed, stored.target_refusal) == (
        LocatorForm.ABSOLUTE,
        False,
        FetchTargetRefusal.NON_HTTPS,
    )


@pytest.mark.parametrize(
    "broken",
    [
        replace(REFUSED, locator=f"https://{IMAGE_HOST}/p/1234.png"),  # a refused target has none
        replace(REFUSED, status=FieldStatus.CONFIRMED, issue=None, sha256="a" * 64),
        replace(REFUSED, target_refusal="NON_HTTPS"),  # a string is not the vocabulary
        replace(REFUSED, source_form="ABSOLUTE"),
        replace(REFUSED, source_form=None),  # trimmed is reported with the form
        replace(REFUSED, source_trimmed="no"),
    ],
)
def test_the_domain_refuses_incoherent_diagnostics(broken: ImageReference) -> None:
    with pytest.raises(InputValidationError):
        evaluate(collected(images=(REPRESENTATIVE, broken)))
