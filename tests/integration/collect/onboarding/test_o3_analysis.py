"""ADR-0036 O3: 「사이트 분석」 — trial reads, local captures, re-analysis and observed hosts.

Everything is offline. The real policed collection gateway reads scripted storefronts
(``tests.support.onboarding_trial_support``) through a mock transport that records every request,
with a fixed CONNECT session payload. Proven here:

- a trial reads one product page of a ``RECON`` site, once, through the protected collection path
  with the site's session, and fetches no image; it records one ``onboarding_trials`` row and
  never a ``ProductFactsRevision``, a collection run or a source asset (OB-02);
- the report has each field's status, a stable facts fingerprint, the unread rows with the slot a
  rule proposes, the observed hosts and the structural problems, on Cafe24 and Godomall pages
  (U-PICK's and 건강산's reconnaissance captures, and synthetic pages);
- the page is kept as a sanitized local capture: the member's name and e-mail, the session's
  cookie value, form values and script text never reach the file;
- a sample off the storefront host or outside the path form is refused before any read, as is a
  site that is not ``RECON``; the same sample is not read again within 60 s, nor the site within
  10 s, and at most 5 samples are taken;
- re-analysis after a words change reads nothing and records new trials under the new words;
- a host is confirmed only as a trial observed it, the host guard still applies, and a confirmed
  image host joins the collection egress;
- the ACTIVE gate counts O3 trials: 3 read samples are ready and wait only for verification.
"""

import json
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig, database_path
from app.container import Container
from app.main import create_app
from app.platform.core.errors import AppError
from app.stages.collect.collection import pacing_key
from app.stages.collect.onboarding.analysis import (
    IDENTITY_UNRESOLVED,
    PRICE_BLOCK_UNREADABLE,
    SCRIPT_RENDERED,
)
from app.stages.collect.onboarding.capture import TrialCaptureStore, sanitize
from app.stages.collect.onboarding.site import HostRole, OnboardingState
from app.stages.collect.onboarding.store import ONBOARDING_NOT_RECON
from app.stages.collect.onboarding.trials import (
    ONBOARDING_HOST_ROLE_UNOBSERVED,
    ONBOARDING_SAMPLE_ALREADY_READ,
    ONBOARDING_SAMPLE_REFUSED,
    ONBOARDING_SAMPLES_FULL,
    ONBOARDING_TRIAL_PENDING,
    ONBOARDING_TRIAL_TOO_SOON,
    TrialBudget,
    last_trial_read,
)
from app.stages.collect.runs import CollectionRunStore, SameProductTooSoon
from integrations.suppliers.collection import ReadKind
from tests.conftest import LOCAL
from tests.support.jobs_support import FakeClock
from tests.support.onboarding_trial_support import (
    CAFE24_HOST,
    CAFE24_ORIGIN,
    GODOMALL_HOST,
    GODOMALL_ORIGIN,
    IMAGE_HOST,
    MEMBER_EMAIL,
    MEMBER_NAME,
    OPERATOR,
    SESSION_VALUE,
    StubSessions,
    TrialShop,
    cafe24_page,
    cafe24_url,
    ggsan_page,
    godomall_url,
    recon_site,
    row,
    script_rendered_page,
    trial_container,
    upick_page,
)

pytestmark = pytest.mark.integration

HEADERS = {"X-ICBM-Client": "icbm-web"}
CANONICAL = ("product_facts_revisions", "collection_runs", "source_assets")


@pytest.fixture
def shop() -> TrialShop:
    return TrialShop()


@pytest.fixture
def sessions() -> StubSessions:
    return StubSessions()


@pytest.fixture
def app(
    config: AppConfig, clock: FakeClock, shop: TrialShop, sessions: StubSessions
) -> Iterator[Container]:
    with trial_container(config, clock, shop, sessions) as built:
        yield built


def count(config: AppConfig, table: str) -> int:
    connection = sqlite3.connect(database_path(config.data_dir))
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def run_trial(app: Container, key: str, url: str) -> str:
    """Submit one sample and run its job; the job's id."""
    submitted = app.onboarding_analysis.submit(key, url, actor=OPERATOR)
    app.runner.run_next()
    job = app.jobs.get(submitted.job_id)
    assert job.state == "SUCCEEDED", (job.last_error_code, job.last_error_message)
    return submitted.job_id


def refused(code: str, call: object) -> AppError:
    assert callable(call)
    with pytest.raises(AppError) as caught:
        call()
    assert caught.value.code == code, caught.value.code
    return caught.value


# ---------------------------------------------------------------- one trial read (§5 steps 2–5)


def test_a_cafe24_trial_reads_one_page_once_and_writes_no_canonical_truth(
    app: Container, config: AppConfig, shop: TrialShop, sessions: StubSessions
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(101)
    shop.serve(url, cafe24_page(101))
    before = {table: count(config, table) for table in CANONICAL}
    run_trial(app, key, url)

    # Exactly one request: the product page, on the storefront, with the site's session.
    assert shop.requests == [("GET", CAFE24_HOST, "/product/sample/101/")]
    assert SESSION_VALUE in shop.cookies[0]
    assert sessions.asked == [key]
    # OB-02: no revision, no run, no image.
    assert {table: count(config, table) for table in CANONICAL} == before
    assert count(config, "onboarding_trials") == 1
    assert app.onboarding.trials(key)[0].created_by == OPERATOR

    view = app.onboarding_analysis.view(key)
    assert len(view.samples) == 1
    sample = view.samples[0]
    assert sample.sample_url == f"https://{CAFE24_HOST}/product/sample/101"
    statuses = {f.field: f.status for f in sample.fields}
    assert statuses["original_name"] == "CONFIRMED"
    assert statuses["prices"] == "CONFIRMED"
    assert statuses["stock"] == "CONFIRMED"
    assert statuses["options"] == "ABSENT"
    assert statuses["images"] == "CONFIRMED"
    assert sample.summary is not None
    assert sample.summary["name"] == "트라이얼 상품 101"
    assert sample.summary["prices"] == [{"label": "판매가", "amount_krw": 12000}]
    assert sample.summary["stock"] == "ON_SALE"
    assert sample.summary["product_id"] == "101"
    assert sample.summary["images"] == {"count": 1, "urls": [f"https://{IMAGE_HOST}/p/101.jpg"]}
    assert sample.structural == [] and sample.verifiable
    hosts = {(h.host, h.role) for h in view.observed_hosts}
    assert (IMAGE_HOST, "image") in hosts
    assert ("login.trial-auth.com", "login") in hosts
    assert ("assets.trial-cdn.com", "asset") in hosts


def test_the_facts_fingerprint_is_stable_and_names_what_was_shown(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(102)
    shop.serve(url, cafe24_page(102, body_extra='<div class="top">최근 본 상품 3개</div>'))
    run_trial(app, key, url)
    (trial,) = app.onboarding.trials(key)
    # The capture keeps no text outside the product regions: the page furniture's is gone.
    captures = TrialCaptureStore(app.config.onboarding_captures_dir)
    assert "최근 본 상품" not in captures.read(trial.capture_sha256)
    # The stored capture reproduces exactly the fingerprint the trial recorded, so the view shows
    # the values it names: what the operator verifies is what was read.
    (sample,) = app.onboarding_analysis.view(key).samples
    assert sample.facts_fingerprint == trial.facts_fingerprint
    assert sample.summary is not None and sample.summary["name"] == "트라이얼 상품 102"
    # A different price is a different fingerprint.
    clock.advance(11)
    other = cafe24_url(103)
    shop.serve(
        other, cafe24_page(103, rows=row("상품명", "트라이얼 상품 103") + row("판매가", "13,000원"))
    )
    run_trial(app, key, other)
    changed = app.onboarding.trials(key)[-1]
    assert changed.facts_fingerprint != trial.facts_fingerprint


def test_the_capture_is_local_sanitized_and_content_addressed(
    app: Container, config: AppConfig, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(103)
    page = cafe24_page(103, body_extra=f"<p>문의 010-1234-5678 / {SESSION_VALUE}</p>")
    shop.serve(url, page)
    run_trial(app, key, url)
    trial = app.onboarding.trials(key)[0]
    path = config.data_dir / "onboarding" / "captures" / "sha256" / trial.capture_sha256[:2]
    stored = (path / trial.capture_sha256).read_text("utf-8")
    for secret in (
        MEMBER_NAME,
        MEMBER_EMAIL,
        SESSION_VALUE,
        "csrf-secret-value-77",
        "tester01",
        "eyJhbGci",
    ):
        assert secret not in stored, secret
    assert "010-1234-5678" not in stored
    # The product itself stays readable.
    assert "트라이얼 상품 103" in stored and "12,000원" in stored
    # Nothing of the page reaches the trial row or the audit log.
    connection = sqlite3.connect(database_path(config.data_dir))
    try:
        dumped = json.dumps(
            connection.execute("SELECT * FROM onboarding_trials").fetchall()
            + connection.execute("SELECT * FROM audit_events").fetchall(),
            ensure_ascii=False,
            default=str,
        )
    finally:
        connection.close()
    assert MEMBER_NAME not in dumped and "트라이얼 상품 103" not in dumped


def test_the_session_cookie_values_are_redacted_from_a_capture() -> None:
    page = (
        f"<html><body><div class='xans-product-detail'><p>sid={SESSION_VALUE}</p>"
        "<img src='https://img.example.com/p.jpg?token=abc123'></div></body></html>"
    )
    cleaned = sanitize(
        page,
        regions=("xans-product-detail",),
        base_url="https://shop.example.com/product/a/1/",
        safe_query_keys={},
        secrets=[SESSION_VALUE],
    )
    assert SESSION_VALUE not in cleaned
    # A query value no profile declares safe is never kept, a token's least of all.
    assert "abc123" not in cleaned and "token" not in cleaned
    assert 'src="https://img.example.com/p.jpg"' in cleaned


def test_unread_rows_carry_the_slot_a_rule_proposes(app: Container, shop: TrialShop) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(4954)
    shop.serve(url, upick_page("4954"))
    run_trial(app, key, url)
    sample = app.onboarding_analysis.view(key).samples[0]
    unread = {(u.kind, u.label): (u.candidate, u.rule) for u in sample.unread_rows}
    # 회원가 is no default price word: a money row ending in 가 proposes the price slot.
    assert unread[("row", "회원가")] == ("price", "money_row")
    # 판매가능플랫폼's phrase is no configured phrase: the channel words propose "all allowed".
    assert unread[("phrase", "판매가능플랫폼")] == ("channel_all_allowed", "channel_words")
    # A row no rule can place proposes nothing.
    assert unread[("row", "발주마감")] == (None, None)
    # Rows the default words read are not unread.
    assert ("row", "최저판매가") not in unread and ("row", "배송비") not in unread
    # With a price proposed, the price block is a words problem, not a structural one.
    assert sample.structural == []
    statuses = {f.field: f.status for f in sample.fields}
    assert statuses["prices"] == "REVIEW_REQUIRED"


def test_a_godomall_trial_reports_its_rows_and_image_hosts(app: Container, shop: TrialShop) -> None:
    key = recon_site(app, GODOMALL_ORIGIN, "godomall")
    url = godomall_url(1000002412)
    shop.serve(url, ggsan_page("1000002412"))
    run_trial(app, key, url)
    assert shop.requests == [("GET", GODOMALL_HOST, "/goods/goods_view.php?goodsNo=1000002412")]
    view = app.onboarding_analysis.view(key)
    sample = view.samples[0]
    statuses = {f.field: f.status for f in sample.fields}
    assert statuses["original_name"] == "CONFIRMED"
    assert statuses["prices"] == "CONFIRMED"
    assert sample.summary is not None and sample.summary["product_id"] == "1000002412"
    unread = {u.label for u in sample.unread_rows}
    assert {"구매제한", "구매혜택"} <= unread
    # The stock row, the product code and the read rows are not unread.
    assert not {"상품재고", "상품코드", "판매가", "정가", "배송비"} & unread
    hosts = {(h.host, h.role): h for h in view.observed_hosts}
    image = hosts[("godomall-storage.cdn-nhncommerce.com", "image")]
    assert image.confirmable and not image.confirmed


def test_structural_problems_are_flagged_by_rule(
    app: Container, shop: TrialShop, clock: FakeClock
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    cases = {
        cafe24_url(201): (script_rendered_page(), [SCRIPT_RENDERED]),
        cafe24_url(202): (
            cafe24_page(
                202, rows=row("상품명", "트라이얼 상품 202") + row("판매가", "10,000원 이상")
            ),
            [PRICE_BLOCK_UNREADABLE],
        ),
        cafe24_url(203): (cafe24_page(203, duplicate_module=True), ["DUPLICATED_ORDER_LIST"]),
        cafe24_url(204): (cafe24_page(999), [IDENTITY_UNRESOLVED]),
    }
    for url, (page, _) in cases.items():
        shop.serve(url, page)
        run_trial(app, key, url)
        clock.advance(11)
    view = app.onboarding_analysis.view(key)
    found = {sample.sample_url + "/": sample.structural for sample in view.samples}
    for url, (_, codes) in cases.items():
        assert found[url] == codes, (url, found[url])
    assert view.activation["structural_open"] == 4
    assert not any(sample.verifiable for sample in view.samples)


# ---------------------------------------------------------------- refusals and pacing


def test_a_sample_off_the_storefront_or_the_path_form_is_refused_before_any_read(
    app: Container, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    for url in (
        "https://other-shop.co.kr/product/sample/1/",
        f"https://{CAFE24_HOST}/category/list/1/",
        f"http://{CAFE24_HOST}/product/sample/1/",
        f"https://{CAFE24_HOST}/product/sample/1/?token=x",
        f"https://{IMAGE_HOST}/product/sample/1/",
    ):
        refused(
            ONBOARDING_SAMPLE_REFUSED,
            lambda url=url: app.onboarding_analysis.submit(key, url, actor=OPERATOR),
        )
    assert shop.requests == []
    assert app.jobs.count(job_type_prefix="onboarding.") == 0


def verify_all(config: AppConfig, app: Container, key: str) -> None:
    """Verify every trial of ``key`` as O5 will: a test fixture naming what each trial showed."""
    connection = sqlite3.connect(database_path(config.data_dir))
    try:
        for trial in app.onboarding.trials(key):
            connection.execute(
                "INSERT INTO onboarding_verifications VALUES (?, ?, ?, ?, 'CORRECT', 'operator',"
                " '2026-10-12 00:00:00')",
                (f"v-{trial.trial_id}"[:36], trial.trial_id, key, trial.facts_fingerprint),
            )
        connection.commit()
    finally:
        connection.close()


def three_samples(app: Container, clock: FakeClock, shop: TrialShop, key: str, base: int) -> None:
    for number in (base, base + 1, base + 2):
        shop.serve(cafe24_url(number), cafe24_page(number))
        run_trial(app, key, cafe24_url(number))
        clock.advance(11)


@pytest.mark.parametrize("state", ["DRAFT", "ACTIVE", "DISABLED"])
def test_only_a_recon_site_is_analysed(
    app: Container, config: AppConfig, clock: FakeClock, shop: TrialShop, state: str
) -> None:
    record = app.onboarding.create(
        name="트라이얼", origin=CAFE24_ORIGIN, template="cafe24", actor=OPERATOR
    )
    key = record.supplier_key
    if state == "ACTIVE":
        app.onboarding.transition(key, OnboardingState.RECON, actor=OPERATOR)
        three_samples(app, clock, shop, key, 11)
        verify_all(config, app, key)
        app.onboarding.transition(key, OnboardingState.ACTIVE, actor=OPERATOR)
        clock.advance(61)
    elif state == "DISABLED":
        app.onboarding.transition(key, OnboardingState.DISABLED, actor=OPERATOR)
    reads = len(shop.requests)
    url = cafe24_url(1)
    shop.serve(url, cafe24_page(1))
    code = ONBOARDING_NOT_RECON
    refused(code, lambda: app.onboarding_analysis.submit(key, url, actor=OPERATOR))
    refused(code, lambda: app.onboarding_analysis.reanalyse(key, actor=OPERATOR))
    assert len(shop.requests) == reads


def test_the_same_sample_waits_60_s_and_the_site_10_s(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    first, second = cafe24_url(301), cafe24_url(302)
    shop.serve(first, cafe24_page(301))
    shop.serve(second, cafe24_page(302))
    submitted = app.onboarding_analysis.submit(key, first, actor=OPERATOR)
    # Queued and not yet read: the same sample is already waiting.
    refused(
        ONBOARDING_TRIAL_PENDING, lambda: app.onboarding_analysis.submit(key, first, actor=OPERATOR)
    )
    app.runner.run_next()
    assert app.jobs.get(submitted.job_id).state == "SUCCEEDED"
    error = refused(
        ONBOARDING_TRIAL_TOO_SOON,
        lambda: app.onboarding_analysis.submit(key, second, actor=OPERATOR),
    )
    assert error.details["scope"] == "site"
    clock.advance(11)
    run_trial(app, key, second)
    # A sample is read once: after its pacing it is still refused, and only its capture is
    # analysed again.
    clock.advance(120)
    refused(
        ONBOARDING_SAMPLE_ALREADY_READ,
        lambda: app.onboarding_analysis.submit(key, first, actor=OPERATOR),
    )
    assert [path for _, _, path in shop.requests] == [
        "/product/sample/301/",
        "/product/sample/302/",
    ]


def test_at_most_five_samples_are_taken(app: Container, clock: FakeClock, shop: TrialShop) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    for number in range(401, 406):
        shop.serve(cafe24_url(number), cafe24_page(number))
        run_trial(app, key, cafe24_url(number))
        clock.advance(11)
    refused(
        ONBOARDING_SAMPLES_FULL,
        lambda: app.onboarding_analysis.submit(key, cafe24_url(406), actor=OPERATOR),
    )
    clock.advance(600)
    # A sample already taken is never read again.
    refused(
        ONBOARDING_SAMPLE_ALREADY_READ,
        lambda: app.onboarding_analysis.submit(key, cafe24_url(401), actor=OPERATOR),
    )
    assert len(shop.requests) == 5


def test_a_failed_read_uses_its_sample_and_counts_toward_the_five(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    """A read that did not answer the product page still read the supplier: the sample is not
    read again, and it takes one of the five."""
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    failed = cafe24_url(450)
    shop.serve(failed, "", status=302)
    submitted = app.onboarding_analysis.submit(key, failed, actor=OPERATOR)
    app.runner.run_next()
    assert app.jobs.get(submitted.job_id).state == "DEAD"
    assert app.onboarding.trials(key) == ()
    clock.advance(600)
    shop.serve(failed, cafe24_page(450))
    refused(
        ONBOARDING_SAMPLE_ALREADY_READ,
        lambda: app.onboarding_analysis.submit(key, failed, actor=OPERATOR),
    )
    for number in range(451, 455):
        shop.serve(cafe24_url(number), cafe24_page(number))
        run_trial(app, key, cafe24_url(number))
        clock.advance(11)
    refused(
        ONBOARDING_SAMPLES_FULL,
        lambda: app.onboarding_analysis.submit(key, cafe24_url(455), actor=OPERATOR),
    )
    assert len(shop.requests) == 5
    # The panel counts the failed read among the samples used, and offers no sixth.
    assert app.onboarding_analysis.view(key).samples_taken == 5


def test_a_trial_budget_allows_one_product_read_and_nothing_else() -> None:
    budget = TrialBudget()
    for kind in (ReadKind.IMAGE_REQUEST, ReadKind.POLICY_READ):
        with pytest.raises(AppError):
            budget.reserve(kind, "x")
    budget.reserve(ReadKind.PRODUCT_READ, "https://x/product/a/1")
    with pytest.raises(AppError):
        budget.reserve(ReadKind.PRODUCT_READ, "https://x/product/a/1")


def test_a_redirected_read_records_no_trial(app: Container, shop: TrialShop) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(501)
    shop.serve(url, "", status=302)
    submitted = app.onboarding_analysis.submit(key, url, actor=OPERATOR)
    app.runner.run_next()
    job = app.jobs.get(submitted.job_id)
    assert job.state == "DEAD" and job.last_error_code == "ONBOARDING_SAMPLE_UNREAD"
    assert app.onboarding.trials(key) == ()
    assert len(shop.requests) == 1


# ---------------------------------------------------------------- re-analysis (§5 step 6)


def test_reanalysis_after_a_words_change_reads_nothing_and_records_new_trials(
    app: Container, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(4954)
    shop.serve(url, upick_page("4954"))
    run_trial(app, key, url)
    reads = len(shop.requests)
    app.onboarding.save_words(
        key,
        {"purchase_price": ["회원가"], "title_suffix": ["- U-PICK B2B"]},
        expected_seq=0,
        actor=OPERATOR,
    )
    stale = app.onboarding_analysis.view(key)
    assert stale.samples == [] and [s.words_seq for s in stale.stale] == [0]
    result = app.onboarding_analysis.reanalyse(key, actor=OPERATOR)
    assert (result.analysed, result.current, result.missing) == (1, 0, 0)
    assert len(shop.requests) == reads
    trials = app.onboarding.trials(key)
    assert [t.words_seq for t in trials] == [0, 1]
    assert trials[0].capture_sha256 == trials[1].capture_sha256
    sample = result.analysis.samples[0]
    assert sample.words_seq == 1
    statuses = {f.field: f.status for f in sample.fields}
    assert statuses["prices"] == "CONFIRMED" and statuses["original_name"] == "CONFIRMED"
    assert "회원가" not in {u.label for u in sample.unread_rows}
    # Nothing left to re-analyse under the same words.
    again = app.onboarding_analysis.reanalyse(key, actor=OPERATOR)
    assert (again.analysed, again.current) == (0, 1)
    assert len(app.onboarding.trials(key)) == 2


# ---------------------------------------------------------------- observed hosts (D1, OB-01)


def test_a_host_is_confirmed_only_as_a_trial_observed_it(app: Container, shop: TrialShop) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(601)
    shop.serve(url, cafe24_page(601))
    run_trial(app, key, url)
    trial = app.onboarding.trials(key)[0]
    analysis = app.onboarding_analysis
    # Not observed, observed in another role, an asset host, a guarded host: refused.
    refused(
        ONBOARDING_HOST_ROLE_UNOBSERVED,
        lambda: analysis.confirm_host(
            key, "unseen.example.co.kr", "image", trial_id=trial.trial_id, actor=OPERATOR
        ),
    )
    refused(
        ONBOARDING_HOST_ROLE_UNOBSERVED,
        lambda: analysis.confirm_host(
            key, IMAGE_HOST, "login", trial_id=trial.trial_id, actor=OPERATOR
        ),
    )
    refused(
        ONBOARDING_HOST_ROLE_UNOBSERVED,
        lambda: analysis.confirm_host(
            key, "assets.trial-cdn.com", "asset", trial_id=trial.trial_id, actor=OPERATOR
        ),
    )
    refused(
        "ONBOARDING_HOST_REFUSED",
        lambda: analysis.confirm_host(
            key, "127.0.0.1", "image", trial_id=trial.trial_id, actor=OPERATOR
        ),
    )
    assert IMAGE_HOST not in app.supplier_catalog.collections[key].collection.profile.image_hosts
    record = analysis.confirm_host(
        key, IMAGE_HOST, "image", trial_id=trial.trial_id, actor=OPERATOR
    )
    assert IMAGE_HOST in record.hosts_of(HostRole.IMAGE)
    # The confirmed image host joins the collection egress at once.
    profile = app.supplier_catalog.collections[key].collection.profile
    assert IMAGE_HOST in profile.image_hosts and IMAGE_HOST in profile.hosts
    view = app.onboarding_analysis.view(key)
    image = next(h for h in view.observed_hosts if h.host == IMAGE_HOST)
    assert image.confirmed and not image.confirmable
    analysis.disable_host(key, IMAGE_HOST, "image", actor=OPERATOR)
    assert IMAGE_HOST not in app.supplier_catalog.collections[key].collection.profile.image_hosts


# ---------------------------------------------------------------- the ACTIVE gate (§7)


def test_three_read_samples_are_ready_and_wait_only_for_verification(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    for number in (701, 702, 703):
        shop.serve(cafe24_url(number), cafe24_page(number))
        run_trial(app, key, cafe24_url(number))
        clock.advance(11)
    view = app.onboarding_analysis.view(key)
    assert view.verifiable_samples == 3
    assert view.activation == {
        "words_seq": 0,
        "template_revision": view.template_revision,
        "required_samples": 3,
        "proven_samples": 0,
        "trials": 3,
        "core_unread": 0,
        "unverified": 3,
        "refuted": 0,
        "structural_open": 0,
    }
    refused(
        "ONBOARDING_ACTIVATION_NOT_PROVEN",
        lambda: app.onboarding.transition(key, OnboardingState.ACTIVE, actor=OPERATOR),
    )


def test_test_only_verifications_of_o3_trials_prove_active(
    app: Container, config: AppConfig, clock: FakeClock, shop: TrialShop
) -> None:
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    three_samples(app, clock, shop, key, 711)
    verify_all(config, app, key)
    proof = app.onboarding.activation(key)
    assert proof.proven and len(proof.proven_samples) == 3
    # The database's own gate agrees: options ABSENT is a read "no options".
    app.onboarding.transition(key, OnboardingState.ACTIVE, actor=OPERATOR)
    assert app.onboarding.current(key).state is OnboardingState.ACTIVE


# ---------------------------------------------------------------- the API


@pytest.fixture
def api(config: AppConfig, shop: TrialShop, sessions: StubSessions) -> Iterator[TestClient]:
    application = create_app(
        config, collection_gateway=shop.gateway(), collection_sessions=sessions
    )
    with TestClient(application, base_url=LOCAL) as client:
        yield client


def test_the_api_queues_a_trial_and_reports_it(api: TestClient, shop: TrialShop) -> None:
    container = api.app.state.container  # type: ignore[attr-defined]
    key = recon_site(container, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(801)
    shop.serve(url, cafe24_page(801))
    base = f"/api/v1/onboarding/suppliers/{key}"
    # CSRF: every write needs the client header.
    assert api.post(f"{base}/trials", json={"sample_url": url}).status_code == 403
    queued = api.post(f"{base}/trials", json={"sample_url": url}, headers=HEADERS)
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    from tests.support.onboarding_support import wait_until

    def done() -> bool:
        return api.get(f"/api/v1/system/jobs/{job_id}").json()["state"] in ("SUCCEEDED", "DEAD")

    wait_until(done)
    report = api.get(f"{base}/trials")
    assert report.status_code == 200
    body = report.json()
    assert body["verifiable_samples"] == 1 and body["sample_limit"] == 5
    assert body["samples_taken"] == 1
    sample = body["samples"][0]
    assert sample["summary"]["name"] == "트라이얼 상품 801"
    trial_id = sample["trial_id"]
    again = api.post(f"{base}/trials", json={"sample_url": url}, headers=HEADERS)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == ONBOARDING_SAMPLE_ALREADY_READ
    off_host = api.post(
        f"{base}/trials",
        json={"sample_url": "https://elsewhere.co.kr/product/a/1/"},
        headers=HEADERS,
    )
    assert (
        off_host.status_code == 422
        and off_host.json()["error"]["code"] == ONBOARDING_SAMPLE_REFUSED
    )
    confirmed = api.post(
        f"{base}/hosts",
        json={"host": IMAGE_HOST, "role": "image", "trial_id": trial_id},
        headers=HEADERS,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert {"host": IMAGE_HOST, "role": "image"} in confirmed.json()["hosts"]
    disabled = api.post(
        f"{base}/hosts/disable", json={"host": IMAGE_HOST, "role": "image"}, headers=HEADERS
    )
    assert disabled.status_code == 200
    assert {"host": IMAGE_HOST, "role": "image"} not in disabled.json()["hosts"]
    reanalysed = api.post(f"{base}/reanalyse", headers=HEADERS)
    assert reanalysed.status_code == 200 and reanalysed.json()["current"] == 1
    assert SESSION_VALUE not in report.text and MEMBER_NAME not in report.text


def test_a_trial_read_paces_collects_next_read_of_that_product(
    app: Container, clock: FakeClock, shop: TrialShop
) -> None:
    """The same-product interval is one owner's (OB-06): a trial's read is counted by COLLECT's
    reservation, so an ordinary collection waits the interval after a trial read the product."""
    key = recon_site(app, CAFE24_ORIGIN, "cafe24")
    url = cafe24_url(901)
    shop.serve(url, cafe24_page(901))
    run_trial(app, key, url)
    entry = app.supplier_catalog.onboarded(key)
    assert entry is not None and entry.bound is not None
    pacing = pacing_key(entry.bound.collection, url)
    # The run store as the container wires it.
    runs = CollectionRunStore(app.db, clock, other_reads=(last_trial_read,))
    with app.db.write() as session:
        run_id = runs.open(
            session,
            job_id="job-after-trial",
            correlation_id="cid-after-trial",
            supplier_key=key,
            source_url=url,
        )
    with pytest.raises(SameProductTooSoon):
        runs.reserve_product_read(run_id, key=pacing, interval_s=60)
    assert runs.seconds_until_readable(pacing, interval_s=60) > 0
    # The trial's own check asks about collection runs alone, so it never counts itself.
    assert runs.seconds_until_readable(pacing, interval_s=60, collection_runs_only=True) == 0
    clock.advance(61)
    runs.reserve_product_read(run_id, key=pacing, interval_s=60)
    # And a collection read paces the next trial read of the product the other way round.
    other = cafe24_url(902)
    with app.db.write() as session:
        collected = runs.open(
            session,
            job_id="job-before-trial",
            correlation_id="cid-before-trial",
            supplier_key=key,
            source_url=other,
        )
    runs.reserve_product_read(
        collected, key=pacing_key(entry.bound.collection, other), interval_s=60
    )
    clock.advance(11)
    shop.serve(other, cafe24_page(902))
    error = refused(
        ONBOARDING_TRIAL_TOO_SOON,
        lambda: app.onboarding_analysis.submit(key, other, actor=OPERATOR),
    )
    assert error.details["scope"] == "sample"
