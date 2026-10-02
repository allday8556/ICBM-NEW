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
        # The panel asks the worker for its status after it paints: the id appears when that
        # answer arrives, not with the first paint.
        expect(panel.locator(_role("extension-id"))).to_have_text(extension_id, timeout=TIMEOUT_MS)
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
        expect(panel.locator(_role("icbm-state"))).to_have_attribute(
            "data-policy", policy_reference()["revision"], timeout=TIMEOUT_MS
        )
        assert _text(panel, "icbm-state") == "연결됨"
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
        # The preview is that revision as ICBM recorded it (ADR-0019 §12.1): the product it
        # names, one row per recorded field with ICBM's own status, and its image references.
        revision = app.revisions.get(run.revision_id)
        assert revision is not None
        assert _text(panel, "product-id") == PRODUCT_NUMBER
        assert _text(panel, "product-name") == "합성 샘플 상품 1kg"
        recorded = list(revision.fields.values())
        rows = panel.locator("[data-role='fields'] .field")
        assert rows.count() == len(recorded)
        statuses = [rows.nth(i).get_attribute("data-status") for i in range(rows.count())]
        assert statuses == [field.status.value for field in recorded]
        for status in {field.status.value for field in recorded}:
            card = panel.locator(f"[data-role='summary'] [data-status='{status}'] b")
            assert card.inner_text() == str(statuses.count(status))
        images = panel.locator("[data-role='images'] .image")
        assert images.count() == len(revision.images) > 0
        assert panel.locator("[data-action='open-icbm']").is_visible()
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
    # The service worker spoke to ICBM only: the policy read, one capture, the run read-back, then
    # the read-back of the revision that run names.
    paths = [path for _, path, _ in requests]
    assert f"/api/v1/collect/extension/capture-policies/{SUPPLIER}" in paths
    assert [r for r in requests if r[:2] == ("POST", "/api/v1/collect/extension/captures")] == [
        ("POST", "/api/v1/collect/extension/captures", 202)
    ]
    assert ("GET", f"/api/v1/collect/collections/{run_id}", 200) in requests
    assert ("GET", f"/api/v1/collect/revisions/{run.revision_id}", 200) in requests
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


# ---------------------------------------------------------------- the list queue (E3)

LIST_URL = "https://kmretail.co.kr/category/synthetic-list/23/"
SECRET = "synthetic-list-secret"


def _product(number: str) -> str:
    return f"https://kmretail.co.kr/product/synthetic-sample/{number}/"


LIST_PAGE = (
    "<!doctype html><html><head><title>합성 카테고리</title></head><body><ul class='prdList'>"
    "<li><a href='/product/synthetic-sample/9001/'>합성 상품 하나</a></li>"
    "<li><a href='/product/synthetic-sample/9002/'>합성 상품 둘</a></li>"
    "<li><a href='/product/synthetic-sample/9001/'>합성 상품 하나 (다시)</a></li>"
    f"<li><a href='/product/synthetic-sample/9003/?token={SECRET}#{SECRET}'>질의 링크</a></li>"
    # Links the operator cannot see are not the operator's list (a real KM list page held one).
    "<li style='display:none'><a href='/product/synthetic-sample/9004/'>숨은 상품</a></li>"
    "<li><a href='/product/synthetic-sample/9005/' style='visibility:hidden'>숨은 상품</a></li>"
    "<li style='opacity:0'><a href='/product/synthetic-sample/9006/'>투명 상품</a></li>"
    "<li style='position:absolute;left:-9999px'>"
    "<a href='/product/synthetic-sample/9007/'>화면 밖</a></li>"
    "<li><a href='/product/synthetic-sample/9008/'"
    " style='display:block;width:0;height:0;overflow:hidden'>크기 없음</a></li>"
    "<li style='width:0;height:0;overflow:hidden'>"
    "<a href='/product/synthetic-sample/9009/' style='display:block;width:200px'>잘린 상품</a></li>"
    "<li><a href='/product/synthetic-sample/9010/' style='position:absolute;width:1px;height:1px;"
    "overflow:hidden;clip:rect(0,0,0,0)'>시각적 숨김</a></li>"
    "<li style='clip-path:inset(100%)'>"
    "<a href='/product/synthetic-sample/9011/'>경로로 잘림</a></li>"
    "<li><a href='/product/synthetic-sample/9012/'>"
    "<span style='visibility:hidden'>내용만 숨김</span></a></li>"
    "<li><a href='/product/synthetic-sample/9013/'><span style='display:block;width:0;height:0;"
    "overflow:hidden'><span style='display:block;width:120px;height:20px'>안에서 잘림</span>"
    "</span></a></li>"
    "<li style='position:fixed;top:5000px'>"
    "<a href='/product/synthetic-sample/9014/'>고정돼 닿지 않음</a></li>"
    "<li><a href='/product/list.html?cate_no=23'>다음 쪽</a></li>"
    "<li><a href='https://elsewhere.invalid/product/synthetic-sample/9004/'>다른 곳</a></li>"
    "</ul></body></html>"
)


def _list_page(context: BrowserContext) -> Page:
    """A synthetic list page and its products, served by the test at the reviewed host. The
    extension navigates this same tab to each product ICBM issues."""
    products = {
        _product(number): FIXTURE.read_text("utf-8").replace(PRODUCT_NUMBER, number)
        for number in (
            "9001",
            "9002",
            "9003",
            "9004",
            "9005",
            "9006",
            "9007",
            "9008",
            "9009",
            "9010",
            "9011",
            "9012",
            "9013",
            "9014",
        )
    }

    def answer(route: Route) -> None:
        url = route.request.url
        if url == LIST_URL:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=LIST_PAGE)
        elif url in products:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=products[url])
        else:
            route.abort()

    page = context.new_page()
    page.route("**/*", answer)
    page.goto(LIST_URL)
    return page


def _paired_panel(context: BrowserContext, app: Container, origin: str) -> Page:
    extension_id = _extension_id(context)
    issued = app.extension_pairing.pair(extension_id, origin=origin)
    panel = context.new_page()
    panel.goto(f"chrome-extension://{extension_id}/sidepanel.html")
    panel.locator("#pairing-code").fill(issued.code)
    panel.locator("[data-role='pairing-form'] button[type='submit']").click()
    expect(panel.locator(_role("pairing-state"))).to_have_text("페어링됨", timeout=TIMEOUT_MS)
    return panel


def _discover(panel: Page, context: BrowserContext) -> Page:
    """Open the synthetic list page and find its products from the panel.

    Discovery runs in whichever tab the browser reports as active and loaded; on a slow runner
    the panel can be told a supplier tab is active before the list page has painted, and then
    finds nothing. That is the runner, not the extension, so the operator's own recovery is
    repeated: back to product mode, find again, at most three times.
    """
    listing = _list_page(context)
    listing.wait_for_load_state("load")
    listing.bring_to_front()
    expect(panel.locator("[data-action='discover']")).to_be_enabled(timeout=TIMEOUT_MS)
    found = panel.locator(_role("list-found"))
    for attempt in range(3):
        panel.locator("[data-action='discover']").dispatch_event("click")
        try:
            expect(found).to_have_text("3", timeout=TIMEOUT_MS // 2)
            return listing
        except AssertionError:
            if attempt == 2:
                raise
            panel.locator("[data-action='product-mode']").dispatch_event("click")
            expect(panel.locator("[data-action='discover']")).to_be_enabled(timeout=TIMEOUT_MS)
    return listing


def test_a_list_page_queue_is_read_by_read_as_icbm_issues_it(
    chromium: BrowserContext, config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO), _served(config) as (app, origin, _server):
        panel = _paired_panel(chromium, app, origin)
        listing = _discover(panel, chromium)
        # The list page's own title is shown here and never sent.
        assert _text(panel, "list-title") == "합성 카테고리"
        rows = panel.locator("[data-role='queue-rows'] .queue-row")
        assert rows.count() == 3
        panel.locator("#queue-max").fill("2")
        panel.locator("#queue-interval").select_option("10")
        panel.locator("[data-action='queue-start']").dispatch_event("click")
        expect(panel.locator(_role("queue-count"))).to_have_text("2 / 2", timeout=60_000)
        expect(panel.locator(_role("queue-status"))).to_contain_text(
            "마쳤습니다", timeout=TIMEOUT_MS
        )
        # Two ordinary extension runs, recorded as any capture is, in the order ICBM issued them.
        runs = sorted(
            (run for run in app.collection.recent_runs() if run.provenance is not None),
            key=lambda run: run.requested_at,
        )
        assert [(run.source_url, run.outcome) for run in runs] == [
            (_product("9001"), CollectionOutcome.RECORDED),
            (_product("9002"), CollectionOutcome.RECORDED),
        ]
        assert all(run.provenance.transport_kind is TransportKind.EXTENSION for run in runs)  # type: ignore[union-attr]
        # At least the declared interval apart, as ICBM issued them.
        assert (runs[1].requested_at - runs[0].requested_at).total_seconds() >= 10
        chips = [rows.nth(i).locator(".chip").inner_text() for i in range(rows.count())]
        assert chips == ["RECORDED", "RECORDED"]
        # The tab the operator opened is where each read happened.
        assert listing.url == _product("9002")
        stored = panel.evaluate(
            "async () => [Object.keys(await chrome.storage.local.get(null)),"
            " Object.keys(await chrome.storage.session.get(null))]"
        )
        assert stored == [["pairing"], []]
        requests = _requests(caplog)
        logged = "\n".join(str(vars(record)) for record in caplog.records)
    # Nothing of the list page left it: not its URL, not a query or fragment of a link.
    assert SECRET not in logged and "synthetic-list" not in logged
    paths = [path for _, path, _ in requests]
    assert ("POST", "/api/v1/collect/extension/queues", 201) in requests
    assert (
        len([r for r in requests if r[:2] == ("POST", "/api/v1/collect/extension/captures")]) == 2
    )
    assert any(path.endswith("/next") for path in paths)


def test_a_queue_without_its_bounds_is_refused_and_reads_nothing(
    chromium: BrowserContext, config: AppConfig
) -> None:
    with _served(config) as (app, origin, _server):
        panel = _paired_panel(chromium, app, origin)
        _discover(panel, chromium)
        # No number of products and no interval: ICBM refuses, and nothing falls back to a default.
        panel.locator("[data-action='queue-start']").dispatch_event("click")
        expect(panel.locator(_role("queue-status"))).to_contain_text(
            "EXTENSION_QUEUE_CAP_MISSING", timeout=TIMEOUT_MS
        )
        assert app.collection.recent_runs() == ()
        assert table_counts(config)["extension_queues"] == 0


def _start_and_close_after_one(
    chromium: BrowserContext, app: Container, origin: str
) -> tuple[Page, str]:
    """Start a two-product queue, close the panel once the first product is recorded, and open a
    new panel: the queue is still open in ICBM, and the new panel finds it there."""
    panel = _paired_panel(chromium, app, origin)
    listing = _discover(panel, chromium)
    panel.locator("#queue-max").fill("2")
    panel.locator("#queue-interval").select_option("10")
    panel.locator("[data-action='queue-start']").dispatch_event("click")
    first = panel.locator("[data-role='queue-rows'] .queue-row").nth(0).locator(".chip")
    expect(first).to_have_text("RECORDED", timeout=60_000)
    panel.close()
    extension_id = _extension_id(chromium)
    reopened = chromium.new_page()
    reopened.goto(f"chrome-extension://{extension_id}/sidepanel.html")
    expect(reopened.locator(_role("pairing-state"))).to_have_text("페어링됨", timeout=TIMEOUT_MS)
    listing.bring_to_front()
    expect(reopened.locator("[data-action='discover']")).to_be_enabled(timeout=TIMEOUT_MS)
    reopened.locator("[data-action='discover']").dispatch_event("click")
    expect(reopened.locator(_role("queue-status"))).to_contain_text(
        "열려 있는 대기열", timeout=TIMEOUT_MS
    )
    open_queue = app.extension_queues.discovery_policy(SUPPLIER).open_queue
    assert open_queue is not None
    return reopened, open_queue.queue_id


def test_a_closed_panel_finds_its_open_queue_again_and_resumes_it(
    chromium: BrowserContext, config: AppConfig
) -> None:
    with _served(config) as (app, origin, _server):
        panel, queue_id = _start_and_close_after_one(chromium, app, origin)
        # Resumed, never declared again: the same queue reads its second product and finishes.
        panel.locator("[data-action='queue-pause']").dispatch_event("click")
        expect(panel.locator(_role("queue-count"))).to_have_text("2 / 2", timeout=60_000)
        assert app.extension_queues.read(queue_id).state.value == "FINISHED"
        assert table_counts(config)["extension_queues"] == 1
        runs = [run for run in app.collection.recent_runs() if run.provenance is not None]
        assert sorted(run.source_url for run in runs) == [_product("9001"), _product("9002")]


def test_a_closed_panel_finds_its_open_queue_again_and_cancels_it(
    chromium: BrowserContext, config: AppConfig
) -> None:
    with _served(config) as (app, origin, _server):
        panel, queue_id = _start_and_close_after_one(chromium, app, origin)
        panel.locator("[data-action='queue-cancel']").dispatch_event("click")
        # The open-queue line already offers to cancel ("취소합니다"): wait for the cancelled one.
        expect(panel.locator(_role("queue-status"))).to_contain_text(
            "취소했습니다", timeout=TIMEOUT_MS
        )
        assert app.extension_queues.read(queue_id).state.value == "CANCELLED"
        # The unread product stays unread.
        runs = [run for run in app.collection.recent_runs() if run.provenance is not None]
        assert [run.source_url for run in runs] == [_product("9001")]


def _requests(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for record in caplog.records:
        if record.getMessage() == "http.request":
            values: dict[str, Any] = vars(record)
            found.append((values["method"], values["path"], values["status"]))
    return found
