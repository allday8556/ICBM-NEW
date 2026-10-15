"""ADR-0036 O5a in the real UI: verify each sample, 「사용 시작」 and 「비활성화」 (§7).

The page runs in the installed browser; every request it makes is answered in-process by the
application under test, whose collection gateway reads a scripted storefront. The three trials
are read by the application's own job worker with the pause between reads set to zero for this
suite only (the pacing itself is proven by the O3 suites). Proven here:

- each verifiable sample offers 「값이 맞음」/「값이 틀림」; a confirmed one shows 값 확인함 and the
  progress line counts it (값 확인 n/3);
- 「사용 시작」 stays disabled until 3 distinct samples are confirmed, then asks once and moves the
  supplier to 사용 중 (ACTIVE) through the server's gate; the screen is drawn again from the server;
- the card then shows 사용 중 and 「비활성화」, which asks once and moves it to 비활성;
- exactly those writes are sent, and no supplier is read beyond the three trials;
- neither the screen nor the open panel scrolls horizontally at 1920×1080.
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
    # No pause between trial reads in this suite: three samples on the real clock.
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


def _three_samples(container: Container, shop: TrialShop) -> str:
    key = recon_site(container, CAFE24_ORIGIN, "cafe24")
    for number in (701, 702, 703):
        shop.serve(cafe24_url(number), cafe24_page(number))
        job_id = container.onboarding_analysis.submit(
            key, cafe24_url(number), actor=OPERATOR
        ).job_id
        wait_until(lambda j=job_id: container.jobs.get(j).state in ("SUCCEEDED", "DEAD"))
        assert container.jobs.get(job_id).state == "SUCCEEDED"
    return key


def _images_shown_beside_the_buttons(page: Page, client: TestClient, key: str) -> None:
    """「값이 맞음」 and 「사용 시작」 confirm the images field itself (§7): each sample beside them
    lists every image the server read — its order, role and address as an openable link — and
    not only a count (PR #369 audit, SAFETY_GATE_BYPASS)."""
    view = client.get(f"/api/v1/onboarding/suppliers/{key}/trials").json()
    assert view["samples"]
    for sample in view["samples"]:
        items = sample["summary"]["images"]["items"]
        assert items, sample["sample_url"]
        block = page.locator(f".modal [data-sample='{sample['sample_url']}']")
        listed = block.locator("[data-role='image-urls']")
        assert listed.get_attribute("data-count") == str(len(items))
        for item in items:
            row = listed.locator(f"li[data-image-rank='{item['rank']}']")
            assert row.get_attribute("data-image-role") == item["role"]
            link = row.locator("a[data-image-url]")
            assert link.get_attribute("href") == item["url"]
            assert link.get_attribute("target") == "_blank"
            assert "noopener" in (link.get_attribute("rel") or "")
            assert link.inner_text() == item["url"]
            assert link.is_visible()


def test_three_samples_are_confirmed_started_and_the_supplier_disabled(
    browser: Browser, client: TestClient, shop: TrialShop
) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    key = _three_samples(container, shop)
    reads = list(shop.requests)
    writes: list[tuple[str, str, str]] = []
    dialogs: list[str] = []
    with _page(browser, client, writes) as page:

        def accept(dialog: Dialog) -> None:
            dialogs.append(dialog.message)
            dialog.accept()

        page.on("dialog", accept)
        card = page.locator(f"[data-supplier='{key}']")
        card.locator("[data-onboarding-state='RECON']").wait_for(timeout=15_000)
        card.locator("[data-action='site-analysis']").click()
        modal = page.locator(MODAL)
        modal.locator("[data-sample] [data-action='verify-correct']").first.wait_for(timeout=15_000)
        start = modal.locator("[data-action='activate']")
        assert start.is_disabled()
        assert "값 확인 0/3" in modal.locator("[data-role='proven-count']").inner_text()
        _images_shown_beside_the_buttons(page, client, key)
        _no_horizontal_scroll(page)

        for confirmed in (1, 2, 3):
            modal.locator("[data-sample] [data-action='verify-correct']").first.click()
            page.wait_for_function(
                "(n) => document.querySelectorAll(\"[data-verdict='CORRECT']\").length === n",
                arg=confirmed,
                timeout=15_000,
            )
        assert "값 확인 3/3" in modal.locator("[data-role='proven-count']").inner_text()
        assert start.is_enabled()
        start.click()
        # The screen is drawn again from the server: the card now reads 사용 중.
        card = page.locator(f"[data-supplier='{key}']")
        card.locator("[data-onboarding-state='ACTIVE']").wait_for(timeout=15_000)
        assert container.onboarding.current(key).state.value == "ACTIVE"
        _no_horizontal_scroll(page)
        card.locator("[data-action='disable-supplier']").click()
        card = page.locator(f"[data-supplier='{key}']")
        card.locator("[data-onboarding-state='DISABLED']").wait_for(timeout=15_000)
        assert card.locator("[data-action='disable-supplier']").count() == 0

    assert container.onboarding.current(key).state.value == "DISABLED"
    assert len(dialogs) == 2 and "수집을 시작할까요" in dialogs[0] and "비활성화" in dialogs[1]
    # No supplier read beyond the three trials.
    assert shop.requests == reads
    base = f"/api/v1/onboarding/suppliers/{key}"
    paths = [(method, path) for method, path, _ in writes]
    verifications = [p for p in paths if p[1].endswith("/verification")]
    assert len(verifications) == 3 and all(m == "POST" for m, _ in verifications)
    assert paths[3:] == [("POST", f"{base}/activate"), ("POST", f"{base}/disable")]
    for _, _, body in writes[:3]:
        sent = json.loads(body)
        assert sent["verdict"] == "CORRECT" and len(sent["shown_fingerprint"]) == 64
