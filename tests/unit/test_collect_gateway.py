"""The common COLLECT gateway (ADR-0010 §3, §4, §9): no network, a mock transport only."""

import logging
from dataclasses import replace

import httpx
import pytest

from app.core.errors import PolicyBlockedError, RateLimitedError, TransientError
from integrations.suppliers.collection import (
    CollectionLimits,
    CollectionProfile,
    FetchIssue,
    ReadKind,
)
from integrations.suppliers.transport import collection as transport
from integrations.suppliers.transport.collection import (
    CollectionBudgetRefused,
    CollectionTargetRefused,
    ImageFetchRefused,
    LiveTransportRefused,
    PolicedCollectionGateway,
)
from integrations.suppliers.transport.session_payload import encode_session
from tests.suppliers import fake_definition

COOKIE_VALUE = "collect-cookie-that-must-never-leave-the-storefront"
SIGNATURE = "SIGNATURE-THAT-MUST-NEVER-BE-LOGGED"
PRODUCT = "https://supplier.test/products/1234"
IMAGE = f"https://img.supplier.test/p/1234.png?X-Signature={SIGNATURE}"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic"


def _profile(**changes: object) -> CollectionProfile:
    profile = CollectionProfile(
        supplier=fake_definition().profile,
        product_path=r"/products/[0-9]+",
        policy_paths=frozenset({"/robots.txt"}),
        image_hosts=frozenset({"img.supplier.test"}),
        safe_query_keys={"supplier.test": frozenset({"variant"})},
        limits=CollectionLimits(
            max_image_refs=20,
            max_image_bytes=512,
            max_image_requests_per_run=10,
            max_new_image_bytes_per_run=4096,
            same_product_interval_s=60.0,
        ),
    )
    return replace(profile, **changes) if changes else profile


class Budget:
    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.reserved: list[tuple[ReadKind, str]] = []

    def reserve(self, kind: ReadKind, subject: str) -> None:
        if self.refuse:
            raise CollectionBudgetRefused("COLLECT_BUDGET_REFUSED", "cap reached")
        self.reserved.append((kind, subject))


class Site:
    """Answers with a fixed response and records every request it receives."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.response = response or httpx.Response(200, text="<html>product</html>")
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


def _gateway(site: Site) -> PolicedCollectionGateway:
    return PolicedCollectionGateway(http_transport=httpx.MockTransport(site))


def _session() -> bytes:
    return encode_session(
        [{"name": "SID", "value": COOKIE_VALUE, "domain": "supplier.test", "path": "/"}],
        user_agent="UA/1",
        hosts={"supplier.test"},
    )


# ---------------------------------------------------------------- targets and budget


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        (ReadKind.PRODUCT_READ, "http://supplier.test/products/1234"),
        (ReadKind.PRODUCT_READ, "https://other.test/products/1234"),
        (ReadKind.PRODUCT_READ, "https://user:pw@supplier.test/products/1234"),
        (ReadKind.PRODUCT_READ, "https://supplier.test:8443/products/1234"),
        (ReadKind.PRODUCT_READ, "https://supplier.test/products/1234#top"),
        (ReadKind.PRODUCT_READ, "https://supplier.test/member/index.html"),
        (ReadKind.PRODUCT_READ, "https://supplier.test/products/1234?token=abc"),
        (ReadKind.PRODUCT_READ, "https://img.supplier.test/products/1234"),
        (ReadKind.POLICY_READ, "https://supplier.test/terms.html"),
        (ReadKind.POLICY_READ, "https://supplier.test/robots.txt?x=1"),
        (ReadKind.IMAGE_REQUEST, "https://cdn.other.test/p.png"),
        (ReadKind.IMAGE_REQUEST, "https://supplier.test/p.png"),
    ],
)
def test_targets_outside_the_profile_are_refused_before_budget_and_send(
    kind: ReadKind, url: str
) -> None:
    site, budget = Site(), Budget()
    gateway = _gateway(site)
    with pytest.raises(CollectionTargetRefused):
        if kind is ReadKind.IMAGE_REQUEST:
            gateway.read_image(_profile(), url, budget=budget)
        else:
            gateway.read_document(_profile(), url, kind=kind, budget=budget)
    assert (budget.reserved, site.requests) == ([], [])


def test_a_budget_refusal_sends_nothing() -> None:
    site = Site()
    with pytest.raises(CollectionBudgetRefused):
        _gateway(site).read_document(
            _profile(), PRODUCT, kind=ReadKind.PRODUCT_READ, budget=Budget(refuse=True)
        )
    assert site.requests == []


def test_budget_subjects_are_the_canonical_url_the_policy_path_or_the_image_host() -> None:
    budget = Budget()
    gateway = _gateway(
        Site(httpx.Response(200, headers={"content-type": "image/png"}, content=PNG))
    )
    gateway.read_document(
        _profile(), f"{PRODUCT}?variant=2", kind=ReadKind.PRODUCT_READ, budget=budget
    )
    gateway.read_document(
        _profile(), "https://supplier.test/robots.txt", kind=ReadKind.POLICY_READ, budget=budget
    )
    gateway.read_image(_profile(), IMAGE, budget=budget)
    assert budget.reserved == [
        (ReadKind.PRODUCT_READ, f"{PRODUCT}?variant=2"),
        (ReadKind.POLICY_READ, "/robots.txt"),
        (ReadKind.IMAGE_REQUEST, "img.supplier.test"),  # never the signed URL
    ]


# ---------------------------------------------------------------- documents


def test_the_session_goes_only_with_a_product_read() -> None:
    site = Site(httpx.Response(200, headers={"content-type": "image/png"}, content=PNG))
    gateway = _gateway(site)
    session = _session()
    gateway.read_document(
        _profile(), PRODUCT, kind=ReadKind.PRODUCT_READ, budget=Budget(), session=session
    )
    gateway.read_document(
        _profile(),
        "https://supplier.test/robots.txt",
        kind=ReadKind.POLICY_READ,
        budget=Budget(),
        session=session,
    )
    gateway.read_image(_profile(), IMAGE, budget=Budget())
    cookies = [request.headers.get("cookie") for request in site.requests]
    assert COOKIE_VALUE in (cookies[0] or "")
    assert cookies[1:] == [None, None]


def test_a_redirect_is_returned_as_evidence_never_followed() -> None:
    site = Site(httpx.Response(302, headers={"location": "https://supplier.test/member/login"}))
    view = _gateway(site).read_document(
        _profile(), PRODUCT, kind=ReadKind.PRODUCT_READ, budget=Budget()
    )
    assert (view.status, view.location, len(site.requests)) == (302, "/member/login", 1)


@pytest.mark.parametrize(
    ("status", "error"), [(429, RateLimitedError), (503, TransientError), (500, TransientError)]
)
def test_supplier_trouble_is_classified(status: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        _gateway(Site(httpx.Response(status))).read_document(
            _profile(), PRODUCT, kind=ReadKind.PRODUCT_READ, budget=Budget()
        )


def test_a_document_over_the_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "MAX_DOCUMENT_BYTES", 16)
    with pytest.raises(PolicyBlockedError) as caught:
        _gateway(Site(httpx.Response(200, text="x" * 17))).read_document(
            _profile(), PRODUCT, kind=ReadKind.PRODUCT_READ, budget=Budget()
        )
    assert caught.value.code == "COLLECT_DOCUMENT_TOO_LARGE"


def test_the_view_holds_the_body_but_never_shows_it() -> None:
    view = _gateway(Site(httpx.Response(200, text="<html>secret body</html>"))).read_document(
        _profile(), PRODUCT, kind=ReadKind.PRODUCT_READ, budget=Budget()
    )
    assert view.body == "<html>secret body</html>"
    assert "secret body" not in repr(view)


# ---------------------------------------------------------------- images


def test_an_image_is_read_with_its_validators() -> None:
    site = Site(
        httpx.Response(
            200,
            headers={"content-type": "image/png", "etag": '"v1"', "last-modified": "Wed, 16 Sep"},
            content=PNG,
        )
    )
    image = _gateway(site).read_image(_profile(), IMAGE, budget=Budget())
    assert (image.status, image.content_type, image.content) == (200, "image/png", PNG)
    assert (image.etag, image.last_modified) == ('"v1"', "Wed, 16 Sep")


def test_a_validated_image_is_reused_only_on_304() -> None:
    site = Site(httpx.Response(304, headers={"etag": '"v1"'}))
    image = _gateway(site).read_image(_profile(), IMAGE, budget=Budget(), etag='"v1"')
    assert image.not_modified and image.content == b""
    assert site.requests[0].headers["if-none-match"] == '"v1"'


@pytest.mark.parametrize(
    ("response", "issue"),
    [
        (httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>"), "BAD"),
        (httpx.Response(200, headers={"content-type": "image/png"}, content=b"x" * 513), "OVER"),
        (
            httpx.Response(200, headers={"content-type": "image/png"}, content=iter([b"x" * 600])),
            "OVER",
        ),
        (httpx.Response(404), "FAIL"),
    ],
    ids=["bad-content-type", "declared-oversize", "streamed-oversize", "not-found"],
)
def test_unusable_image_responses_are_refused(response: httpx.Response, issue: str) -> None:
    with pytest.raises(ImageFetchRefused) as caught:
        _gateway(Site(response)).read_image(_profile(), IMAGE, budget=Budget())
    expected = {
        "BAD": FetchIssue.BAD_CONTENT_TYPE,
        "OVER": FetchIssue.OVERSIZE,
        "FAIL": FetchIssue.FETCH_FAILED,
    }
    assert caught.value.issue is expected[issue]


# ---------------------------------------------------------------- attribution and safety


def test_httpcore_debug_lines_are_silenced(caplog: pytest.LogCaptureFixture) -> None:
    # httpcore's debug lines can carry request/response headers, cookies included.
    _gateway(Site())
    caplog.set_level(logging.DEBUG)
    logging.getLogger("httpcore.http11").debug("receive_response_headers set-cookie=%s", "x")
    assert not [r for r in caplog.records if r.name.startswith("httpcore")]


def test_the_log_carries_the_path_or_host_never_a_query(caplog: pytest.LogCaptureFixture) -> None:
    # Every logger at DEBUG, as under ICBM_LOG_LEVEL=DEBUG: httpx writes its own request line.
    caplog.set_level(logging.DEBUG)
    gateway = _gateway(
        Site(httpx.Response(200, headers={"content-type": "image/png"}, content=PNG))
    )
    gateway.read_document(
        _profile(), f"{PRODUCT}?variant=2", kind=ReadKind.PRODUCT_READ, budget=Budget()
    )
    gateway.read_image(_profile(), IMAGE, budget=Budget())
    targets = [r.target for r in caplog.records if r.getMessage() == "supplier.collect_request"]
    assert targets == ["/products/1234", "img.supplier.test"]
    assert SIGNATURE not in caplog.text and COOKIE_VALUE not in caplog.text


def test_the_live_transport_never_exists_under_pytest() -> None:
    with pytest.raises(LiveTransportRefused):
        PolicedCollectionGateway()


def test_profiles_refuse_wildcards_defaults_and_browser_collection() -> None:
    with pytest.raises(ValueError):
        _profile(image_hosts=frozenset({"*.supplier.test"}))
    with pytest.raises(ValueError):
        replace(_profile().limits, same_product_interval_s=30.0)
    with pytest.raises(TypeError):
        CollectionLimits()  # type: ignore[call-arg]  # no limit has a default
    from integrations.suppliers.base import SupplierTransport

    with pytest.raises(ValueError):
        _profile(transport=SupplierTransport.BROWSER)
