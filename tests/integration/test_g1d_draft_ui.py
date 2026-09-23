"""The Draft command in the real Product DB screen (Gate 1 G1-D, Issue #89 5796323069).

The page runs in the installed browser; every request is answered in-process by the application
under test, recorded, and a POST can be parked to replay a slow network. Proven here:
- the operator chooses Items, a target the server lists and a listing shape, and one click — a
  second click while the POST is pending included — sends exactly one command carrying only
  those choices; the Draft's exact pins come back and the Draft opens in Registration Management;
- a command still pending for Product A never renders under Product B, and B cannot send A's
  Items: B starts with nothing chosen and sends only its own;
- a target without a policy is offered disabled with the server's reason, and a refused command
  shows the server's reason and creates nothing;
- a reload sends nothing.
"""

import contextlib
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.collect.facts import FieldFact, QuantityTier, QuantityTiersValue
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.products.materialization import MaterializationStatus
from app.products.model import MoveReason
from tests.collect_support import confirmed
from tests.conftest import LOCAL
from tests.gate1_support import MARKET, save_policy
from tests.product_support import SUPPLIER, Collections, product, raw
from tests.register_support import establish

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
FORWARDED = ("x-icbm-client", "content-type", "accept")
DETAIL = "[data-role='product-detail']"
DRAFTS = "/api/v1/register/drafts"
CHOICES = {
    "product_group_id",
    "membership_revision_id",
    "item_ids",
    "marketplace_key",
    "marketplace_account_id",
    "listing_shape",
    "actor",
}


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


@dataclass
class Wire:
    requests: list[tuple[str, str, bytes | None]] = field(default_factory=list)
    hold_posts: bool = False
    parked: list[Route] = field(default_factory=list)

    def posts(self) -> list[tuple[str, str, bytes | None]]:
        return [r for r in self.requests if r[0] != "GET"]


def _fulfill(route: Route, client: TestClient) -> None:
    request = route.request
    parts = urlsplit(request.url)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
    response = client.request(
        request.method, path, content=request.post_data_buffer, headers=headers
    )
    route.fulfill(
        status=response.status_code, headers=dict(response.headers), body=response.content
    )


@contextmanager
def _page(browser: Browser, client: TestClient, wire: Wire, url: str) -> Iterator[Page]:
    def answer(route: Route) -> None:
        request = route.request
        wire.requests.append((request.method, request.url, request.post_data_buffer))
        if request.method == "POST" and wire.hold_posts:
            wire.parked.append(route)
            return
        _fulfill(route, client)

    page = browser.new_page()
    page.route("**/*", answer)
    try:
        page.goto(url)
        yield page
    finally:
        page.close()


class Setup:
    """A bound account, and Products materialized by M4 in the served application."""

    def __init__(self, client: TestClient, config: AppConfig) -> None:
        self.client = client
        self.container: Container = client.app.state.container  # type: ignore[attr-defined]
        self.sources = Collections.of(self.container, config)
        self.account = establish(self.container, config, MARKET, "provider-account-1")

    def product(self, source_id: str, *quantities: int) -> str:
        fields = product(quantity_tiers=_tiers(*quantities)) if quantities else product()
        run_id, _ = self.sources.collect(fields, source_product_id=source_id)
        result = self.container.materializer.materialize_run(run_id)
        assert result.status is MaterializationStatus.MATERIALIZED, result
        return str(result.product_group_id)

    def join(self, group: str, source_id: str) -> None:
        _, revision = self.sources.collect(product(), source_product_id=source_id)
        store = self.container.product_store
        uid = store.source_product(SUPPLIER, source_id).source_product_uid
        store.record_move(
            uid, revision.revision_id, reason=MoveReason.INITIAL, decided_by="t", correlation_id="c"
        )
        store.confirm_new_member(group, uid, reason="t-join", decided_by="t", correlation_id="c")


def _tiers(*quantities: int) -> FieldFact:
    return confirmed(
        QuantityTiersValue(
            tiers=tuple(
                QuantityTier(quantity=q, total_price_krw=q * 9900, label=f"{q}") for q in quantities
            )
        ),
        ".tiers",
    )


def _db(group: str) -> str:
    return f"{LOCAL}/#/db?product={group}"


def _ready(page: Page, group: str) -> None:
    page.wait_for_selector(f"{DETAIL}[data-product='{group}'][data-state='ready']", timeout=15_000)


def _choose_all(page: Page) -> None:
    for box in page.locator(f"{DETAIL} input[type='checkbox']:not([disabled])").all():
        box.check()


def _choose_target(page: Page, account: str) -> None:
    option = page.locator(f"#draft-target option[data-account='{account}']")
    option.wait_for(state="attached")
    page.select_option("#draft-target", value=option.get_attribute("value"))
    page.select_option("#draft-shape", "SINGLE_LISTING_WITH_OPTIONS")


def _drafts(config: AppConfig) -> int:
    with contextlib.closing(raw(config)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM registration_drafts").fetchone()[0])


# ---------------------------------------------------------------- the command


def test_one_click_is_one_command_and_the_draft_opens_in_registration_management(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire(hold_posts=True)
    with TestClient(create_app(config), base_url=LOCAL) as client:
        setup = Setup(client, config)
        policy_revision = save_policy(client, setup.account)
        group = setup.product("S-TIER", 1, 2)
        with _page(browser, client, wire, _db(group)) as page:
            _ready(page, group)
            _choose_all(page)
            _choose_target(page, setup.account)
            button = page.locator("[data-action='create-draft']")
            button.click()
            page.wait_for_function(
                "() => document.querySelector(\"[data-action='create-draft']\").disabled"
            )
            button.click(force=True)
            page.wait_for_timeout(300)
            assert len(wire.parked) == 1 and len(wire.posts()) == 1
            method, url, body = wire.posts()[0]
            assert (method, urlsplit(url).path) == ("POST", DRAFTS)
            sent = json.loads(body or b"{}")
            # Only the operator's choices: no price, policy revision or snapshot.
            assert set(sent) == CHOICES
            items = setup.container.products.product(group).items
            assert sent["product_group_id"] == group
            assert sent["item_ids"] == [item.item_id for item in items]
            assert sent["marketplace_account_id"] == setup.account
            wire.hold_posts = False
            _fulfill(wire.parked[0], client)
            result = page.locator("[data-role='draft-result'][data-state='created']")
            result.wait_for()
            draft_id = result.get_attribute("data-draft")
            draft = setup.container.registrations.draft(str(draft_id))
            assert draft is not None
            shown = {
                row.get_attribute("data-pin-item"): row.get_attribute("data-pricing-snapshot")
                for row in result.locator("tr[data-pin-item]").all()
            }
            assert shown == {row.item_id: row.pricing_snapshot_id for row in draft.items}
            assert policy_revision[:8] in result.inner_text()
            page.locator("[data-action='open-draft']").click()
            unit = page.locator(f".register-unit[data-draft='{draft_id}'][aria-current='true']")
            unit.wait_for(timeout=15_000)
            assert unit.get_attribute("data-preparation") == "DRAFTED"
            # A reload of either screen sends nothing.
            page.reload()
            unit.wait_for(timeout=15_000)
            page.goto(_db(group))
            _ready(page, group)
            assert page.locator("[data-role='draft-result'][data-state]").count() == 0
    assert len(wire.posts()) == 1
    assert _drafts(config) == 1


def test_a_command_pending_for_product_a_never_renders_under_product_b(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire(hold_posts=True)
    with TestClient(create_app(config), base_url=LOCAL) as client:
        setup = Setup(client, config)
        save_policy(client, setup.account)
        first = setup.product("S-A", 1, 2)
        second = setup.product("S-B")
        with _page(browser, client, wire, _db(first)) as page:
            _ready(page, first)
            _choose_all(page)
            _choose_target(page, setup.account)
            page.locator("[data-action='create-draft']").click()
            page.wait_for_timeout(300)
            assert len(wire.parked) == 1
            # The operator moves on to B while A's command is on the wire.
            page.locator(f"tr[data-product='{second}']").click()
            _ready(page, second)
            assert page.locator(f"{DETAIL} input:checked").count() == 0
            wire.hold_posts = False
            _fulfill(wire.parked[0], client)
            page.wait_for_timeout(600)
            assert page.locator(DETAIL).get_attribute("data-product") == second
            assert page.locator("[data-role='draft-result'][data-state]").count() == 0
            # B cannot send A's Items: nothing is chosen, so there is nothing to send...
            assert page.locator("[data-action='create-draft']").is_disabled()
            # ...and what B sends is B's own.
            _choose_all(page)
            _choose_target(page, setup.account)
            page.locator("[data-action='create-draft']").click()
            page.wait_for_selector("[data-role='draft-result'][data-state='created']")
    sent = [json.loads(body or b"{}") for _, _, body in wire.posts()]
    assert [s["product_group_id"] for s in sent] == [first, second]
    second_items = {i.item_id for i in setup.container.products.product(second).items}
    assert set(sent[1]["item_ids"]) <= second_items
    assert _drafts(config) == 2


def test_an_unusable_target_and_a_refused_command_create_nothing(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with TestClient(create_app(config), base_url=LOCAL) as client:
        setup = Setup(client, config)
        group = setup.product("S-TIER", 1, 2)
        with _page(browser, client, wire, _db(group)) as page:
            _ready(page, group)
            option = page.locator(f"#draft-target option[data-account='{setup.account}']")
            option.wait_for(state="attached")
            assert option.get_attribute("data-reason") == "REGISTER_TARGET_POLICY_MISSING"
            assert option.is_disabled()
        save_policy(client, setup.account)
        with _page(browser, client, wire, _db(group)) as page:
            _ready(page, group)
            _choose_all(page)
            _choose_target(page, setup.account)
            # The membership moves after the operator chose.
            setup.join(group, "S-LATE")
            page.locator("[data-action='create-draft']").click()
            refused = page.locator("[data-role='draft-result'][data-state='refused']")
            refused.wait_for()
            assert (
                refused.locator(".note[data-reason]").get_attribute("data-reason")
                == "PRODUCTS_SELECTION_MEMBERSHIP_STALE"
            )
            refused.locator("[data-action='reload-detail']").click()
            _ready(page, group)
            assert page.locator(f"{DETAIL} input:checked").count() == 0
    assert len(wire.posts()) == 1
    assert _drafts(config) == 0
