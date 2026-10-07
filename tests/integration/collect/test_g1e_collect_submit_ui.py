"""The COLLECT screen's one-product submit, in the real UI (Gate 1 G1-E, Issue #89 5794763664).

The page runs in the installed browser; every request it makes is answered in-process by the
application under test, served over the scripted shop of ``tests.support.collect_submit_support``.
Every
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
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Browser, Page, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from tests.conftest import LOCAL
from tests.support.browser import BROWSER_CHANNEL, launch_browser
from tests.support.collect_submit_support import (
    FAILED_ID,
    HELD_ID,
    NO_REVISION_ID,
    SUPPLIER_KEY,
    ScriptedShop,
    gated_materialization,
    product_url,
    served,
)
from tests.support.collect_support import PNG
from tests.support.jobs_support import FakeClock
from tests.support.product_support import Collections, FakeDecoder, raw

pytestmark = pytest.mark.integration

JOBS = f"{LOCAL}/#/collect?view=jobs"
RUNS = "/api/v1/collect/collections"
FORWARDED = ("x-icbm-client", "content-type", "accept")
FORM = "[data-role='collect-submit']"
FOCUS = "[data-role='run-focus']"
FACTS = "[data-role='facts-slot'] > [data-role='collect-facts']"
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
            launched = launch_browser(playwright)
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


# ------------------------------------------------------- A-UX1: 수집 사실 and the run filters


def test_the_focused_run_shows_its_revision_fields_and_evidence_as_stored(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        _submit(page, "4242")
        run_id = _outcome(page, "RECORDED")
        facts = page.locator(f"{FACTS}[data-state='ready']")
        facts.wait_for()
        assert page.locator(f"{FOCUS} [data-role='collect-facts']").count() == 0
        served_container: Container = client.app.state.container  # type: ignore[attr-defined]
        run = served_container.collection.run(run_id)
        assert run.revision_id is not None
        revision = client.get(
            f"/api/v1/collect/revisions/{run.revision_id}", headers={"X-ICBM-Client": "pytest"}
        ).json()
        # Every field, in the revision's order, with the status the revision holds.
        rows = facts.locator("[data-role='field-summary'] tr[data-field]")
        assert [
            (rows.nth(i).get_attribute("data-field"), rows.nth(i).get_attribute("data-status"))
            for i in range(rows.count())
        ] == [(f["key"], f["status"]) for f in revision["fields"]]
        for status in ("CONFIRMED", "ABSENT", "REVIEW_REQUIRED"):
            counted = facts.locator(f"[data-count-status='{status}']").get_attribute("data-count")
            assert counted == str(sum(f["status"] == status for f in revision["fields"]))
        included = sum(i["disposition"] == "INCLUDED" for i in revision["images"])
        count = facts.locator("[data-role='image-count']")
        assert count.get_attribute("data-included") == str(included)
        assert count.get_attribute("data-total") == str(len(revision["images"]))
        # A field the source did not confirm shows no value: only its status.
        for field in revision["fields"]:
            if field["status"] != "CONFIRMED":
                value = facts.locator(f"tr[data-field='{field['key']}'] [data-role='field-value']")
                assert value.inner_text() == "—"
        # Collapsed, an absent part renders as nothing. (An opened value is the stored JSON itself,
        # where null is the value's own word for a part the source did not state.)
        _no_absent_text(page)
        # The evidence is the field's own, opened on demand.
        first = revision["fields"][0]
        evidence = facts.locator(f"tr[data-evidence-for='{first['key']}']")
        toggle = facts.locator(f"tr[data-field='{first['key']}'] [data-action='toggle-evidence']")
        assert evidence.count() == 0
        assert toggle.get_attribute("aria-expanded") == "false"
        toggle.click()
        assert evidence.count() == 1
        assert evidence.is_visible()
        assert toggle.get_attribute("aria-expanded") == "true"
        # A CONFIRMED field's whole stored value is there too, never shortened.
        if first["status"] == "CONFIRMED":
            full = evidence.locator("[data-role='field-value-full']").inner_text()
            assert json.loads(full) == json.loads(first["value_json"])
        for field in revision["fields"]:
            if field["status"] == "CONFIRMED" and field["key"] != first["key"]:
                row = facts.locator(f"tr[data-field='{field['key']}']")
                row.locator("[data-action='toggle-evidence']").click()
                shown = facts.locator(
                    f"tr[data-evidence-for='{field['key']}'] [data-role='field-value-full']"
                ).inner_text()
                assert json.loads(shown) == json.loads(field["value_json"]), field["key"]
                row.locator("[data-action='toggle-evidence']").click()
                assert facts.locator(f"tr[data-evidence-for='{field['key']}']").count() == 0
        # Inspecting other confirmed fields must not collapse or replace the first field's detail.
        assert toggle.get_attribute("aria-expanded") == "true"
        assert evidence.count() == 1
        entries = evidence.locator("[data-role='evidence'] tbody tr")
        assert [
            entries.nth(i).get_attribute("data-evidence-kind") for i in range(entries.count())
        ] == [e["kind"] for e in first["evidence"]]
        toggle.click()
        assert evidence.count() == 0
        assert toggle.get_attribute("aria-expanded") == "false"
        # Image state follows the same contract: its table is built only while expanded.
        image_table = facts.locator("[data-role='image-refs']")
        image_toggle = facts.locator("[data-action='toggle-images']")
        assert image_table.count() == 0
        if revision["images"]:
            assert image_toggle.get_attribute("aria-expanded") == "false"
            image_toggle.click()
            image_rows = image_table.locator("tbody tr")
            assert image_rows.count() == len(revision["images"])
            assert [
                image_rows.nth(i).get_attribute("data-status") for i in range(image_rows.count())
            ] == [image["status"] for image in revision["images"]]
            assert image_toggle.get_attribute("aria-expanded") == "true"
            image_toggle.click()
            assert image_table.count() == 0
            assert image_toggle.get_attribute("aria-expanded") == "false"
        else:
            assert image_toggle.count() == 0
        # Two axes, never merged: the run's outcome and the revision's facts status.
        assert (
            page.locator(f"{FOCUS} [data-role='run-outcome'] [data-outcome='RECORDED']").count()
            == 1
        )
        assert (
            page.locator(f"{FOCUS} [data-facts-status]").first.get_attribute("data-facts-status")
            == revision["facts_status"]
        )
        assert "%" not in facts.inner_text(), "no confidence number is shown"
        # v29's 수집 미리보기 beside the list holds the revision's own values; a slot the system
        # has no source for keeps its place and reads 데이터 없음 (owner decision 2026-10-07, 가).
        preview = page.locator(
            f"[data-role='run-preview'] [data-role='preview-body'][data-run='{run_id}']"
        )
        preview.wait_for()
        fields = {f["key"]: f for f in revision["fields"]}
        for label, key in {
            "도매가": "prices",
            "배송비": "shipping",
            "최저판매가": "minimum_sale_price",
            "옵션 수": "options",
        }.items():
            expected = fields[key]["status"] if key in fields else "NO_DATA"
            assert (
                preview.locator(f"[data-preview='{label}']").get_attribute("data-status")
                == expected
            )
        assert "데이터 없음" in preview.locator("[data-role='ai-note']").inner_text()
        row = page.locator(f"[data-role='recent-runs'] tr[data-run='{run_id}']")
        assert row.locator("[data-no-data]").count() == 3
        assert "%" not in preview.inner_text(), "no confidence number is shown"
        # The v29 controls with no contract yet only explain themselves: they send nothing.
        for role in ("revalidate", "ai-correct", "collect-option"):
            control = page.locator(f"[data-role='{role}']").first
            assert control.get_attribute("aria-disabled") == "true"
            control.click(force=True)
        _no_browser_truth(page)
    # Reading the revision sent nothing: the one POST is the operator's submit.
    assert len(wire.posts()) == 1


def test_the_run_list_is_filtered_on_the_server_and_counts_what_it_selected(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client:
        made = []
        for number in (FAILED_ID, "51", NO_REVISION_ID, "52"):
            response = client.post(
                RUNS,
                json={"supplier_key": SUPPLIER_KEY, "product_url": product_url(number)},
                headers={"X-ICBM-Client": "pytest"},
            )
            made.append(response.json()["collection_run_id"])
        for run_id in made:
            for _ in range(300):
                state = client.get(f"{RUNS}/{run_id}", headers={"X-ICBM-Client": "pytest"}).json()
                if state["outcome"] != "PENDING":
                    break
                threading.Event().wait(0.05)
        with _page(browser, client, wire, f"{JOBS}&filter=failed") as page:
            panel = page.locator("[data-role='recent-runs'][data-filter='failed']")
            panel.wait_for()
            page.wait_for_selector("[data-role='runs-count'][data-total='1']")
            rows = page.locator("[data-role='recent-runs'] tr[data-run]")
            assert [rows.nth(i).get_attribute("data-outcome") for i in range(rows.count())] == [
                "FAILED"
            ]
            # The server applied the filter: the page asked for it, and never for a bare page.
            reads = [u for m, u, _ in wire.requests if m == "GET" and urlsplit(u).path == RUNS]
            listed = [u for u in reads if "limit=20" in u]
            assert listed and all("outcome=FAILED" in u for u in listed)
            # Each v29 state card shows the server's own total for its filter, read as one run;
            # the page counts nothing itself.
            assert all("limit=1" in urlsplit(u).query.split("&") for u in reads if u not in listed)
            for key, total in {"all": "4", "recorded": "2", "failed": "1", "pending": "0"}.items():
                page.wait_for_selector(
                    f"[data-filter='{key}'] [data-role='filter-count'][data-total='{total}']"
                )
            card = page.locator("[data-role='run-filters'] [data-filter='failed']")
            assert card.get_attribute("aria-selected") == "true"
            page.wait_for_selector(
                "[data-filter='no_revision'] [data-role='filter-count'][data-total='1']"
            )
            # A NO_REVISION run is its own answer: no facts status, no facts block.
            page.locator("[data-filter='no_revision']").click()
            page.wait_for_selector("[data-role='recent-runs'][data-filter='no_revision']")
            page.wait_for_selector("[data-role='runs-count'][data-total='1']")
            page.locator("[data-action='open-run']").click()
            _outcome(page, "NO_REVISION")
            assert page.locator(f"{FOCUS} [data-reason='NO_REVISION']").count() == 1
            assert page.locator(FACTS).count() == 0
            assert page.locator(f"{FOCUS} [data-facts-status]").count() == 0
            # 전체 lists every run the server holds.
            page.locator("[data-filter='all']").click()
            page.wait_for_selector("[data-role='runs-count'][data-total='4']")
            _no_browser_truth(page)
    assert wire.posts() == []


# ------------------------------------------- A-UX3: recovery fills the form, never submits


def test_a_failed_run_can_be_put_back_in_the_form_and_only_the_operator_submits(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        _submit(page, FAILED_ID)
        failed_id = _outcome(page, "FAILED")
        assert page.locator("#collect-url").input_value() == ""
        page.locator(f"{FOCUS} [data-action='refill-url']").click()
        # The run's own URL and supplier are back in the form; nothing was sent.
        assert page.locator("#collect-url").input_value() == product_url(FAILED_ID)
        assert page.locator("#collect-supplier").input_value() == SUPPLIER_KEY
        page.wait_for_timeout(1000)
        assert len(wire.posts()) == 1
        # The operator's own submit goes through the one submit path and its server rules: the
        # same product read moments ago is refused by the same-product interval, so no run is made
        # and the failed run stays as recorded.
        page.locator("[data-action='submit-collection']").click()
        page.wait_for_selector(
            "[data-role='submit-refusal'] [data-reason='COLLECT_SAME_PRODUCT_TOO_SOON']"
        )
        assert len(wire.posts()) == 2
        served_container: Container = client.app.state.container  # type: ignore[attr-defined]
        assert served_container.collection.run(failed_id).outcome.value == "FAILED"
        assert [r.collection_run_id for r in served_container.collection.recent_runs()] == [
            failed_id
        ]
        _no_browser_truth(page)


def test_only_a_failed_run_offers_to_be_put_back_in_the_form(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        _submit(page, NO_REVISION_ID)
        _outcome(page, "NO_REVISION")
        assert page.locator(f"{FOCUS} [data-action='refill-url']").count() == 0
        _submit(page, "4242")
        _outcome(page, "RECORDED")
        assert page.locator(f"{FOCUS} [data-action='refill-url']").count() == 0
    assert len(wire.posts()) == 2


# ------------------------------------------------ A-UX2: up to 50 URLs, each its own single request


def test_each_url_of_the_box_is_its_own_single_url_request_and_run(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    lines = [
        product_url("61"),
        product_url("62"),
        product_url("61"),
        "not a url",
        product_url("63"),
    ]
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        page.locator("#collect-url").fill("\n".join(lines))
        summary = page.locator("[data-role='intake-summary']")
        assert (
            summary.get_attribute("data-lines"),
            summary.get_attribute("data-duplicates"),
            summary.get_attribute("data-invalid"),
            summary.get_attribute("data-planned"),
        ) == ("5", "1", "1", "3")
        assert page.locator("[data-role='invalid-lines'] li").inner_text() == "not a url"
        page.locator("[data-action='submit-collection']").click()
        page.wait_for_selector("[data-role='intake-results'] li[data-state]:nth-child(3)")
        rows = page.locator("[data-role='intake-results'] li[data-state]")
        assert [
            (rows.nth(i).get_attribute("data-url"), rows.nth(i).get_attribute("data-state"))
            for i in range(rows.count())
        ] == [
            (product_url("61"), "accepted"),
            (product_url("62"), "accepted"),
            (product_url("63"), "accepted"),
        ]
        # One request per URL, each the one-product body; the invalid line was never sent.
        bodies = [json.loads(body or b"{}") for _, _, body in wire.posts()]
        assert bodies == [
            {"supplier_key": SUPPLIER_KEY, "product_url": product_url(n)}
            for n in ("61", "62", "63")
        ]
        served_container: Container = client.app.state.container  # type: ignore[attr-defined]
        runs = {rows.nth(i).get_attribute("data-run") for i in range(rows.count())}
        assert len(runs) == 3
        assert {served_container.collection.run(r).source_url for r in runs if r} == {
            product_url(n) for n in ("61", "62", "63")
        }
        assert page.locator("#collect-url").input_value() == ""
        _no_browser_truth(page)


def test_more_than_fifty_lines_is_refused_before_anything_is_sent(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        page.locator("#collect-url").fill("\n".join(product_url(str(n)) for n in range(100, 151)))
        page.locator("[data-action='submit-collection']").click()
        page.wait_for_selector("[data-role='submit-refusal'] [data-reason='INTAKE_TOO_MANY']")
    assert wire.posts() == []


def test_a_refused_url_keeps_its_line_and_the_others_go_on(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    foreign = "https://elsewhere.invalid/product/sample/70/"
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:
        page.locator("#collect-url").fill("\n".join([foreign, product_url("71")]))
        page.locator("[data-action='submit-collection']").click()
        page.wait_for_selector("[data-role='intake-results'] li[data-state='accepted']")
        refused = page.locator("[data-role='intake-results'] li[data-state='refused']")
        assert refused.get_attribute("data-url") == foreign
        assert refused.get_attribute("data-reason") == "COLLECT_URL_REFUSED"
        # The server refused it; nothing was made for it, and its line waits for the operator.
        assert page.locator("#collect-url").input_value() == foreign
        served_container: Container = client.app.state.container  # type: ignore[attr-defined]
        assert [r.source_url for r in served_container.collection.recent_runs()] == [
            product_url("71")
        ]
    assert len(wire.posts()) == 2


# ----------------------------------------- A-NEXT1: the recorded image reasons, worded


def _ref(ordinal: int, **recorded: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "role": "DETAIL",
        "ordinal": ordinal,
        "host": "img.collect.invalid",
        "provenance": "fake.detail",
        "locator": None,
        "status": "REVIEW_REQUIRED",
        "issue": None,
        "http_etag": None,
        "http_last_modified": None,
        "asset": None,
        "source_form": "ABSOLUTE",
        "source_trimmed": False,
        "target_refusal": None,
        "certainty": "INDETERMINATE",
        "disposition": "UNRESOLVED",
        "exclusion": None,
    }
    base.update(recorded)
    return base


def test_each_image_reason_is_the_recorded_code_worded_and_nothing_more(
    browser: Browser, config: AppConfig
) -> None:
    # The page words the codes a revision holds; it never decides one. The references below are
    # put into the revision read as the server would record them, to show every kind of reason.
    crafted = [
        _ref(
            8,
            issue="FETCH_FAILED",
            target_refusal="NON_HTTPS",
            exclusion="SOURCE_AUTHORED_NON_HTTPS",
            certainty="DETERMINATE",
            disposition="EXCLUDED",
        ),
        _ref(9, issue="OVERSIZE"),
        _ref(10, issue="FETCH_FAILED", target_refusal="HOST_NOT_ALLOWLISTED"),
        _ref(11, issue="SOMETHING_NEW"),
    ]
    wire = Wire()
    with served(config, ScriptedShop()) as client, _page(browser, client, wire) as page:

        def revision(route: Route) -> None:
            parts = urlsplit(route.request.url)
            answer = client.get(parts.path, headers={"X-ICBM-Client": "pytest"})
            body = answer.json()
            body["images"] = [*body["images"], *crafted]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        page.route("**/api/v1/collect/revisions/**", revision)
        _submit(page, "4242")
        _outcome(page, "RECORDED")
        facts = page.locator(f"{FACTS}[data-state='ready']")
        facts.wait_for()
        image_table = facts.locator("[data-role='image-refs']")
        image_toggle = facts.locator("[data-action='toggle-images']")
        assert image_table.count() == 0
        assert image_toggle.get_attribute("aria-expanded") == "false"
        image_toggle.click()
        assert image_toggle.get_attribute("aria-expanded") == "true"
        rows = facts.locator("[data-role='image-refs'] tbody tr")
        shown = {
            rows.nth(i).locator("td").nth(1).inner_text(): (
                rows.nth(i).locator("td").nth(4).inner_text(),
                rows.nth(i).locator("[data-role='image-reason']").inner_text()
                if rows.nth(i).locator("[data-role='image-reason']").count()
                else "—",
            )
            for i in range(rows.count())
        }
        assert shown["8"] == ("제외", "원천이 http 주소로 적은 이미지 (보안 연결 아님)")
        assert shown["9"] == ("확인 필요", "파일 크기 제한 초과")
        assert shown["10"] == ("확인 필요", "가져오기 전 거절: 허용되지 않은 이미지 호스트")
        assert shown["11"] == ("확인 필요", "SOMETHING_NEW")
        # The codes stay beside the words, exactly as recorded.
        codes = facts.locator(
            "[data-role='image-refs'] tr:has-text('원천이 http') [data-role='image-reason-codes']"
        )
        assert codes.inner_text() == "SOURCE_AUTHORED_NON_HTTPS · NON_HTTPS · FETCH_FAILED"
        image_toggle.click()
        assert image_table.count() == 0
        assert image_toggle.get_attribute("aria-expanded") == "false"
    assert len(wire.posts()) == 1


# --------------------------------- A-NEXT2b: the bound Item's common-image notes, read-only


def test_the_run_shows_the_bound_items_common_image_notes_and_nothing_before_it(
    browser: Browser, config: AppConfig
) -> None:
    wire = Wire()
    with served(config, ScriptedShop()) as client:
        served_container: Container = client.app.state.container  # type: ignore[attr-defined]
        # Recorded with no materializer behind it: no Item is bound to this revision yet. The page
        # showed one description image besides its representative one.
        assets = SourceAssetStore(
            config.source_assets_dir, served_container.db, FakeDecoder(), FakeClock()
        )
        detail = ImageReference(
            role=ImageRole.DETAIL,
            ordinal=1,
            host="img.shop.example",
            provenance=".detail img:nth-of-type(1)",
            status=FieldStatus.CONFIRMED,
            sha256=assets.put(PNG + b"a-next2b-detail").sha256,
        )
        run_id, _ = Collections.of(served_container, config).collect(
            source_product_id="5252", extra_images=(detail,)
        )
        with _page(browser, client, wire, url=f"{JOBS}&run={run_id}") as page:
            _outcome(page, "RECORDED", run_id)
            summary = page.locator("[data-role='common-image-summary']")
            summary.wait_for()
            assert summary.get_attribute("data-selection") == "NOT_MATERIALIZED"
            page.locator("[data-action='toggle-images']").click()
            headers = page.locator("[data-role='image-refs'] th")
            assert "자동 선택" not in [headers.nth(i).inner_text() for i in range(headers.count())]
            # Once the Product DB binds an Item to this revision, its own notes are shown.
            served_container.materializer.materialize_run(run_id)
            page.locator("[data-action='recheck-product']").click()
            page.wait_for_selector("[data-role='common-image-summary'][data-selection='READY']")
            run = served_container.collection.run(run_id)
            group = served_container.products.product_of_source("kmretail", "5252")
            item = next(
                i
                for i in client.get(
                    f"/api/v1/products/{group.product_group_id}",
                    headers={"X-ICBM-Client": "pytest"},
                ).json()["items"]
                if (i["current_binding"] or {}).get("provenance_revision_id") == run.revision_id
            )
            candidates = f"/api/v1/products/items/{item['item_id']}/image-candidates"
            preview = client.get(candidates, headers={"X-ICBM-Client": "pytest"}).json()
            # This synthetic page's role rules are not KM's, so the owner's rule makes no
            # selection; the page says exactly that, with the owner's own code.
            assert preview["source_revision_id"] == run.revision_id
            assert preview["auto_selection"]["blocked"] == "ROLE_RULE_UNKNOWN"
            summary = page.locator("[data-role='common-image-summary']")
            assert "ROLE_RULE_UNKNOWN" in summary.inner_text()

            # The notes an Item would carry are put into the owner's preview as the owner records
            # them, to show how each is worded; the page decides none of them.
            revision = client.get(
                f"/api/v1/collect/revisions/{run.revision_id}", headers={"X-ICBM-Client": "pytest"}
            ).json()
            keys = [(i["role"], i["ordinal"]) for i in revision["images"]]
            notes = {
                keys[0]: "REPRESENTATIVE",
                (detail.role.value, detail.ordinal): "COMMON_IMAGE_BLOCKED",
            }

            def crafted(route: Route) -> None:
                body = client.get(candidates, headers={"X-ICBM-Client": "pytest"}).json()
                body["auto_selection"] = {
                    "blocked": None,
                    "detail": None,
                    "outputs": [],
                    "notes": [
                        {"source_role": role, "source_ordinal": ordinal, "note": note}
                        for (role, ordinal), note in notes.items()
                    ],
                }
                route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

            page.route(f"**{candidates}", crafted)
            page.reload()
            page.wait_for_selector("[data-role='common-image-summary'][data-blocked='1']")
            assert (
                page.locator("[data-role='common-image-summary']").get_attribute("data-undecided")
                == "0"
            )
            page.locator("[data-action='toggle-images']").click()
            blocked = page.locator("[data-role='image-refs'] td[data-note='COMMON_IMAGE_BLOCKED']")
            assert blocked.inner_text() == "공통 이미지(차단)라 제외"
            assert blocked.get_attribute("data-common") == "true"
            first = page.locator("[data-role='image-refs'] td[data-note='REPRESENTATIVE']")
            assert first.inner_text() == "대표 이미지"
            _no_browser_truth(page)
    assert wire.posts() == []
