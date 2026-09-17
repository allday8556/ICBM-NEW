"""Can the PR #74 diagnostics tell a source-reference defect from a resolution defect?

Issue #52 ruling 5719697528, PR-2: **diagnostic contract reproduction, not a reproduction of the
historical `m3-accept-02` DETAIL:16 reference.** No historical raw reference is used, read or
guessed here; every reference below is synthetic fixture text written for this test.

One synthetic KM-shaped page carries one image reference per case. Each case runs through the
production path and is reported on separately:

    fixture source text
      → production KM parser (``classify_images``): the ``source`` it reports and the ``url`` it
        resolves
      → the production target judge (``image_fetch_target``): a canonical target or a structured
        refusal
      → generic COLLECT core: whether a request was reserved and whether anything was sent
      → the revision store and the API: ``source_form``, ``source_trimmed``, ``target_refusal``,
        ``locator``, status and issue

**A. A source-reference defect** — the parser faithfully reports and resolves what the page wrote,
and the target judge refuses it with the matching reason — is what the matrix below asserts.

**B. A parser or resolution defect** — a fetchable reference the parser reports or resolves as
something else, or diagnostics that go missing on the way to the read-back — must make that same
matrix fail. The negative controls at the end inject exactly that, in the test process only, and
assert the verification fails. No such defect is introduced into production code.

Provider traffic is zero: the production transport runs over ``httpx.MockTransport``.
"""

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlsplit

import httpx
import pytest

from app.api.routes.collect import revision as revision_route
from app.collect import collection as collect_core
from app.collect import sourceassets
from app.collect.collection import RegisteredCollection
from app.collect.facts import FetchTargetRefusal, FieldStatus, ImageIssue, LocatorForm
from app.collect.models import CollectionOutcome
from app.config import AppConfig
from app.container import Container, build_container
from app.core.ownership import acquire_data_dir
from app.jobs.models import JobState
from integrations.suppliers.base import SupplierTransport
from integrations.suppliers.collection import (
    CollectionLimits,
    CollectionProfile,
    ImageCandidate,
    ImageResponse,
    ImageRoleRules,
    ReadKind,
)
from integrations.suppliers.extraction import supplier_manifest
from integrations.suppliers.kmretail import IMAGE_ROLES
from integrations.suppliers.kmretail.collect.images import classify_images
from integrations.suppliers.transport.collection import (
    CollectionTargetRefused,
    ImageFetchTarget,
    PolicedCollectionGateway,
    RequestBudget,
    image_fetch_target,
)
from integrations.suppliers.transport.session_payload import encode_session
from scripts.m3collect import fake_shop
from tests.support import FakeClock

pytestmark = pytest.mark.integration

STORE = "shop.invalid"
ASSETS = "assets.invalid"  # an allowlisted image host, as KM's own profile allows two
FOREIGN = "cdn.other.invalid"  # never allowlisted
PRODUCT_URL = f"https://{STORE}/product/item/1/category/2/display/1/"
PNG = fake_shop.png(20, 20)
KM_PARSER = supplier_manifest("kmretail")


@dataclass(frozen=True)
class Case:
    """One synthetic reference, and what the contract must say about it end to end."""

    name: str
    written: str  # the fixture's own text; never a historical reference
    emitted: bool  # whether the production parser emits a candidate for it at all
    form: LocatorForm | None
    trimmed: bool | None
    refusal: FetchTargetRefusal | None
    fetched: bool


CASES = (
    Case(
        "absolute https",
        f"https://{ASSETS}/d/ok.png",
        True,
        LocatorForm.ABSOLUTE,
        False,
        None,
        True,
    ),
    Case("relative", "/d/relative.png", True, LocatorForm.RELATIVE, False, None, True),
    Case(
        "protocol-relative",
        f"//{ASSETS}/d/protocol.png",
        True,
        LocatorForm.PROTOCOL_RELATIVE,
        False,
        None,
        True,
    ),
    Case(
        "http",
        f"http://{ASSETS}/d/http.png",
        True,
        LocatorForm.ABSOLUTE,
        False,
        FetchTargetRefusal.NON_HTTPS,
        False,
    ),
    Case(
        "surrounding whitespace",
        f"  https://{ASSETS}/d/trimmed.png\n",
        True,
        LocatorForm.ABSOLUTE,
        True,
        None,
        True,
    ),
    Case(
        "internal whitespace",
        f"https://{ASSETS}/d/in side.png",
        True,
        LocatorForm.ABSOLUTE,
        False,
        FetchTargetRefusal.WHITESPACE,
        False,
    ),
    Case(
        "fragment",
        f"https://{ASSETS}/d/frag.png#part",
        True,
        LocatorForm.ABSOLUTE,
        False,
        FetchTargetRefusal.FRAGMENT,
        False,
    ),
    Case(
        "credentials",
        f"https://user:secret@{ASSETS}/d/cred.png",
        True,
        LocatorForm.ABSOLUTE,
        False,
        FetchTargetRefusal.CREDENTIALS_PRESENT,
        False,
    ),
    Case(
        "non-443 port",
        f"https://{ASSETS}:8443/d/port.png",
        True,
        LocatorForm.ABSOLUTE,
        False,
        FetchTargetRefusal.NON_STANDARD_PORT,
        False,
    ),
    # The KM parser emits no candidate for a scheme that is not http(s); the boundary is proven
    # below rather than claimed as something generic core observed.
    Case("unsupported scheme", f"ftp://{ASSETS}/d/ftp.png", False, None, None, None, False),
    Case(
        "non-allowlisted host",
        f"https://{FOREIGN}/d/foreign.png",
        True,
        LocatorForm.ABSOLUTE,
        False,
        FetchTargetRefusal.HOST_NOT_ALLOWLISTED,
        False,
    ),
    Case(
        "unparseable port",
        f"https://{ASSETS}:99999/d/bad.png",
        True,
        LocatorForm.ABSOLUTE,
        False,
        FetchTargetRefusal.UNPARSEABLE,
        False,
    ),
)
EMITTED = tuple(case for case in CASES if case.emitted)
# A reference whose authority does not parse has no host, and the schema requires one: the
# collection fails loudly rather than dropping it. Kept out of the matrix page on purpose.
NO_HOST = "https://[::1/d/unparseable.png"


def page(references: tuple[str, ...], *, representative: bool = True) -> str:
    """A KM-shaped product page: one description block holding the fixture's references."""
    declared = '<meta property="product:id" content="4242">'
    og = (
        f'<meta property="og:image" content="https://{ASSETS}/d/primary.png">'
        if representative
        else ""
    )
    images = "".join(f'<img ec-data-src="{written}">' for written in references)
    return (
        f"<html><head>{declared}{og}</head><body>"
        '<div class="xans-element- xans-product xans-product-additional">'
        f'<div id="prdDetail"><div class="cont">{images}</div></div>'
        "</div></body></html>"
    )


MATRIX_PAGE = page(tuple(case.written for case in CASES))


def profile() -> CollectionProfile:
    """A profile shaped like KM's: the storefront serves images too, and one other host does."""
    return CollectionProfile(
        supplier=replace(
            fake_shop.PROFILE,
            base_url=f"https://{STORE}",
            egress_hosts=frozenset({STORE, ASSETS}),
        ),
        product_path=r"/product/[^/]+/\d+(?:/category/\d+)?(?:/display/\d+)?/?",
        policy_paths=frozenset({"/robots.txt"}),
        image_hosts=frozenset({STORE, ASSETS}),
        safe_query_keys={},
        limits=CollectionLimits(
            max_image_refs=30,
            max_image_bytes=2 * 1024 * 1024,
            max_image_requests_per_run=20,
            max_new_image_bytes_per_run=24 * 1024 * 1024,
            same_product_interval_s=60.0,
        ),
        transport=SupplierTransport.HTTP,
    )


def registered(
    classify: Callable[[str, str], tuple[ImageCandidate, ...]] = classify_images,
) -> RegisteredCollection:
    """The production KM image-role parser, with fixture identity and field readings around it."""
    return RegisteredCollection(
        collection=replace(
            fake_shop.collection(),
            profile=profile(),
            roles=ImageRoleRules(identity=IMAGE_ROLES.identity, classify=classify),
        ),
        extractor_revision=KM_PARSER.revision,
        extractor_fingerprint=KM_PARSER.fingerprint,
    )


class Sessions:
    """CONNECT's part: one real session payload, because the production transport decodes it."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def collection_session(self, supplier_key: str, *, operator_initiated: bool = False) -> bytes:
        self.asked.append(supplier_key)
        return encode_session(
            [{"name": "SID", "value": "fixture-session", "domain": STORE, "path": "/"}],
            user_agent="UA/1",
            hosts={STORE},
        )


class Network:
    """The mock network behind the production transport. Every send is recorded here."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)


class Reserving:
    """The run's own budget, with every reservation recorded before it is forwarded."""

    def __init__(self, inner: RequestBudget, log: list[tuple[str, str]]) -> None:
        self._inner = inner
        self._log = log

    def reserve(self, kind: ReadKind, subject: str) -> None:
        self._inner.reserve(kind, subject)
        self._log.append((kind.value, subject))


class Observed:
    """The production policed gateway, watched: what it reserved and what it sent."""

    def __init__(self, network: Network, document: str) -> None:
        self.network = network
        self.document = document
        self.reserved: list[tuple[str, str]] = []
        self._inner = PolicedCollectionGateway(http_transport=httpx.MockTransport(self._answer))

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if urlsplit(str(request.url)).path.startswith("/product/"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text=self.document)
        return self.network(request)

    def read_document(self, profile, url, *, kind, budget, session=None):  # type: ignore[no-untyped-def]
        return self._inner.read_document(
            profile, url, kind=kind, budget=Reserving(budget, self.reserved), session=session
        )

    def read_image(self, profile, url, *, budget, etag=None, last_modified=None, max_bytes=None):  # type: ignore[no-untyped-def]
        return self._inner.read_image(
            profile,
            url,
            budget=Reserving(budget, self.reserved),
            etag=etag,
            last_modified=last_modified,
            max_bytes=max_bytes,
        )


@pytest.fixture
def network() -> Network:
    return Network()


@pytest.fixture
def gateway(network: Network) -> Observed:
    return Observed(network, MATRIX_PAGE)


def container_for(
    config: AppConfig, clock: FakeClock, gateway: Observed, collection: RegisteredCollection
) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=gateway,
            collection_sessions=Sessions(),
            collections=(collection,),
        )
        try:
            yield built
        finally:
            built.db.dispose()


@pytest.fixture
def collecting(config: AppConfig, clock: FakeClock, gateway: Observed) -> Iterator[Container]:
    yield from container_for(config, clock, gateway, registered())


def collect_once(container: Container) -> str:
    submitted = container.collection.submit(fake_shop.SUPPLIER_KEY, PRODUCT_URL)
    assert container.runner.run_next() is not None
    run = container.collection.run(submitted.collection_run_id)
    job = container.jobs.get(submitted.job_id)
    why = f"{run.detail} / {job.state} {job.last_error_code}: {job.last_error_message}"
    assert run.outcome is CollectionOutcome.RECORDED, why
    assert run.revision_id is not None
    return run.revision_id


# ---------------------------------------------------------------- the reproduction matrix


def judge(candidate: ImageCandidate) -> tuple[str | None, FetchTargetRefusal | None]:
    """The single production judge's answer for one resolved reference."""
    try:
        return image_fetch_target(profile(), candidate.url).locator, None
    except CollectionTargetRefused as refused:
        return None, refused.reason


def verify(
    container: Container,
    gateway: Observed,
    revision_id: str,
    classify: Callable[[str, str], tuple[ImageCandidate, ...]] = classify_images,
) -> list[dict[str, object]]:
    """Assert the whole contract for every case, and return the matrix that was asserted.

    ``classify`` is the parser the run itself used, so a parser that reports or resolves something
    else is judged on what it actually produced. Anything a parser or resolution defect changes —
    the reported source, the resolved URL, the refusal reason, whether a refused target was sent,
    or a diagnostic that goes missing before read-back — makes one of these assertions fail.
    """
    parsed = classify(MATRIX_PAGE, PRODUCT_URL)
    candidates = {c.source: c for c in parsed if c.source}
    stored = container.source_truth.revision(revision_id)
    by_ordinal = {ref.ordinal: ref for ref in stored.images}
    api = {ref.ordinal: ref for ref in revision_route(revision_id, container).images}
    # The reference order is the page's own: the og:image first, then the description block.
    emitted_order = [c.source for c in parsed]
    sends = [url for url in gateway.network.sent if url != PRODUCT_URL]
    reservations = [subject for kind, subject in gateway.reserved if kind == "IMAGE_REQUEST"]
    matrix: list[dict[str, object]] = []
    allowed = 1 + sum(1 for case in CASES if case.emitted and case.fetched)  # the og:image too
    assert len(reservations) == allowed, "only an allowed target is ever reserved"
    assert len(sends) == allowed, "only an allowed target is ever sent"

    for case in CASES:
        if not case.emitted:
            assert case.written not in candidates, f"{case.name}: the parser emitted it after all"
            # The generic contract would still refuse it, but core never saw it.
            with pytest.raises(CollectionTargetRefused) as refused:
                image_fetch_target(profile(), case.written)
            matrix.append(
                {
                    "case": case.name,
                    "fixture source": case.written,
                    "parser": "no candidate emitted",
                    "judge": f"would refuse {refused.value.reason.value} (not observed by core)",
                    "reserved": False,
                    "sent": False,
                    "readback": "no reference",
                }
            )
            continue

        assert case.written in candidates, f"{case.name}: the parser reported another source"
        candidate = candidates[case.written]
        resolved = urljoin(PRODUCT_URL, case.written.strip())
        assert candidate.url == resolved, f"{case.name}: the parser resolved something else"
        assert candidate.source_form == (case.form, case.trimmed), case.name

        locator, refusal = judge(candidate)
        assert refusal == case.refusal, f"{case.name}: the judge gave another reason"

        ordinal = emitted_order.index(case.written)
        ref = by_ordinal[ordinal]
        assert (ref.source_form, ref.source_trimmed) == (case.form, case.trimmed), case.name
        assert ref.target_refusal == case.refusal, (
            f"{case.name}: the refusal did not reach the store"
        )
        assert ref.locator == locator, f"{case.name}: the canonical target did not reach the store"
        assert api[ordinal].target_refusal == ref.target_refusal, case.name
        assert api[ordinal].source_form == ref.source_form, case.name

        sent = [url for url in sends if url == resolved]
        if case.fetched:
            assert ref.status is FieldStatus.CONFIRMED and ref.issue is None, case.name
            assert ref.asset is not None and ref.asset.sha256 == hashlib.sha256(PNG).hexdigest()
            assert sent, f"{case.name}: an allowed target was never sent"
        else:
            assert ref.status is FieldStatus.REVIEW_REQUIRED, case.name
            assert ref.issue is ImageIssue.FETCH_FAILED and ref.asset is None, case.name
            assert not sent, f"{case.name}: a refused target was sent"
        matrix.append(
            {
                "case": case.name,
                "fixture source": case.written,
                "parser": f"source kept, resolved {resolved}",
                "form": f"{case.form.value if case.form else None}/trimmed={case.trimmed}",
                "judge": refusal.value if refusal else f"canonical {locator}",
                "reserved": bool(sent),
                "sent": bool(sent),
                "readback": f"{ref.status.value}/{ref.issue.value if ref.issue else '-'}"
                f"/{ref.target_refusal.value if ref.target_refusal else '-'}/{ref.locator}",
            }
        )
    return matrix


def test_the_contract_tells_a_source_defect_from_a_resolution_defect(
    collecting: Container, gateway: Observed
) -> None:
    revision_id = collect_once(collecting)
    matrix = verify(collecting, gateway, revision_id)
    assert len(matrix) == len(CASES)
    for row in matrix:  # printed with -s, so the asserted matrix can be read off a run
        print(" | ".join(f"{key}={value}" for key, value in row.items()))

    stored = collecting.source_truth.revision(revision_id)
    # Every emitted reference reached the revision, and the representative image beside them.
    assert len(stored.images) == len(EMITTED) + 1
    refused = [ref for ref in stored.images if ref.target_refusal is not None]
    assert {ref.target_refusal for ref in refused} == {
        case.refusal for case in EMITTED if case.refusal is not None
    }
    # Nothing was requested for a refused target: the og:image and the four allowed ones only.
    assert len(gateway.network.sent) == 1 + sum(1 for case in EMITTED if case.fetched)
    assert stored.extractor_revision == KM_PARSER.revision


def test_a_reference_whose_authority_does_not_parse_fails_loudly_and_is_never_dropped(
    config: AppConfig, clock: FakeClock, network: Network
) -> None:
    # Measured, not assumed: resolving this authority raises inside the standard library, so the
    # parser itself cannot produce a candidate for it. The page is refused as a whole — loudly,
    # with nothing appended and nothing requested — rather than the reference being dropped.
    document = page((NO_HOST,), representative=False)
    with pytest.raises(ValueError, match="IPv6"):
        classify_images(document, PRODUCT_URL)

    gateway = Observed(network, document)
    for container in container_for(config, clock, gateway, registered()):
        submitted = container.collection.submit(fake_shop.SUPPLIER_KEY, PRODUCT_URL)
        container.runner.run_next()
        run = container.collection.run(submitted.collection_run_id)
        job = container.jobs.get(submitted.job_id)
        assert run.revision_id is None, "nothing was appended"
        assert run.outcome is not CollectionOutcome.RECORDED
        # The job carries the failure and dies; the run is left unsettled, which is visible.
        assert (job.state, job.last_error_code) == (JobState.DEAD, "UNHANDLED_EXCEPTION")
        assert "ValueError" in (job.last_error_message or "")
        assert network.sent == [], "no image request was made for it"


# ---------------------------------------------------------------- negative controls


def _defective(
    monkeypatch: pytest.MonkeyPatch,
    make: Callable[[tuple[ImageCandidate, ...]], tuple[ImageCandidate, ...]],
) -> Callable[[str, str], tuple[ImageCandidate, ...]]:
    def classify(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
        return make(classify_images(body, product_url))

    return classify


def run_with(
    config: AppConfig,
    clock: FakeClock,
    network: Network,
    classify: Callable[[str, str], tuple[ImageCandidate, ...]],
) -> None:
    gateway = Observed(network, MATRIX_PAGE)
    for container in container_for(config, clock, gateway, registered(classify)):
        revision_id = collect_once(container)
        verify(container, gateway, revision_id, classify)


def test_a_resolution_defect_fails_the_matrix(
    config: AppConfig, clock: FakeClock, network: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B: the page wrote a fetchable reference; the parser resolves a different target.
    def elsewhere(found: tuple[ImageCandidate, ...]) -> tuple[ImageCandidate, ...]:
        return tuple(
            replace(c, url=f"https://{ASSETS}/d/elsewhere.png")
            if c.source == CASES[0].written
            else c
            for c in found
        )

    with pytest.raises(AssertionError, match="resolved something else"):
        run_with(config, clock, network, _defective(monkeypatch, elsewhere))


def test_a_dropped_source_report_fails_the_matrix(
    config: AppConfig, clock: FakeClock, network: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forget(found: tuple[ImageCandidate, ...]) -> tuple[ImageCandidate, ...]:
        return tuple(replace(c, source=None) for c in found)

    with pytest.raises(AssertionError, match="reported another source"):
        run_with(config, clock, network, _defective(monkeypatch, forget))


def test_a_rewritten_source_form_fails_the_matrix(
    config: AppConfig, clock: FakeClock, network: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The parser reports an absolute form for what the page wrote as relative.
    def rewrite(found: tuple[ImageCandidate, ...]) -> tuple[ImageCandidate, ...]:
        relative = CASES[1].written
        return tuple(
            replace(c, source=urljoin(PRODUCT_URL, relative)) if c.source == relative else c
            for c in found
        )

    with pytest.raises(AssertionError, match="reported another source"):
        run_with(config, clock, network, _defective(monkeypatch, rewrite))


def test_a_wrong_refusal_reason_fails_the_matrix(
    collecting: Container, gateway: Observed, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision_id = collect_once(collecting)
    original = collect_core.image_fetch_target

    def mislabel(profile_, url):  # type: ignore[no-untyped-def]
        try:
            return original(profile_, url)
        except CollectionTargetRefused as refused:
            raise CollectionTargetRefused(
                FetchTargetRefusal.PATH_NOT_ALLOWED, refused.message
            ) from None

    monkeypatch.setattr(
        "tests.integration.test_collect_diagnostic_contract_reproduction.image_fetch_target",
        mislabel,
    )
    with pytest.raises(AssertionError, match="another reason"):
        verify(collecting, gateway, revision_id)


class Permissive(Observed):
    """Neither judge refuses: the core's check is bypassed and the transport is not the policed
    one, so a refused target would be reserved and sent."""

    def read_image(self, profile_, url, *, budget, etag=None, last_modified=None, max_bytes=None):  # type: ignore[no-untyped-def]
        budget.reserve(ReadKind.IMAGE_REQUEST, urlsplit(url).hostname or "")
        self.reserved.append(("IMAGE_REQUEST", urlsplit(url).hostname or ""))
        self.network.sent.append(url)
        return ImageResponse(200, "image/png", None, None, PNG)


def test_sending_a_refused_target_fails_the_matrix(
    config: AppConfig, clock: FakeClock, network: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both guards removed at once: core stops asking the judge, and the transport does not check.
    def permissive(profile_, url):  # type: ignore[no-untyped-def]
        try:
            return image_fetch_target(profile_, url)
        except CollectionTargetRefused:
            return ImageFetchTarget(host=urlsplit(url).hostname or "", locator=None)

    monkeypatch.setattr(collect_core, "image_fetch_target", permissive)
    gateway = Permissive(network, MATRIX_PAGE)
    for container in container_for(config, clock, gateway, registered()):
        revision_id = collect_once(container)
        with pytest.raises(AssertionError, match="only an allowed target is ever"):
            verify(container, gateway, revision_id)


def test_losing_the_diagnostics_before_readback_fails_the_matrix(
    config: AppConfig, clock: FakeClock, network: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sourceassets._review

    def stripped(image, issue):  # type: ignore[no-untyped-def]
        return replace(original(image, issue), target_refusal=None)

    monkeypatch.setattr(sourceassets, "_review", stripped)
    gateway = Observed(network, MATRIX_PAGE)
    for container in container_for(config, clock, gateway, registered()):
        revision_id = collect_once(container)
        with pytest.raises(AssertionError, match="refusal did not reach the store"):
            verify(container, gateway, revision_id)
