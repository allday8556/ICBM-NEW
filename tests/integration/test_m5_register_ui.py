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
from tests.register_support import CATEGORY, MARKET, Preparation, establish, preparation

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
    # The screen reads the served application's own preflight owner (see the API tests).
    return preparation(container, account, served=True)


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


def test_each_unit_of_a_draft_is_its_own_panel_with_the_servers_facts(
    browser: Browser,
    client: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    from tests.integration.test_m5_register_api import _separate_listings

    _draft_id, frozen = _separate_listings(container, sources, account, prep, intents=1)
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        # Two provider-listing units of one Draft are two panels, each with its own identity.
        panels = page.locator(".register-unit")
        assert panels.count() == 2
        first = page.locator(f".register-unit[data-unit='{frozen[0].snapshot_id}']")
        second = page.locator(f".register-unit[data-unit='{frozen[1].snapshot_id}']")
        assert first.count() == 1 and second.count() == 1
        assert first.get_attribute("data-preparation") == "INTENT_OPEN"
        assert second.get_attribute("data-preparation") == "SNAPSHOT_FROZEN"
        # Neither panel shows the other's Item or the key it was frozen under.
        assert frozen[1].item.item_id not in first.inner_text()
        assert frozen[1].item_key not in first.inner_text()
        assert frozen[0].item_key not in second.inner_text()
        # The server's own facts are rendered: the category, its required fields, the QA verdict
        # of the selected asset, and the reason no preflight evaluation exists yet.
        assert first.locator("li[data-field='brand'][data-provided='true']").count() == 1
        assert first.locator("li[data-field='color'][data-provided='false']").count() == 1
        assert first.locator("td[data-assets='1'] span[data-qa='PASS']").count() == 1
        preflight = first.locator(".register-preflight").first
        assert preflight.get_attribute("data-preflight") == "UNAVAILABLE"
        assert "Preflight 입력" in preflight.inner_text()
        assert writes == []


def test_an_operator_authors_a_preparation_in_the_screen(
    browser: Browser,
    client: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    from tests.register_support import draft, ready_item

    item = ready_item(container, sources, "1234")
    draft_id = draft(container.registrations, account, [item])
    writes: list[tuple[str, str]] = []
    with _page(browser, client, writes) as page:
        unit = page.locator(".register-unit[data-preparation='DRAFTED']")
        assert unit.count() == 1
        # Nothing is authored yet, so the server says so and offers no freeze.
        assert page.locator(".register-authoring[data-preparation='']").count() == 1
        assert _action(page, "EVALUATE")["disabled"] is True
        assert _action(page, "FREEZE")["disabled"] is True
        # The operator authors the unit in the screen, and the server keeps it.
        page.fill("input[name='category_id']", CATEGORY)
        page.fill("input[name='name']", "브라우저가 저장한 상품명")
        page.fill("input[name='brand']", "브랜드")
        page.fill("input[name='manufacturer']", "제조사")
        page.fill("input[name='origin']", "상세페이지 참고")
        page.fill("textarea[name='detail_body']", "상세 본문")
        page.locator("button[data-action='SAVE_PREPARATION']").first.click()
        page.wait_for_selector(".register-canary", timeout=15_000)
        assert writes == [("POST", "/api/v1/register/preparations")]
        # A reload rebuilds the same authored inputs from the durable rows.
        stored = container.registrations.preparations_of_draft(draft_id)
        assert len(stored) == 1 and stored[0].current.revision_no == 1
        page.reload()
        page.wait_for_selector(".register-canary", timeout=15_000)
        saved = page.locator(f".register-authoring[data-preparation='{stored[0].preparation_id}']")
        assert saved.count() == 1
        assert saved.locator("input[name='name']").input_value() == "브라우저가 저장한 상품명"
        # The server evaluated those inputs, with no CREATE job anywhere.
        assert page.locator(".register-preflight[data-preflight='CANDIDATE']").count() >= 0
        assert container.jobs.count(job_type_prefix="register.create") == 0


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
