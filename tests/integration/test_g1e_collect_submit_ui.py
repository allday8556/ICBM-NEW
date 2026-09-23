"""The COLLECT screen's one-product submit, in the real UI (Gate 1 G1-E, Issue #89 5794763664).

The page runs in the installed browser; every request it makes is answered in-process by the
application under test, served over the scripted shop of ``tests.collect_submit_support``. Every
request is recorded, and a request can be parked to replay a slow network. Proven here:
- a fresh screen offers the one-product form directly, and the Product DB's empty-state link lands
  on it;
- one submit — double clicks and Enter while it is on the wire included — sends exactly one POST
  and follows that run to RECORDED, its exact revision and facts status, then into the Product DB;
- a refused URL and a pacing refusal show the server's reason and no run;
- NO_REVISION and FAILED show the durable detail and are never submitted again;
- a PENDING run survives leaving the screen and a full reload: the same run is read again from
  the server, and nothing is submitted a second time or kept in browser storage;
- a RECORDED run whose Product is not yet materialized says so, and a read-only recheck follows it.

No supplier, marketplace or AI provider is reached.
"""

import contextlib
import json
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from tests.collect_submit_support import (
    FAILED_ID,
    HELD_ID,
    NO_REVISION_ID,
    SUPPLIER_KEY,
    ScriptedShop,
    gated_materialization,
    product_url,
    served,
)
from tests.conftest import LOCAL
from tests.product_support import Collections, raw

pytestmark = pytest.mark.integration

BROWSER_CHANNEL = "msedge" if sys.platform == "win32" else "chrome"
JOBS = f"{LOCAL}/#/collect?view=jobs"
RUNS = "/api/v1/collect/collections"
FORWARDED = ("x-icbm-client", "content-type", "accept")
FORM = "[data-role='collect-submit']"
FOCUS = "[data-role='run-focus']"
SESSION = "fake-session-payload"
NEVER_CREATED = (
    "pricing_snapshots",
    "registration_drafts",
    "registration_draft_items",
    "registration_preparations",
    "registration_preparation_revisions",
    "registration_snapshots",
    "registration_item_snapshots",
    "registration_batches",
    "registration_intents",
    "registration_attempts",
)


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
    """Every request the page made, and any POST parked unanswered."""

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
def _page(browser: Browser, client: TestClient, wire: Wire, url: str = JOBS) -> Iterator[Page]:
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
        page.wait_for_selector(FORM, timeout=15_000)
        yield page
    finally:
        page.close()


def _submit(page: Page, number: str) -> None:
    page.locator("#collect-url").fill(product_url(number))
    page.locator("[data-action='submit-collection']").click()


def _outcome(page: Page, outcome: str, run_id: str | None = None, timeout: float = 15_000) -> str:
    selector = f"{FOCUS}[data-outcome='{outcome}']"
    if run_id is not None:
        selector = f"{FOCUS}[data-run='{run_id}'][data-outcome='{outcome}']"
    page.wait_for_selector(selector, timeout=timeout)
    return str(page.locator(FOCUS).get_attribute("data-run"))


def _no_browser_truth(page: Page) -> None:
    """Nothing is kept in browser storage, and the route holds no product URL or session."""
    stored = page.evaluate("() => [localStorage.length, sessionStorage.length]")
    assert stored == [0, 0]
    route = unquote(urlsplit(page.url).fragment)
    assert "://" not in route and "shop.collect.invalid" not in route, route


RECHECK_LIMIT = 40
RECHECK_PAUSE_MS = 250


def _await_materialized(page: Page) -> int:
    """Follow the focused RECORDED run's handoff until its Product is MATERIALIZED, and return how
    many rechecks that took.

    The job commits RECORDED before it materializes, so NOT_YET_VISIBLE is a truthful moment and
    one click is no synchronization: each recheck is the screen's own read-only ``다시 확인``,
    repeated a bounded number of times. It never submits anything.
    """
    for attempt in range(RECHECK_LIMIT):
        handoff = page.locator(f"{FOCUS} [data-role='handoff']")
        handoff.wait_for()
        state = handoff.get_attribute("data-state")
        if state == "MATERIALIZED":
            return attempt
        assert state == "NOT_YET_VISIBLE", state
        page.locator("[data-action='recheck-product']").click()
        page.wait_for_timeout(RECHECK_PAUSE_MS)
    raise AssertionError(f"the Product was not visible after {RECHECK_LIMIT} rechecks")


def _no_absent_text(page: Page) -> None:
    """An absent part renders as nothing: never as the words ``null`` or ``undefined``."""
    for part in (FORM, FOCUS):
        text = page.locator(part).inner_text()
        assert "null" not in text and "undefined" not in text, text


def _counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


# ---------------------------------------------------------------- the screen


def test_a_fresh_screen_offers_the_one_product_form_and_the_db_link_lands_on_it(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        options = page.locator("#collect-supplier option")
        assert [options.nth(i).get_attribute("value") for i in range(options.count())] == [
            SUPPLIER_KEY
        ]
        assert page.locator("#collect-url").is_editable()
        assert page.locator("[data-action='submit-collection']").is_enabled()
        page.wait_for_selector("[data-role='recent-runs'] .table-empty")
        # The Product DB's empty state now leads here, to a form that can submit.
        page.goto(f"{LOCAL}/#/db")
        page.get_by_role("button", name="상품 수집하기").click()
        page.wait_for_selector(FORM)
        assert "view=jobs" in page.url
    assert wire.posts() == []


def test_one_submit_is_one_post_followed_to_recorded_and_into_the_product_db(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire(hold_posts=True)
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        page.locator("#collect-url").fill(product_url("4242"))
        button = page.locator("[data-action='submit-collection']")
        button.click()
        # While the POST is on the wire, more clicks and Enter send nothing more.
        page.wait_for_function(
            "() => document.querySelector(\"[data-action='submit-collection']\").disabled"
        )
        button.click(force=True)
        page.locator("#collect-url").press("Enter")
        # A submit that reaches the form some other way while the first is pending is dropped too.
        page.evaluate(f'() => document.querySelector("{FORM}").requestSubmit()')
        page.wait_for_timeout(300)
        assert len(wire.parked) == 1 and len(wire.posts()) == 1
        method, url, body = wire.posts()[0]
        assert (method, urlsplit(url).path) == ("POST", RUNS)
        assert json.loads(body or b"{}") == {
            "supplier_key": SUPPLIER_KEY,
            "product_url": product_url("4242"),
        }
        wire.hold_posts = False
        _fulfill(wire.parked[0], client)
        run_id = _outcome(page, "RECORDED")
        assert f"run={run_id}" in page.url
        served_container: Container = client.app.state.container  # type: ignore[attr-defined]
        run = served_container.collection.run(run_id)
        assert page.locator(f"{FOCUS} [data-role='revision-id']").inner_text() == run.revision_id
        assert run.facts_status is not None
        assert (
            page.locator(f"{FOCUS} [data-facts-status]").get_attribute("data-facts-status")
            == run.facts_status.value
        )
        # The Product DB handoff, by the revision's own source identity. The run may be RECORDED
        # while its Product is still NOT_YET_VISIBLE: follow it with bounded read-only rechecks.
        _await_materialized(page)
        _no_absent_text(page)
        group = served_container.products.product_of_source(SUPPLIER_KEY, "4242").product_group_id
        page.locator("[data-action='open-product']").click()
        page.wait_for_selector(
            f"[data-role='product-detail'][data-product='{group}'][data-state='ready']"
        )
        _no_browser_truth(page)
        assert SESSION not in page.content()
    assert len(wire.posts()) == 1
    assert all(
        SESSION not in url and SESSION.encode() not in (b or b"") for _, url, b in wire.requests
    )
    counts = _counts(config)
    assert counts["collection_runs"] == 1
    assert {t: counts[t] for t in NEVER_CREATED} == dict.fromkeys(NEVER_CREATED, 0)


def test_a_refused_submit_shows_the_server_reason_and_no_run(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        page.locator("#collect-url").fill("https://elsewhere.invalid/p/1/")
        page.locator("[data-action='submit-collection']").click()
        refusal = page.locator("[data-role='submit-refusal'] [data-reason='COLLECT_URL_REFUSED']")
        refusal.wait_for()
        assert page.locator(FOCUS).is_hidden()
        assert page.locator("[data-role='recent-runs'] tr[data-run]").count() == 0
        assert page.locator("[data-action='submit-collection']").is_enabled()
        # Pacing: once the product has been read, the same request is refused by the server.
        _submit(page, "4242")
        run_id = _outcome(page, "RECORDED")
        _submit(page, "4242")
        page.wait_for_selector(
            "[data-role='submit-refusal'] [data-reason='COLLECT_SAME_PRODUCT_TOO_SOON']"
        )
        assert page.locator(FOCUS).get_attribute("data-run") == run_id
        page.wait_for_timeout(500)
        assert page.locator("[data-role='recent-runs'] tr[data-run]").count() == 1
    assert len(wire.posts()) == 3
    assert _counts(config)["collection_runs"] == 1


def test_no_revision_and_failed_show_the_durable_answer_and_are_never_resubmitted(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    shop = ScriptedShop()
    with served(config, shop) as client, _page(browser, client, wire) as page:
        _submit(page, NO_REVISION_ID)
        _outcome(page, "NO_REVISION")
        assert (
            page.locator(f"{FOCUS} [data-role='run-detail']").inner_text()
            == "the page declares no product number"
        )
        _submit(page, FAILED_ID)
        _outcome(page, "FAILED")
        assert (
            page.locator(f"{FOCUS} [data-role='run-detail']").inner_text()
            == "SUPPLIER_SESSION_EXPIRED"
        )
        assert page.locator(f"{FOCUS} [data-role='handoff']").count() == 0
        _no_absent_text(page)
        # The page keeps reading nothing new and submits nothing on its own.
        page.wait_for_timeout(3000)
        rows = page.locator("[data-role='recent-runs'] tr[data-run]")
        assert sorted(rows.nth(i).get_attribute("data-outcome") or "" for i in range(2)) == [
            "FAILED",
            "NO_REVISION",
        ]
    assert len(wire.posts()) == 2
    assert sorted(shop.reads) == [NO_REVISION_ID, FAILED_ID]
    with contextlib.closing(raw(config)) as connection:
        attempts = connection.execute(
            "SELECT attempt_count FROM jobs WHERE job_type = 'collect.product'"
        ).fetchall()
    assert sorted(a[0] for a in attempts) == [1, 1]


def test_a_pending_run_survives_leaving_and_reloading_without_a_second_submit(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    shop = ScriptedShop()
    with served(config, shop) as client, _page(browser, client, wire) as page:
        _submit(page, HELD_ID)
        run_id = _outcome(page, "PENDING")
        page.wait_for_selector(f"{FOCUS} [data-role='job-state']")
        _no_absent_text(page)
        # Leave the screen, then come back without the run in the route: the server's own list
        # names the same run, still pending.
        page.goto(f"{LOCAL}/#/dashboard")
        page.wait_for_selector("#content[data-page='dashboard']")
        page.goto(JOBS)
        page.wait_for_selector(
            f"[data-role='recent-runs'] tr[data-run='{run_id}'][data-outcome='PENDING']"
        )
        # A full reload of the run's own route reads the same durable run again.
        page.goto(f"{JOBS}&run={run_id}")
        page.reload()
        _outcome(page, "PENDING", run_id)
        _no_browser_truth(page)
        assert len(wire.posts()) == 1
        shop.release.set()
        _outcome(page, "RECORDED", run_id, timeout=20_000)
        page.wait_for_selector(
            f"[data-role='recent-runs'] tr[data-run='{run_id}'][data-outcome='RECORDED']"
        )
    assert len(wire.posts()) == 1
    assert shop.reads == [HELD_ID]
    assert _counts(config)["collection_runs"] == 1


def test_a_recorded_run_not_yet_materialized_is_shown_truthfully_and_rechecked(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client:
        served_container: Container = client.app.state.container  # type: ignore[attr-defined]
        # A run recorded with no materializer behind it: its Product does not exist yet.
        run_id, _ = Collections.of(served_container, config).collect(source_product_id="5151")
        with _page(browser, client, wire, url=f"{JOBS}&run={run_id}") as page:
            _outcome(page, "RECORDED", run_id)
            handoff = page.locator(f"{FOCUS} [data-role='handoff'][data-state='NOT_YET_VISIBLE']")
            handoff.wait_for()
            assert handoff.locator("[data-reason='NOT_YET_VISIBLE']").count() == 1
            assert handoff.locator("[data-action='open-product']").count() == 0
            _no_absent_text(page)
            before = _counts(config)
            page.locator("[data-action='recheck-product']").click()
            page.wait_for_timeout(500)
            assert _counts(config) == before, "a recheck is a read"
            served_container.materializer.materialize_run(run_id)
            page.locator("[data-action='recheck-product']").click()
            page.wait_for_selector(f"{FOCUS} [data-role='handoff'][data-state='MATERIALIZED']")
            group = served_container.products.product_of_source("kmretail", "5151")
            page.locator("[data-action='open-product']").click()
            page.wait_for_selector(
                f"[data-role='product-detail'][data-product='{group.product_group_id}']"
            )
    assert wire.posts() == []


def test_a_submitted_run_is_followed_through_its_not_yet_visible_window(
    browser: Browser, config: AppConfig
) -> None:
    """The post-merge race of `0f0502e3`, made deterministic in the screen.

    Materialization is held open after the job commits RECORDED, so the screen truthfully shows
    NOT_YET_VISIBLE and one recheck cannot be enough; it is let through a second later, while
    the bounded read-only rechecks are running.
    """
    wire = Wire()
    with gated_materialization() as gate, served(config, ScriptedShop()) as client:
        try:
            with _page(browser, client, wire) as page:
                _submit(page, "4242")
                run_id = _outcome(page, "RECORDED")
                page.wait_for_selector(
                    f"{FOCUS} [data-role='handoff'][data-state='NOT_YET_VISIBLE']"
                )
                assert page.locator(f"{FOCUS} [data-action='open-product']").count() == 0
                threading.Timer(1.0, gate.set).start()
                assert _await_materialized(page) >= 1, "the first render was NOT_YET_VISIBLE"
                served_container: Container = client.app.state.container  # type: ignore[attr-defined]
                group = served_container.products.product_of_source(SUPPLIER_KEY, "4242")
                page.locator("[data-action='open-product']").click()
                page.wait_for_selector(
                    f"[data-role='product-detail'][data-product='{group.product_group_id}']"
                )
                assert f"run={run_id}" not in page.url
        finally:
            gate.set()
    # Every recheck was a read: the one POST is the operator's submit.
    assert len(wire.posts()) == 1
