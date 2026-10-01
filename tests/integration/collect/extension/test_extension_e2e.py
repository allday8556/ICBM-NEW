"""The extension itself, loaded unpacked into a real Chromium, against the real application
(ADR-0019 E1; ruling 5906290729 B-8).

One click, end to end: the side panel, the service worker, the injected cut, the paired loopback
ingest, the canonical run and its read-back. The application is served on a real loopback socket,
because an extension service worker talks to a real origin. The supplier is synthetic: the product
page is answered by the test, and the browser resolves no host name at all, so nothing can leave
this machine.

Loading an unpacked extension needs a Chromium that still accepts it from the command line. Where
no such browser is installed the tests are skipped, and say why.
"""

import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import BrowserContext, Page, Route, Worker, expect, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.stages.collect.models import CollectionOutcome, TransportKind
from automation.acceptance.m3.rehearsal.fake_shop import FakeGateway, StubSessions
from tests.support.browser import BROWSER_CHANNEL, launch_extension_context
from tests.support.extension_support import (
    EXTENSION_ROOT,
    FIXTURE,
    PRODUCT_NUMBER,
    PRODUCT_URL,
    REVISION_TABLES,
    SUPPLIER,
    policy_reference,
    table_counts,
    untouched,
)

pytestmark = pytest.mark.integration

CAPTURE = "[data-action='capture']"
TIMEOUT_MS = 20_000


def _role(name: str) -> str:
    return f"[data-role='{name}']"


@contextlib.contextmanager
def _served(config: AppConfig) -> Iterator[tuple[Container, str, uvicorn.Server]]:
    """The real application on a real loopback socket, on a port the system chose.

    Its collection gateway is a script: the server fetches a recorded run's images through it
    (ADR-0019 §7), and nothing in a test may reach a supplier.
    """
    app = create_app(
        config,
        collection_gateway=FakeGateway(documents=[]),
        collection_sessions=StubSessions(),
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the application did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield app.state.container, f"http://127.0.0.1:{port}", server
    finally:
        server.should_exit = True
        thread.join(timeout=20)


@pytest.fixture
def chromium(tmp_path: Path) -> Iterator[BrowserContext]:
    """A Chromium with the unpacked extension loaded, and no way off this machine.

    The system browser is tried first, then the Chromium Playwright bundles. A browser that will
    not load an unpacked extension — some branded builds refuse the switch — starts no service
    worker, and the test is skipped with that reason rather than passed.
    """
    with sync_playwright() as playwright:
        reasons: list[str] = []
        for label, channel in (
            (BROWSER_CHANNEL, BROWSER_CHANNEL),
            ("bundled chromium", "chromium"),
        ):
            try:
                context = launch_extension_context(
                    playwright,
                    tmp_path / label.replace(" ", "-"),
                    extension_root=EXTENSION_ROOT,
                    channel=channel,
                )
            except PlaywrightError as exc:
                reasons.append(f"{label}: not launched ({type(exc).__name__})")
                continue
            if _service_worker(context) is None:
                context.close()
                reasons.append(f"{label}: did not load the unpacked extension")
                continue
            try:
                yield context
            finally:
                context.close()
            return
        pytest.skip("no browser here loads an unpacked extension — " + "; ".join(reasons))


def _service_worker(context: BrowserContext) -> Worker | None:
    if context.service_workers:
        return context.service_workers[0]
    try:
        worker: Worker = context.wait_for_event("serviceworker", timeout=10_000)
        return worker
    except PlaywrightError:
        return None


def _extension_id(context: BrowserContext) -> str:
    worker = _service_worker(context)
    assert worker is not None
    return worker.url.split("/")[2]


def _supplier_page(context: BrowserContext) -> Page:
    """The synthetic product page, served by the test at the reviewed supplier host."""

    def answer(route: Route) -> None:
        if route.request.url == PRODUCT_URL:
            route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=FIXTURE.read_text("utf-8")
            )
        else:
            route.abort()

    page = context.new_page()
    page.route("**/*", answer)
    page.goto(PRODUCT_URL)
    return page


def _text(panel: Page, role: str) -> str:
    return panel.locator(_role(role)).inner_text()


def test_one_click_captures_the_page_and_reads_the_canonical_run_back(
    chromium: BrowserContext, config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    extension_id = _extension_id(chromium)
    with caplog.at_level(logging.INFO), _served(config) as (app, origin, _server):
        before = table_counts(config)
        # The operator pairs from the CLI's one-time code, pasted into the side panel.
        issued = app.extension_pairing.pair(extension_id, origin=origin)
        panel = chromium.new_page()
        panel.goto(f"chrome-extension://{extension_id}/sidepanel.html")
        assert _text(panel, "extension-id") == extension_id
        panel.locator("#pairing-code").fill(issued.code)
        panel.locator("[data-role='pairing-form'] button[type='submit']").click()
        expect(panel.locator(_role("pairing-state"))).to_have_text("페어링됨", timeout=TIMEOUT_MS)
        # The code is a secret: the field does not keep it.
        assert panel.locator("#pairing-code").input_value() == ""
        # The operator has the product page open, and clicks once.
        product = _supplier_page(chromium)
        product.bring_to_front()
        expect(panel.locator(CAPTURE)).to_be_enabled(timeout=TIMEOUT_MS)
        assert SUPPLIER in _text(panel, "supplier-state")
        # A tab change asks ICBM nothing; the operator's own check does, once.
        policy_reads = len(_requests(caplog))
        assert _text(panel, "icbm-state") == "확인 전"
        panel.locator("[data-action='check']").dispatch_event("click")
        expect(panel.locator(_role("icbm-state"))).to_contain_text(
            policy_reference()["revision"], timeout=TIMEOUT_MS
        )
        assert len(_requests(caplog)) == policy_reads + 1
        panel.locator(CAPTURE).dispatch_event("click")
        expect(panel.locator(_role("run-outcome"))).not_to_have_text("—", timeout=TIMEOUT_MS)
        # What the side panel shows is the canonical run ICBM read back: the outcome on its own
        # line, the code on its own line, and a transport state that is neither.
        assert _text(panel, "run-outcome") == "RECORDED"
        assert _text(panel, "run-code") == "—"
        assert _text(panel, "transport-state") == "ICBM에 전달됨"
        run_id = _text(panel, "run-id")
        run = app.collection.run(run_id)
        assert (run.outcome, run.detail) == (CollectionOutcome.RECORDED, None)
        assert run.revision_id is not None
        assert run.provenance is not None
        assert run.provenance.transport_kind is TransportKind.EXTENSION
        assert run.provenance.capture_policy_digest == policy_reference()["digest"]
        assert (run.source_url, run.source_product_id) == (PRODUCT_URL, PRODUCT_NUMBER)
        # Exactly one run and its one revision, recorded as any collection is.
        after = table_counts(config)
        assert after["collection_runs"] == before["collection_runs"] + 1
        assert after["product_facts_revisions"] == before["product_facts_revisions"] + 1
        assert set(untouched(before, after)) >= REVISION_TABLES - {"source_assets"}
        assert after["adaptive_shadow_records"] == before["adaptive_shadow_records"]
        # The extension keeps its pairing and nothing else: no page material, policy or result.
        stored = panel.evaluate(
            "async () => [Object.keys(await chrome.storage.local.get(null)),"
            " Object.keys(await chrome.storage.session.get(null))]"
        )
        assert stored == [["pairing"], []]
        # The page was never modified, and no anchor of the layout was sent: the server's final
        # scan accepted the capture, which it refuses for that anchor's text.
        assert "help@synthetic.invalid" in product.content()
        requests = _requests(caplog)
    # The service worker spoke to ICBM only: the policy read, one capture, then the run read-back.
    paths = [path for _, path, _ in requests]
    assert f"/api/v1/collect/extension/capture-policies/{SUPPLIER}" in paths
    assert [r for r in requests if r[:2] == ("POST", "/api/v1/collect/extension/captures")] == [
        ("POST", "/api/v1/collect/extension/captures", 202)
    ]
    assert ("GET", f"/api/v1/collect/collections/{run_id}", 200) in requests
    # Whatever preflight Chromium chose to send was answered for exactly these paths and nowhere
    # else; a refused one would be a 403 here.
    assert all(status == 204 for method, _, status in requests if method == "OPTIONS")


def test_a_disconnected_icbm_refuses_and_the_extension_keeps_nothing(
    chromium: BrowserContext, config: AppConfig
) -> None:
    extension_id = _extension_id(chromium)
    with _served(config) as (app, origin, _server):
        issued = app.extension_pairing.pair(extension_id, origin=origin)
        panel = chromium.new_page()
        panel.goto(f"chrome-extension://{extension_id}/sidepanel.html")
        panel.locator("#pairing-code").fill(issued.code)
        panel.locator("[data-role='pairing-form'] button[type='submit']").click()
        expect(panel.locator(_role("pairing-state"))).to_have_text("페어링됨", timeout=TIMEOUT_MS)
        product = _supplier_page(chromium)
        product.bring_to_front()
        expect(panel.locator(CAPTURE)).to_be_enabled(timeout=TIMEOUT_MS)
    # ICBM is gone. The click reads and keeps nothing: the policy cannot be fetched, so the page
    # is never cut, and there is no local copy to retry from (ADR-0019 §12.6).
    panel.locator(CAPTURE).dispatch_event("click")
    expect(panel.locator(_role("transport-state"))).to_have_attribute(
        "data-state", "REFUSED_DISCONNECTED", timeout=TIMEOUT_MS
    )
    assert _text(panel, "run-outcome") == "—"
    assert _text(panel, "run-code") == "ICBM_DISCONNECTED"
    stored = panel.evaluate(
        "async () => [Object.keys(await chrome.storage.local.get(null)),"
        " Object.keys(await chrome.storage.session.get(null))]"
    )
    assert stored == [["pairing"], []]


def test_a_page_of_another_host_offers_no_capture(
    chromium: BrowserContext, config: AppConfig
) -> None:
    extension_id = _extension_id(chromium)
    with _served(config) as (app, origin, _server):
        issued = app.extension_pairing.pair(extension_id, origin=origin)
        panel = chromium.new_page()
        panel.goto(f"chrome-extension://{extension_id}/sidepanel.html")
        panel.locator("#pairing-code").fill(issued.code)
        panel.locator("[data-role='pairing-form'] button[type='submit']").click()
        expect(panel.locator(_role("pairing-state"))).to_have_text("페어링됨", timeout=TIMEOUT_MS)
        other = chromium.new_page()
        other.route(
            "**/*",
            lambda route: route.fulfill(status=200, content_type="text/html", body="<html></html>"),
        )
        other.goto("https://shop.other.invalid/product/sample/1/")
        other.bring_to_front()
        expect(panel.locator(_role("supplier-state"))).to_contain_text("아님", timeout=TIMEOUT_MS)
        assert panel.locator(CAPTURE).is_disabled()
        assert app.collection.recent_runs() == ()


def _requests(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for record in caplog.records:
        if record.getMessage() == "http.request":
            values: dict[str, Any] = vars(record)
            found.append((values["method"], values["path"], values["status"]))
    return found
