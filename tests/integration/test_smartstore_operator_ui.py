"""SmartStore operator actions in the real UI (M2 PR-E; instructions §8A, Issue #44 §2, §4, §7).

The page is the real UI in the installed browser. Every browser request — method, body and the
client header included — is answered in-process by the application under test, whose SmartStore
caller talks to a fake transport: no test here makes a real SmartStore call or reaches a network.
PR-E owns no CAPABILITY_MAPPING §17 target and claims none of PR-D's projection coverage.
"""

import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.connect.marketplace.capability import FailureEvidence, Finding, WorkflowScope
from app.container import Container
from app.core.egress import EGRESS
from app.core.errors import ErrorClass
from app.main import create_app
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from tests.conftest import LOCAL
from tests.integration.test_smartstore_operator_api import (
    CLIENT,
    CLIENT_ID,
    MARKETPLACE,
    SECRET,
    UID_A,
    UID_B,
    Provider,
)

pytestmark = pytest.mark.integration

KEY = "smartstore"
BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
API_TAB = f"{LOCAL}/#/settings?tab=smartstore&sub=api"
FORWARDED = ("x-icbm-client", "content-type", "accept")
AUTH_TEXT = ".capability-projection [data-axis='auth'] .cap-text"


@dataclass
class Traffic:
    """Every request the page made: (method, path, JSON body, carried the client header)."""

    requests: list[tuple[str, str, object, bool]] = field(default_factory=list)

    def writes(self) -> list[tuple[str, str, object]]:
        return [(m, p, b) for m, p, b, _ in self.requests if m != "GET"]


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
def provider() -> Provider:
    return Provider()


@pytest.fixture
def client(config: AppConfig, provider: Provider) -> Iterator[TestClient]:
    app = create_app(
        config.with_overrides(smartstore_renewal_margin_s=600),
        smartstore_caller=SmartStoreEndpointCaller(transport=httpx.MockTransport(provider)),
    )
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client


@contextmanager
def _page(browser: Browser, client: TestClient, traffic: Traffic) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        body = request.post_data_buffer
        traffic.requests.append(
            (
                request.method,
                parts.path,
                json.loads(body) if body else None,
                "x-icbm-client" in {k.lower() for k in request.headers},
            )
        )
        response = client.request(request.method, path, content=body, headers=headers)
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(API_TAB)
        page.wait_for_selector(".capability-projection .kv", timeout=15_000)
        yield page
    finally:
        page.close()


@contextmanager
def _no_egress() -> Iterator[None]:
    before = EGRESS.snapshot()
    yield
    after = EGRESS.snapshot()
    assert (after["external_attempts"], after["granted_events"]) == (
        before["external_attempts"],
        before["granted_events"],
    )


def _container(client: TestClient) -> Container:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    return container


def _binding(config: AppConfig) -> str | None:
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        row = raw.execute(
            "SELECT provider_account_uid FROM marketplace_connections WHERE marketplace_key = ?",
            (KEY,),
        ).fetchone()
    return row[0] if row else None


def _storage(page: Page) -> str:
    stored: str = page.evaluate(
        "() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])"
    )
    return stored


def _prepare(client: TestClient) -> None:
    """Credentials saved and the contract recorded CURRENT, through the operator API."""
    saved = client.put(
        f"{MARKETPLACE}/credentials",
        json={"client_id": CLIENT_ID, "client_secret": SECRET},
        headers=CLIENT,
    )
    assert saved.status_code == 200, saved.text
    recorded = client.post(
        f"{MARKETPLACE}/contract-freshness", json={"contract_freshness": "CURRENT"}, headers=CLIENT
    )
    assert recorded.status_code == 200, recorded.text


def _wait_auth(page: Page, text: str) -> None:
    page.wait_for_function(
        "([selector, text]) => document.querySelector(selector)?.textContent === text",
        arg=[AUTH_TEXT, text],
        timeout=15_000,
    )


# ---------------------------------------------------------------- credentials


def test_the_ui_replaces_credentials_without_ever_showing_the_secret(
    browser: Browser, client: TestClient, provider: Provider
) -> None:
    traffic = Traffic()
    with _no_egress(), _page(browser, client, traffic) as page:
        assert page.locator(".smartstore-credentials").inner_text().count("미설정") == 1
        # The prototype's store-identity input is gone: identity comes only from the account read.
        assert page.locator("text=스토어 식별값").count() == 0
        page.fill("#smartstore-client-id", CLIENT_ID)
        page.fill("#smartstore-client-secret", SECRET)
        page.click("[data-action='save-credentials']")
        page.wait_for_selector(".smartstore-credentials .chip.good", timeout=15_000)
        status = page.locator(".smartstore-credentials .chip.good").inner_text()
        secret_left = page.input_value("#smartstore-client-secret")
        html, stored = page.content(), _storage(page)
    assert status == "저장됨 · 자격증명 세대 1"
    assert secret_left == ""
    assert SECRET not in html and SECRET not in stored
    assert traffic.writes() == [
        ("PUT", f"{MARKETPLACE}/credentials", {"client_id": CLIENT_ID, "client_secret": SECRET})
    ]
    assert all(carried for method, _, _, carried in traffic.requests if method != "GET")
    assert provider.calls == []  # saving credentials calls nothing


# ---------------------------------------------------------------- observation, then first binding


def test_observation_and_first_binding_are_separate_operator_actions(
    browser: Browser, client: TestClient, config: AppConfig
) -> None:
    _prepare(client)
    traffic = Traffic()
    with _no_egress(), _page(browser, client, traffic) as page:
        page.click("[data-action='observe']")
        page.wait_for_selector(".account-observation .kv", timeout=15_000)
        shown = page.locator(".account-observation").inner_text()
        bind = page.locator("[data-action='bind']")
        assert UID_A in shown
        assert bind.is_disabled()  # nothing commits until the operator confirms
        assert _binding(config) is None
        assert not any(path.endswith("/bind") for _, path, _ in traffic.writes())

        # A reload forgets the unconfirmed identity and still binds nothing.
        page.reload()
        page.wait_for_selector(".capability-projection .kv", timeout=15_000)
        assert page.locator(".account-observation .kv").count() == 0
        assert UID_A not in page.content() and UID_A not in _storage(page)
        assert _binding(config) is None

        # Observe again, confirm explicitly, then commit.
        page.click("[data-action='observe']")
        page.wait_for_selector("#smartstore-bind-confirm", timeout=15_000)
        page.check("#smartstore-bind-confirm")
        page.click("[data-action='bind']")
        _wait_auth(page, "연결됨")  # the projection re-read the server's truth
        stored = _storage(page)
    assert _binding(config) == UID_A
    binds = [body for _, path, body in traffic.writes() if path.endswith("/bind")]
    assert binds == [{"confirmed_account_uid": UID_A}]  # exactly the identity shown
    assert UID_A not in stored


def test_a_bound_account_is_offered_no_rebind(
    browser: Browser, client: TestClient, config: AppConfig, provider: Provider
) -> None:
    _prepare(client)
    client.post(f"{MARKETPLACE}/connect", headers=CLIENT)
    assert client.post(
        f"{MARKETPLACE}/bind", json={"confirmed_account_uid": UID_A}, headers=CLIENT
    ).is_success
    provider.account_uid = UID_B
    traffic = Traffic()
    with _no_egress(), _page(browser, client, traffic) as page:
        page.click("[data-action='observe']")
        page.wait_for_selector(".account-observation .kv", timeout=15_000)
        shown = page.locator(".account-observation").inner_text()
        offered = page.locator("[data-action='bind']").count()
        _wait_auth(page, "계정 확인 필요")
    assert UID_B in shown and "불일치" in shown
    assert offered == 0
    assert _binding(config) == UID_A


# ---------------------------------------------------------------- resolution and contract review


def test_workflow_actions_offer_only_the_typed_resolution(
    browser: Browser, client: TestClient
) -> None:
    failure = FailureEvidence(WorkflowScope.AUTHENTICATION, ErrorClass.UNKNOWN, Finding.UNRESOLVED)
    _container(client).marketplace_capability.observe_failure(KEY, failure)
    traffic = Traffic()
    with _no_egress(), _page(browser, client, traffic) as page:
        action = page.locator(".workflow-action[data-scope='AUTHENTICATION'] button")
        assert action.get_attribute("data-resolution") == "REVIEW_RESOLVED"
        action.click()
        page.wait_for_function(
            "() => document.querySelectorAll('.workflow-action').length === 0", timeout=15_000
        )
        auth = page.locator(AUTH_TEXT).inner_text()
    assert auth != "연결됨"  # resolving a review never makes anything READY
    assert traffic.writes() == [
        (
            "POST",
            f"{MARKETPLACE}/workflow-resolution",
            {"workflow_scope": "AUTHENTICATION", "resolution": "REVIEW_RESOLVED"},
        )
    ]


def test_scope_insufficient_offers_no_generic_resolution(
    browser: Browser, client: TestClient
) -> None:
    _prepare(client)
    client.post(
        f"{MARKETPLACE}/permission-attestation", json={"observed_groups": []}, headers=CLIENT
    )
    traffic = Traffic()
    with _no_egress(), _page(browser, client, traffic) as page:
        row = page.locator(".workflow-action[data-scope='PRODUCT_REGISTRATION']")
        buttons = row.locator("button").count()
        text = row.inner_text()
    assert buttons == 0
    assert "등록 권한 확인" in text


def test_the_contract_review_is_recorded_as_an_icbm_review(
    browser: Browser, client: TestClient
) -> None:
    traffic = Traffic()
    with _no_egress(), _page(browser, client, traffic) as page:
        page.select_option("#contract-review-smartstore", "CURRENT")
        page.click("[data-action='record-freshness']")
        page.wait_for_function(
            "() => document.querySelector(\"[data-axis='freshness'] .cap-text\")"
            "?.textContent.includes('운영자 기록')",
            timeout=15_000,
        )
    assert traffic.writes() == [
        ("POST", f"{MARKETPLACE}/contract-freshness", {"contract_freshness": "CURRENT"})
    ]
