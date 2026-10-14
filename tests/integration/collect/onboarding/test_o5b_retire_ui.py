"""ADR-0036 O5b in the real UI: 「분석에서 제외」 (§7, owner amendment 6160586049).

The page runs in the installed browser against the application under test; the four trials are
read by the application's own job worker with the pause between reads set to zero for this suite
only (the pacing itself is proven by the O3 suites). Proven here: a sample with a structural
problem keeps 「사용 시작」 disabled although 3 other samples are confirmed; 「분석에서 제외」 asks
for a reason, the sample stays shown as 제외됨 with its reason and flags, and 「사용 시작」 is then
enabled; exactly the retirement and the verifications are sent, and nothing is read from the
supplier beyond the trials.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Dialog, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.stages.collect.onboarding.trials import SiteAnalysisService
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.fake_suppliers import KMRETAIL_PAGES, FakeGateway
from tests.support.onboarding_support import wait_until
from tests.support.onboarding_trial_support import (
    CAFE24_ORIGIN,
    OPERATOR,
    StubSessions,
    TrialShop,
    cafe24_page,
    cafe24_url,
    recon_site,
)

pytestmark = pytest.mark.integration

SUPPLIERS = f"{LOCAL}/#/collect?view=suppliers"
FORWARDED = ("x-icbm-client", "content-type", "accept")
MODAL = ".modal"
REASON = "이 상품만 모듈이 두 개"


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            launched = launch_browser(playwright)
        except PlaywrightError as exc:
            pytest.skip(f"no {BROWSER_CHANNEL} browser to launch here ({type(exc).__name__})")
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture
def shop() -> TrialShop:
    return TrialShop()


@pytest.fixture
def client(
    config: AppConfig, shop: TrialShop, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    # No pause between trial reads in this suite: four samples on the real clock.
    monkeypatch.setattr(SiteAnalysisService, "_intervals", staticmethod(lambda site: (60.0, 0.0)))
    app = create_app(
        config,
        supplier_gateway=FakeGateway(KMRETAIL_PAGES),
        collection_gateway=shop.gateway(),
        collection_sessions=StubSessions(),
    )
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client


@contextmanager
def _page(
    browser: Browser, client: TestClient, writes: list[tuple[str, str, str]]
) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        body = request.post_data_buffer
        if request.method != "GET":
            writes.append((request.method, parts.path, (body or b"").decode("utf-8")))
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        response = client.request(request.method, path, content=body, headers=headers)
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    page.route("**/*", answer)
    try:
        page.goto(SUPPLIERS)
        page.wait_for_selector("[data-action='add-supplier']", timeout=15_000)
        yield page
    finally:
        page.close()


def _no_horizontal_scroll(page: Page) -> None:
    overflow = page.evaluate(
        """() => {
          const doc = document.documentElement;
          const card = document.querySelector('.modal .modal-card');
          const body = document.querySelector('.modal .modal-body');
          return {
            page: doc.scrollWidth - doc.clientWidth,
            card: card ? card.scrollWidth - card.clientWidth : 0,
            body: body ? body.scrollWidth - body.clientWidth : 0,
          };
        }"""
    )
    assert overflow == {"page": 0, "card": 0, "body": 0}, overflow


def _samples(container: Container, shop: TrialShop) -> tuple[str, str]:
    """Four samples read: 901 with two product modules (structural), 902–904 readable."""
    key = recon_site(container, CAFE24_ORIGIN, "cafe24")
    for number in (901, 902, 903, 904):
        shop.serve(cafe24_url(number), cafe24_page(number, duplicate_module=number == 901))
        job_id = container.onboarding_analysis.submit(
            key, cafe24_url(number), actor=OPERATOR
        ).job_id
        wait_until(lambda j=job_id: container.jobs.get(j).state in ("SUCCEEDED", "DEAD"))
        assert container.jobs.get(job_id).state == "SUCCEEDED"
    bad = next(t for t in container.onboarding.trials(key) if t.structural).sample_url_key
    return key, bad


def test_a_structural_sample_is_retired_with_a_reason_and_the_others_start_the_site(
    browser: Browser, client: TestClient, shop: TrialShop
) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    key, bad = _samples(container, shop)
    reads = list(shop.requests)
    writes: list[tuple[str, str, str]] = []
    dialogs: list[str] = []
    with _page(browser, client, writes) as page:

        def answer(dialog: Dialog) -> None:
            dialogs.append(dialog.type)
            dialog.accept(REASON if dialog.type == "prompt" else "")

        page.on("dialog", answer)
        card = page.locator(f"[data-supplier='{key}']")
        card.locator("[data-onboarding-state='RECON']").wait_for(timeout=15_000)
        card.locator("[data-action='site-analysis']").click()
        modal = page.locator(MODAL)
        target = modal.locator(f"[data-sample='{bad}']")
        target.locator("[data-action='retire-sample']").wait_for(timeout=15_000)
        for confirmed in (1, 2, 3):
            modal.locator("[data-sample] [data-action='verify-correct']").first.click()
            page.wait_for_function(
                "(n) => document.querySelectorAll(\"[data-verdict='CORRECT']\").length === n",
                arg=confirmed,
                timeout=15_000,
            )
        start = modal.locator("[data-action='activate']")
        assert start.is_disabled()  # the structural sample is still open
        target.locator("[data-action='retire-sample']").click()
        target = modal.locator(f"[data-sample='{bad}']")
        target.locator("[data-role='retired']").wait_for(timeout=15_000)
        assert REASON in target.locator("[data-role='retired']").inner_text()
        assert target.locator("[data-structural]").count() >= 1  # still shown with its flags
        page.wait_for_function(
            "() => !document.querySelector(\"[data-action='activate']\").disabled",
            timeout=15_000,
        )
        _no_horizontal_scroll(page)

    assert dialogs == ["prompt"]
    assert shop.requests == reads
    base = f"/api/v1/onboarding/suppliers/{key}"
    retire = [w for w in writes if w[1] == f"{base}/samples/retire"]
    assert len(retire) == 1
    assert json.loads(retire[0][2]) == {"sample_url": bad, "reason": REASON}
    assert len([w for w in writes if w[1].endswith("/verification")]) == 3
    assert container.onboarding.current(key).state.value == "RECON"
