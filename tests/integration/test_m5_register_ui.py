"""The Registration Management screen in the real UI (M5 PR-F §B).

The page runs in the installed browser and every request it makes is answered in-process by the
application under test. What is proven here is the division of labour: the screen renders the
server's state and the server's action verdicts, and computes none of them. A disabled action is
disabled because the server said so; pressing an offered one calls the contract that offered it;
and a reload rebuilds the same screen from durable rows, with nothing kept in the page.

No provider is reached: CREATE stays NOT_ADOPTED and every seam refuses locally.
"""

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.connect.marketplace.capability import RemoteOutcome
from app.container import Container
from app.core.errors import AppError, ErrorClass
from app.main import create_app
from app.register.execution import CREATE_ENDPOINT_GROUP
from tests.conftest import LOCAL
from tests.integration.test_m5_register_execution import (
    FakeSender,
    context,
    execution,
    prepare,
)
from tests.product_support import Collections
from tests.register_support import MARKET, Preparation, establish, preparation

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
REGISTER = f"{LOCAL}/#/register"
FORWARDED = ("x-icbm-client", "content-type", "accept")


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
def client(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as test_client:
        yield test_client


@pytest.fixture
def container(client: TestClient) -> Container:
    served: Container = client.app.state.container  # type: ignore[attr-defined]
    return served


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


@pytest.fixture
def prep(container: Container, account: str) -> Preparation:
    return preparation(container, account)


@contextmanager
def _page(browser: Browser, client: TestClient, writes: list[tuple[str, str]]) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        body = request.post_data_buffer
        if request.method != "GET":
            writes.append((request.method, parts.path))
        response = client.request(request.method, path, content=body, headers=headers)
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(REGISTER)
        page.wait_for_selector(".register-canary", timeout=15_000)
        yield page
    finally:
        page.close()


def _action(page: Page, action: str) -> dict[str, object]:
    button = page.locator(f"button[data-action='{action}']").first
    return {
        "disabled": button.is_disabled(),
        "label": button.inner_text().strip(),
    }


def test_the_screen_renders_server_state_and_the_servers_verdicts(
    browser: Browser,
    client: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        # The unit, its account and its frozen identity come from the server.
        unit = page.locator(f".register-unit[data-draft='{ready.draft_id}']")
        assert unit.count() == 1
        assert account in unit.inner_text()
        # Every action the server disabled is disabled here, with the server's reason rendered.
        assert _action(page, "CREATE_ENQUEUE")["disabled"] is True
        assert _action(page, "RECONCILE")["disabled"] is True
        assert _action(page, "VERIFY")["disabled"] is True
        assert _action(page, "RESUME_SCOPE")["disabled"] is True
        # The canary plan is BLOCKED and names the unadopted contracts, not "not needed".
        canary = page.locator(".register-canary")
        assert canary.get_attribute("data-canary") == "BLOCKED"
        create = page.locator("li[data-requirement='CREATE_ADOPTED']")
        assert create.get_attribute("data-satisfied") == "false"
        assert "SMARTSTORE_PRODUCT_CREATE_V2" in create.inner_text()
        # The page performed no write at all while rendering.
        assert writes == []


def test_an_unknown_outcome_offers_reconcile_only(
    browser: Browser,
    client: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    run = execution(
        container, prep, sender=FakeSender(outcome=RemoteOutcome.UNKNOWN, product_id=None)
    )
    with pytest.raises(AppError):
        run.service.run(context(ready))
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        text = page.locator(f".register-unit[data-draft='{ready.draft_id}']").inner_text()
        # An unproven outcome is shown as unproven — never as a failure, never as confirmed.
        assert "결과 미확인" in text
        assert "확인 완료" not in text
        assert _action(page, "CREATE_ENQUEUE")["disabled"] is True
        assert _action(page, "RECONCILE")["disabled"] is False
        # Pressing the offered action calls the contract that offered it, and nothing else.
        page.locator("button[data-action='RECONCILE']").first.click()
        page.wait_for_timeout(500)
        assert writes == [("POST", f"/api/v1/register/intents/{ready.intent_id}/reconcile")]
        # With no adopted lookup the server resolves nothing, and the page does not pretend.
        assert container.registrations.intent(ready.intent_id).state.value == "UNKNOWN"


def test_an_auth_brake_is_shown_and_never_offered_a_resume(
    browser: Browser,
    client: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.AUTH,
            error_code="PROVIDER_AUTH",
        ),
    )
    with pytest.raises(AppError):
        run.service.run(context(ready))
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        scope = page.locator(".register-scope").first
        assert scope.get_attribute("data-scope-state") == "PAUSED"
        assert "인증 중단" in scope.inner_text()
        assert _action(page, "RESUME_SCOPE")["disabled"] is True
        # A reload rebuilds the same screen from durable rows: nothing was kept in the page.
        page.reload()
        page.wait_for_selector(".register-canary", timeout=15_000)
        assert page.locator(".register-scope").first.get_attribute("data-scope-state") == "PAUSED"
        assert writes == []
    assert (
        container.registrations.execution_scope(
            MARKET, account, CREATE_ENDPOINT_GROUP
        ).resume_generation
        == 0
    )


def test_the_page_keeps_no_registration_state_of_its_own(
    browser: Browser,
    client: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    prepare(container, sources, container.registrations, account, prep)
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        stored = page.evaluate(
            "() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])"
        )
        assert json.loads(stored) == [[], []]
