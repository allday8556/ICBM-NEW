"""SmartStore capability projection in the real UI (M2 PR-D; CAPABILITY_MAPPING §14, §14.8–§14.11).

``test_s17_06_*``, ``test_s17_07_*`` and ``test_s17_16_*`` are the named tests for the PR-D (UI)
halves of CAPABILITY_MAPPING.md §17 targets 6, 7 and 16 (ownership registry §3.2). Each state is
produced through the real capability, attestation and SmartStore services and their database. The
page is the real UI in the installed browser, and every browser request is answered in-process by
the application under test: nothing reaches a network, the SmartStore caller is never invoked,
and the page makes read (GET) requests only.
"""

import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.connect.marketplace.attestation import ApiGroup
from app.connect.marketplace.capability import (
    AuthEvidence,
    ContractFreshness,
    EvidenceStrength,
    FailureEvidence,
    Finding,
    Generations,
    IdentityProof,
    WorkflowScope,
    WriteScope,
    WriteScopeStatus,
)
from app.container import Container
from app.core.egress import EGRESS
from app.core.errors import ErrorClass
from app.main import create_app
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from tests.conftest import LOCAL

pytestmark = pytest.mark.integration

KEY = "smartstore"
ACTOR = "operator:test"
# The channels test_ownership_children uses: the locally installed browser, nothing downloaded.
BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
SECRET = "$2a$04$abcdefghijklmnopqrstuu"  # a fixture bcrypt salt, never a real credential
CLIENT_ID = "fixture-client-id-pd01"
UID = "uid-fixture-A"
NOW = datetime(2026, 9, 15, 3, 0, tzinfo=UTC)
GENERATIONS = Generations(1, 1)
API_TAB = f"{LOCAL}/#/settings?tab=smartstore&sub=api"
COMMON_TAB = f"{LOCAL}/#/settings?tab=common&sub=basic"

ROWS = """(selector) => [...document.querySelectorAll(selector)].map((row) => {
  const chip = row.querySelector('.cap-status');
  const glyph = chip.querySelector('.glyph');
  return {
    label: row.firstElementChild.textContent,
    axis: chip.dataset.axis,
    glyph: glyph ? glyph.textContent : null,
    strength: glyph ? glyph.dataset.strength : null,
    text: chip.querySelector('.cap-text').textContent,
    tone: ['good', 'warn', 'bad', 'info'].find((tone) => chip.classList.contains(tone)) ?? null,
  };
})"""

Row = tuple[str, str | None, str, str | None]  # label, marker, text, tone
Seed = Callable[[Container], None]


# ---------------------------------------------------------------- harness


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            launched = playwright.chromium.launch(channel=BROWSER_CHANNEL, headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({type(exc).__name__})")
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def client(config: AppConfig, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real application. Any SmartStore provider call fails the test."""

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("the projection reached the SmartStore caller")

    monkeypatch.setattr(SmartStoreEndpointCaller, "call", refuse)
    with TestClient(create_app(config), base_url=LOCAL) as test_client:
        yield test_client


def _container(client: TestClient) -> Container:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    return container


@contextmanager
def _page(browser: Browser, client: TestClient, methods: list[str]) -> Iterator[Page]:
    def answer(route: Route) -> None:
        methods.append(route.request.method)
        parts = urlsplit(route.request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        response = client.request(route.request.method, path)
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        yield page
    finally:
        page.close()


def _dated(text: str) -> str:
    return re.sub(r"\d{4}\.\d{2}\.\d{2}", "YYYY.MM.DD", text)


def _rows(page: Page, selector: str) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = page.evaluate(ROWS, selector)
    return rows


def _projection(browser: Browser, client: TestClient) -> tuple[list[Row], list[str]]:
    methods: list[str] = []
    with _page(browser, client, methods) as page:
        page.goto(API_TAB)
        page.wait_for_selector(".capability-projection .kv", timeout=15_000)
        rows = [
            (str(r["label"]), r["glyph"], _dated(str(r["text"])), r["tone"])
            for r in _rows(page, ".capability-projection .kv")
        ]
    return rows, methods


@contextmanager
def _no_egress() -> Iterator[None]:
    """Ownership registry §3.2: every named §17 6/7/16 rendering test asserts zero egress. The
    process-wide guard's blocked attempts and granted events are unchanged across the block."""
    before = EGRESS.snapshot()
    yield
    after = EGRESS.snapshot()
    assert (after["external_attempts"], after["granted_events"]) == (
        before["external_attempts"],
        before["granted_events"],
    )


# ---------------------------------------------------------------- seeds (real services)


def _current(c: Container) -> None:
    c.marketplace_capability.record_contract_freshness(KEY, ContractFreshness.CURRENT, actor=ACTOR)


def _freshness(value: ContractFreshness) -> Seed:
    return lambda c: c.marketplace_capability.record_contract_freshness(KEY, value, actor=ACTOR)


def _auth(*, observed: str | None = UID) -> Seed:
    proof = IdentityProof(GENERATIONS, observed, NOW) if observed else None
    evidence = AuthEvidence(True, UID, GENERATIONS, proof)
    return lambda c: c.marketplace_capability.observe_auth(KEY, evidence)  # type: ignore[func-returns-value]


def _attest(*groups: ApiGroup) -> Seed:
    def seed(c: Container) -> None:
        # The application identity comes from the committed credential bundle (PR-A wiring).
        c.smartstore.save_credentials(CLIENT_ID, SECRET, actor=ACTOR)
        c.permission_attestation.attest(KEY, list(groups), actor=ACTOR)

    return seed


def _fail(scope: WorkflowScope, error_class: ErrorClass, finding: Finding) -> Seed:
    failure = FailureEvidence(scope, error_class, finding)
    return lambda c: c.marketplace_capability.observe_failure(KEY, failure)  # type: ignore[func-returns-value]


def _machine_verified(c: Container) -> None:
    # No M2 path produces MACHINE_VERIFIED; the domain keeps the strength, so it is seeded here.
    ready = WriteScope(WriteScopeStatus.READY, EvidenceStrength.MACHINE_VERIFIED)
    c.marketplace_capability.observe_permission(KEY, ready)


def _seed(client: TestClient, *seeds: Seed) -> None:
    container = _container(client)
    for seed in seeds:
        seed(container)


AUTH_READY: Row = ("인증", "●", "연결됨", "good")
UNBOUND: Row = ("인증", "○", "연결 필요", None)
PERMISSION_UNKNOWN: Row = ("등록 권한", "○", "권한 미확인", None)
PERMISSION_ATTESTED: Row = ("등록 권한", "◐", "권한 확인됨 (관리자 화면 확인)", "info")
WRITE_UNVERIFIED: Row = ("실제 등록", "○", "미확인", None)
CONTRACT_CURRENT: Row = ("API 계약", None, "API 계약 확인됨 (운영자 기록 · YYYY.MM.DD)", None)
CONTRACT_UNRECORDED: Row = ("API 계약", None, "API 계약 상태 미확인", None)


# ---------------------------------------------------------------- §17 targets 6 and 7


def test_s17_06_operator_attested_permission_renders_the_limited_marker_never_strong(
    browser: Browser, client: TestClient
) -> None:
    with _no_egress():
        _seed(client, _current, _attest(ApiGroup.PRODUCT), _auth())
        rows, methods = _projection(browser, client)
        # The A0 card's current-truth chip carries the same limited marker.
        with _page(browser, client, methods) as page:
            page.goto(API_TAB)
            page.wait_for_selector(
                '.permission-attestation [data-axis="permission"]', timeout=15_000
            )
            (chip,) = _rows(page, '.permission-attestation .kv:has([data-axis="permission"])')
            strong = page.locator('[data-axis="permission"] [data-strength="strong"]').count()
    assert rows[:4] == [AUTH_READY, PERMISSION_ATTESTED, WRITE_UNVERIFIED, CONTRACT_CURRENT]
    assert (chip["glyph"], chip["strength"]) == ("◐", "limited")
    assert chip["text"] == "권한 확인됨 (관리자 화면 확인) · 반영됨"
    assert strong == 0  # operator evidence is never drawn with the strong marker
    assert set(methods) == {"GET"}


def test_s17_07_machine_verified_permission_renders_the_distinct_strong_marker(
    browser: Browser, client: TestClient
) -> None:
    with _no_egress():
        _seed(client, _current, _auth(), _machine_verified)
        rows, methods = _projection(browser, client)
    assert rows[1] == ("등록 권한", "●", "권한 확인됨 (자동 확인)", "good")
    # Distinct from the limited operator-attested line in marker, wording and tone.
    assert rows[1] != PERMISSION_ATTESTED
    assert {rows[1][1], PERMISSION_ATTESTED[1]} == {"●", "◐"}
    assert set(methods) == {"GET"}


# ---------------------------------------------------------------- §17 target 16


STATES: dict[str, tuple[tuple[Seed, ...], list[Row]]] = {
    "brand-new": (
        (),
        [UNBOUND, PERMISSION_UNKNOWN, WRITE_UNVERIFIED, CONTRACT_UNRECORDED],
    ),
    "connected-permission-unknown": (
        (_current, _auth()),
        [AUTH_READY, PERMISSION_UNKNOWN, WRITE_UNVERIFIED, CONTRACT_CURRENT],
    ),
    "stale-contract-keeps-the-proven-auth": (
        (_current, _auth(), _freshness(ContractFreshness.STALE)),
        [
            AUTH_READY,
            PERMISSION_UNKNOWN,
            WRITE_UNVERIFIED,
            ("API 계약", None, "API 계약 재검토 필요", "warn"),
        ],
    ),
    "contract-under-review": (
        (_current, _freshness(ContractFreshness.REVIEW_REQUIRED)),
        [
            UNBOUND,
            PERMISSION_UNKNOWN,
            WRITE_UNVERIFIED,
            ("API 계약", None, "API 계약 검토 필요", "bad"),
        ],
    ),
    "bound-without-current-proof": (
        (_current, _auth(observed=None)),
        [
            ("인증", "○", "연결 확인 전", None),
            PERMISSION_UNKNOWN,
            WRITE_UNVERIFIED,
            CONTRACT_CURRENT,
        ],
    ),
    "authentication-review": (
        (
            _current,
            _auth(),
            _fail(WorkflowScope.AUTHENTICATION, ErrorClass.UNKNOWN, Finding.UNRESOLVED),
        ),
        [
            ("인증", None, "확인 필요", "warn"),
            PERMISSION_UNKNOWN,
            WRITE_UNVERIFIED,
            CONTRACT_CURRENT,
            ("조치 필요", None, "인증 · 확인 필요", "warn"),
            ("최근 오류 분류", None, "UNKNOWN (원인 미확정)", None),
        ],
    ),
    "account-mismatch": (
        (_current, _auth(observed="uid-fixture-other")),
        [
            ("인증", None, "계정 확인 필요", "bad"),
            PERMISSION_UNKNOWN,
            WRITE_UNVERIFIED,
            CONTRACT_CURRENT,
            ("조치 필요", None, "인증 · 확인 필요", "warn"),
        ],
    ),
    "auth-retry-limit": (
        (
            _current,
            _auth(),
            _fail(WorkflowScope.AUTHENTICATION, ErrorClass.AUTH, Finding.AUTH_RECOVERY_EXHAUSTED),
        ),
        [
            ("인증", None, "인증 확인 필요", "warn"),
            PERMISSION_UNKNOWN,
            WRITE_UNVERIFIED,
            CONTRACT_CURRENT,
            ("조치 필요", None, "인증 · 인증 재시도 한도 초과", "bad"),
            ("최근 오류 분류", None, "AUTH (인증 오류)", None),
        ],
    ),
    "account-restricted": (
        (
            _fail(
                WorkflowScope.AUTHENTICATION,
                ErrorClass.POLICY_BLOCKED,
                Finding.ACCOUNT_RESTRICTION_PROVEN,
            ),
        ),
        [
            ("인증", None, "계정 제한 확인 필요", "warn"),
            PERMISSION_UNKNOWN,
            WRITE_UNVERIFIED,
            CONTRACT_UNRECORDED,
            ("조치 필요", None, "인증 · 계정 제한 확인 필요", "bad"),
            ("최근 오류 분류", None, "POLICY_BLOCKED (정책 차단)", None),
        ],
    ),
    "permission-missing": (
        (_current, _attest()),
        [
            UNBOUND,
            ("등록 권한", None, "권한 부족", "bad"),
            ("실제 등록", None, "차단됨", "bad"),
            CONTRACT_CURRENT,
            ("조치 필요", None, "상품 등록 · 필수 API 권한 부족", "bad"),
        ],
    ),
    "registration-review-keeps-the-write-label": (
        (_fail(WorkflowScope.PRODUCT_REGISTRATION, ErrorClass.UNKNOWN, Finding.UNRESOLVED),),
        [
            UNBOUND,
            PERMISSION_UNKNOWN,
            WRITE_UNVERIFIED,
            CONTRACT_UNRECORDED,
            ("조치 필요", None, "상품 등록 · 확인 필요", "warn"),
            ("최근 오류 분류", None, "UNKNOWN (원인 미확정)", None),
        ],
    ),
}


@pytest.mark.parametrize(("seeds", "expected"), STATES.values(), ids=STATES.keys())
def test_s17_16_the_ui_keeps_every_axis_on_its_own_line(
    browser: Browser, client: TestClient, seeds: tuple[Seed, ...], expected: list[Row]
) -> None:
    with _no_egress():
        _seed(client, *seeds)
        rows, methods = _projection(browser, client)
    assert rows == expected
    assert set(methods) == {"GET"}  # read-only: PR-D adds no mutating request


def test_s17_16_the_common_summary_takes_smartstore_from_its_capability(
    browser: Browser, client: TestClient
) -> None:
    # §14.11 surface 2: the SmartStore row is the authentication line, never a hard-coded
    # 미연동; marketplaces without a capability contract are unchanged.
    methods: list[str] = []
    with _no_egress():
        _seed(client, _current, _auth())
        with _page(browser, client, methods) as page:
            page.goto(COMMON_TAB)
            page.wait_for_selector(
                '.alert-row[data-marketplace="smartstore"] .cap-status', timeout=15_000
            )
            (smartstore,) = _rows(page, '.alert-row[data-marketplace="smartstore"]')
            others = {
                key: page.locator(f'.alert-row[data-marketplace="{key}"] .chip').inner_text()
                for key in ("coupang", "st11")
            }
    assert (smartstore["glyph"], smartstore["text"], smartstore["tone"]) == ("●", "연결됨", "good")
    assert others == {"coupang": "미연동", "st11": "미연동"}
    assert set(methods) == {"GET"}
