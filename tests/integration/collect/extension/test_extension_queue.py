"""The extension list queue (ADR-0019 §8.1, AC-27 to AC-31; E3).

These run the owners directly, with a clock they control and no worker, so every interval and
expiry happens exactly when the test says. The collection gateway is a script that sends nothing:
the server reads no product page, and every queued product is captured by the "browser" here.
"""

import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container, build_container
from app.platform.core.errors import AppError
from app.platform.core.ownership import acquire_data_dir
from app.platform.core.secrets import MemorySecretStore
from app.stages.collect.extension.capture import CaptureEnvelope
from app.stages.collect.extension.queue import (
    EXTENSION_QUEUE_CAPTURE_REFUSED,
    EXTENSION_QUEUE_RUN_FAILED,
    EXTENSION_QUEUE_TICKET_EXPIRED,
    ExtensionQueues,
    QueueAnswer,
    QueueDeclaration,
)
from app.stages.collect.models import CollectionOutcome, QueueItemState, QueueState
from automation.acceptance.m3.rehearsal.fake_shop import FakeGateway, StubSessions
from integrations.suppliers.kmretail.collection import (
    QUEUE_ISSUE_TTL_S,
    QUEUE_MAX_DISCOVERED_LINKS,
    QUEUE_MAX_PRODUCTS,
    QUEUE_MIN_INTERVAL_S,
)
from tests.support.extension_support import (
    BODY,
    HEAD,
    PRODUCT_NUMBER,
    SUPPLIER,
    envelope,
    frame,
    pair,
    signed,
    transport,
    wait_for_outcome,
)
from tests.support.jobs_support import TEST_JOBS, FakeClock
from tests.support.shadow_support import rows

pytestmark = pytest.mark.integration

QUEUES = "/api/v1/collect/extension/queues"
QUEUE_POLICY = f"/api/v1/collect/extension/queue-policies/{SUPPLIER}"
CAPTURES = "/api/v1/collect/extension/captures"


def _url(number: str) -> str:
    return f"https://kmretail.co.kr/product/synthetic-sample/{number}/"


def _page(number: str) -> str:
    return frame(
        head=HEAD.replace(PRODUCT_NUMBER, number), body=BODY.replace(PRODUCT_NUMBER, number)
    )


def _declare(
    app: Container,
    links: list[str],
    *,
    max_products: int | None = 3,
    interval_s: float | None = QUEUE_MIN_INTERVAL_S,
    skip_collected: bool = False,
) -> Any:
    return app.extension_queues.create(
        QueueDeclaration(
            supplier_key=SUPPLIER,
            links=tuple(links),
            max_products=max_products,
            interval_s=interval_s,
            skip_collected=skip_collected,
        )
    )


def _capture(app: Container, number: str, ticket: str | None, *, html: str | None = None) -> str:
    url = _url(number)
    accepted = app.extension_capture.ingest(
        CaptureEnvelope.model_validate(
            envelope(
                _page(number) if html is None else html,
                transport=transport(url=url, navigation_name=url),
                queue_ticket=ticket,
            )
        )
    )
    return accepted.collection_run_id


def _issue(app: Container, queue_id: str) -> QueueAnswer:
    answer = app.extension_queues.next(queue_id)
    assert answer.kind == "ISSUE", answer
    assert answer.item is not None and answer.ticket is not None
    return answer


def _count(config: AppConfig, table: str) -> int:
    return int(rows(config, f"SELECT COUNT(*) FROM {table}")[0][0])


def _refused(call: Any, code: str) -> None:
    with pytest.raises(AppError) as refused:
        call()
    assert refused.value.code == code


# ---------------------------------------------------------------- the declared bounds (AC-28)


def test_a_supplier_that_declares_no_queue_limits_has_no_queue(
    container: Container, config: AppConfig
) -> None:
    registered = container.extension_queues._collections[SUPPLIER]
    profile = replace(registered.collection.profile, queue_limits=None)
    undeclared = ExtensionQueues(
        db=container.db,
        clock=container.clock,
        runs=container.extension_queues._runs,
        collections=[
            replace(registered, collection=replace(registered.collection, profile=profile))
        ],
    )
    _refused(lambda: undeclared.discovery_policy(SUPPLIER), "EXTENSION_QUEUE_NOT_DECLARED")
    _refused(
        lambda: undeclared.create(
            QueueDeclaration(SUPPLIER, (_url("9001"),), 1, QUEUE_MIN_INTERVAL_S, False)
        ),
        "EXTENSION_QUEUE_NOT_DECLARED",
    )
    assert _count(config, "extension_queues") == 0


def test_the_discovery_policy_is_the_reviewed_product_path_and_the_declared_limits(
    container: Container,
) -> None:
    policy = container.extension_queues.discovery_policy(SUPPLIER)
    profile = container.extension_queues._collections[SUPPLIER].collection.profile
    assert policy.product_path == profile.product_path
    assert policy.storefront_host == "kmretail.co.kr"
    assert (policy.max_discovered_links, policy.max_queue_products) == (
        QUEUE_MAX_DISCOVERED_LINKS,
        QUEUE_MAX_PRODUCTS,
    )
    assert policy.min_queue_interval_s == QUEUE_MIN_INTERVAL_S


@pytest.mark.parametrize(
    ("max_products", "interval_s", "code"),
    [
        (None, QUEUE_MIN_INTERVAL_S, "EXTENSION_QUEUE_CAP_MISSING"),
        (3, None, "EXTENSION_QUEUE_CAP_MISSING"),
        (0, QUEUE_MIN_INTERVAL_S, "EXTENSION_QUEUE_CAP_OUT_OF_RANGE"),
        (QUEUE_MAX_PRODUCTS + 1, QUEUE_MIN_INTERVAL_S, "EXTENSION_QUEUE_CAP_OUT_OF_RANGE"),
        (3, QUEUE_MIN_INTERVAL_S - 0.1, "EXTENSION_QUEUE_CAP_OUT_OF_RANGE"),
    ],
)
def test_a_missing_or_out_of_range_operator_bound_refuses_before_the_queue_exists(
    container: Container,
    config: AppConfig,
    max_products: int | None,
    interval_s: float | None,
    code: str,
) -> None:
    _refused(
        lambda: _declare(
            container, [_url("9001")], max_products=max_products, interval_s=interval_s
        ),
        code,
    )
    assert _count(config, "extension_queues") == _count(config, "extension_queue_items") == 0


def test_too_many_links_refuse_the_whole_declaration(
    container: Container, config: AppConfig
) -> None:
    links = [_url(str(9000 + n)) for n in range(QUEUE_MAX_DISCOVERED_LINKS + 1)]
    _refused(lambda: _declare(container, links), "EXTENSION_QUEUE_TOO_MANY_LINKS")
    assert _count(config, "extension_queues") == 0


# ---------------------------------------------------------------- discovery is judged (AC-27)


def test_the_server_judges_every_link_and_keeps_none_it_refuses(
    container: Container, config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    refused = [
        "https://kmretail.co.kr/product/list.html",
        "https://kmretail.co.kr/product/synthetic-sample/9002/?cate_no=1",
        "https://kmretail.co.kr/product/synthetic-sample/9002/#reviews",
        "https://elsewhere.invalid/product/synthetic-sample/9002/",
        "http://kmretail.co.kr/product/synthetic-sample/9002/",
        "https://user:synthetic-pass@kmretail.co.kr/product/synthetic-sample/9002/",
        "https://kmretail.co.kr/product/token=synthetic-value/9002/",
        "https://kmretail.co.kr/product/session%3Dsynthetic-value/9002/",
        "not a url",
        "http://[::1",
        "https://kmretail.co.kr/product/" + "a" * 2048 + "/9002/",
    ]
    links = [
        _url("9001"),
        *refused,
        # The same product under another accepted spelling is a duplicate, not a second read.
        "https://kmretail.co.kr/product/synthetic-sample/9001/category/23/display/1/",
        _url("9002"),
    ]
    with caplog.at_level(logging.INFO):
        created = _declare(container, links)
    assert (created.count.submitted, created.count.refused, created.count.duplicates) == (
        len(links),
        len(refused),
        1,
    )
    assert [item.source_url for item in created.view.items] == [_url("9001"), _url("9002")]
    assert all(item.state is QueueItemState.WAITING for item in created.view.items)
    stored = " ".join(str(row) for row in rows(config, "SELECT * FROM extension_queue_items"))
    logged = "\n".join(json.dumps(vars(r), default=str) for r in caplog.records)
    for secret in ("synthetic-value", "synthetic-pass", "cate_no", "reviews", "elsewhere"):
        assert secret not in stored and secret not in logged


def test_a_queue_holds_at_most_its_declared_products(container: Container) -> None:
    links = [_url(str(9001 + n)) for n in range(5)]
    created = _declare(container, links, max_products=2)
    assert [item.source_url for item in created.view.items] == links[:2]
    assert (created.count.queued, created.count.beyond_cap) == (2, 3)


def test_no_acceptable_link_opens_no_queue(container: Container, config: AppConfig) -> None:
    _refused(
        lambda: _declare(container, ["https://kmretail.co.kr/product/list.html"]),
        "EXTENSION_QUEUE_NO_PRODUCTS",
    )
    assert _count(config, "extension_queues") == 0


# ---------------------------------------------------------------- server-issued reads (AC-29)


def test_every_read_is_issued_once_durably_and_paced_by_the_server(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    queue_id = _declare(container, [_url("9001"), _url("9002")]).view.queue_id
    first = _issue(container, queue_id)
    assert first.item is not None and first.ticket is not None
    assert first.item.source_url == _url("9001") and first.expires_in_s == QUEUE_ISSUE_TTL_S
    # Written before the answer: the item is ISSUED and holds the ticket's digest, never the ticket.
    stored = rows(
        config,
        "SELECT state, ticket_sha256, issued_at FROM extension_queue_items WHERE item_id = ?",
        first.item.item_id,
    )[0]
    assert stored[0] == "ISSUED" and stored[1] != first.ticket and len(stored[1]) == 64
    assert stored[2] is not None
    # One issued, unsettled read at a time: the next answer is a wait, never a second issue.
    assert container.extension_queues.next(queue_id).kind == "WAIT"
    run_id = _capture(container, "9001", first.ticket)
    assert container.extension_queues.next(queue_id).kind == "WAIT"
    container.runner.run_next()
    assert container.collection.run(run_id).outcome is CollectionOutcome.RECORDED
    # Settled; the queue interval still owes time, counted by the server's clock.
    waiting = container.extension_queues.next(queue_id)
    assert waiting.kind == "WAIT" and waiting.wait_s == pytest.approx(QUEUE_MIN_INTERVAL_S)
    clock.advance(QUEUE_MIN_INTERVAL_S)
    second = _issue(container, queue_id)
    assert second.item is not None and second.item.source_url == _url("9002")
    view = container.extension_queues.read(queue_id)
    assert [item.state for item in view.items] == [QueueItemState.CAPTURED, QueueItemState.ISSUED]
    # The item state and the run outcome are separate axes (AC-31).
    assert view.items[0].collection_run_id == run_id
    assert view.items[0].run_outcome is CollectionOutcome.RECORDED
    _capture(container, "9002", second.ticket)
    container.runner.run_next()
    clock.advance(QUEUE_MIN_INTERVAL_S)
    done = container.extension_queues.next(queue_id)
    assert (done.kind, done.queue_state) == ("DONE", QueueState.FINISHED)


def test_a_ticket_claims_once_and_an_unknown_ticket_changes_nothing(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    queue_id = _declare(container, [_url("9001"), _url("9002")]).view.queue_id
    issued = _issue(container, queue_id)
    runs_before = _count(config, "collection_runs")
    # A ticket that was never issued answers no read: refused, and the issued read stays open.
    _refused(lambda: _capture(container, "9001", "A" * 43), "EXTENSION_QUEUE_TICKET_REFUSED")
    assert _count(config, "collection_runs") == runs_before
    view = container.extension_queues.read(queue_id)
    assert view.state is QueueState.OPEN and view.items[0].state is QueueItemState.ISSUED
    _capture(container, "9001", issued.ticket)
    container.runner.run_next()
    clock.advance(QUEUE_MIN_INTERVAL_S)
    # Used once: the same ticket never claims again, and its captured item stays captured.
    _refused(lambda: _capture(container, "9001", issued.ticket), "EXTENSION_QUEUE_TICKET_REFUSED")
    assert container.extension_queues.read(queue_id).items[0].state is QueueItemState.CAPTURED


@pytest.mark.parametrize(
    ("number", "changes", "code"),
    [
        # Another product's capture under this ticket.
        ("9002", {}, "EXTENSION_QUEUE_TICKET_REFUSED"),
        # The right product, cut with a policy that is no longer the reviewed one.
        ("9001", {"policy": {"revision": "stale", "digest": "0" * 64}}, "CAPTURE_POLICY_MISMATCH"),
    ],
)
def test_a_refused_capture_spends_its_read_and_stops_the_queue(
    container: Container, config: AppConfig, number: str, changes: dict[str, Any], code: str
) -> None:
    queue_id = _declare(container, [_url("9001"), _url("9002")]).view.queue_id
    issued = _issue(container, queue_id)
    runs_before = _count(config, "collection_runs")
    url = _url(number)
    refused = envelope(
        _page(number),
        transport=transport(url=url, navigation_name=url),
        queue_ticket=issued.ticket,
        **changes,
    )
    _refused(
        lambda: container.extension_capture.ingest(CaptureEnvelope.model_validate(refused)), code
    )
    assert _count(config, "collection_runs") == runs_before
    view = container.extension_queues.read(queue_id)
    assert (view.state, view.stop_reason) == (QueueState.STOPPED, EXTENSION_QUEUE_CAPTURE_REFUSED)
    # Spent and counted, never reissued, and the queue never skips forward.
    assert [item.state for item in view.items] == [QueueItemState.EXPIRED, QueueItemState.WAITING]
    _refused(lambda: _capture(container, "9001", issued.ticket), "EXTENSION_QUEUE_TICKET_REFUSED")
    assert container.extension_queues.next(queue_id).kind == "DONE"


def test_a_single_click_waits_while_a_queue_read_is_out(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    queue_id = _declare(container, [_url("9001")]).view.queue_id
    _issue(container, queue_id)
    runs_before = _count(config, "collection_runs")
    _refused(lambda: _capture(container, "9003", None), "EXTENSION_QUEUE_READ_IN_FLIGHT")
    assert _count(config, "collection_runs") == runs_before
    # Once the issued read has expired it no longer holds the supplier.
    clock.advance(QUEUE_ISSUE_TTL_S)
    assert container.collection.run(_capture(container, "9003", None)).outcome is (
        CollectionOutcome.PENDING
    )


def test_an_expired_read_still_counts_and_stops_the_queue(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    queue_id = _declare(container, [_url("9001"), _url("9002")]).view.queue_id
    issued = _issue(container, queue_id)
    clock.advance(QUEUE_ISSUE_TTL_S)
    _refused(lambda: _capture(container, "9001", issued.ticket), "EXTENSION_QUEUE_TICKET_REFUSED")
    done = container.extension_queues.next(queue_id)
    assert (done.kind, done.queue_state) == ("DONE", QueueState.STOPPED)
    view = container.extension_queues.read(queue_id)
    assert view.stop_reason == EXTENSION_QUEUE_TICKET_EXPIRED
    # Never reissued, and the queue never skips forward.
    assert [item.state for item in view.items] == [QueueItemState.EXPIRED, QueueItemState.WAITING]


def test_a_failed_run_stops_the_queue_and_it_never_skips_forward(
    container: Container, clock: FakeClock
) -> None:
    queue_id = _declare(container, [_url("9001"), _url("9002")]).view.queue_id
    issued = _issue(container, queue_id)
    # Security material inside the product scope: the run settles FAILED (ADR-0019 §6.1).
    failing = frame(
        head=HEAD, body=BODY.replace("</table>", "</table><p>session=synthetic-value-1234</p>")
    )
    run_id = _capture(container, "9001", issued.ticket, html=failing)
    container.runner.run_next()
    assert container.collection.run(run_id).outcome is CollectionOutcome.FAILED
    clock.advance(QUEUE_MIN_INTERVAL_S)
    done = container.extension_queues.next(queue_id)
    assert (done.kind, done.queue_state) == ("DONE", QueueState.STOPPED)
    view = container.extension_queues.read(queue_id)
    assert view.stop_reason == EXTENSION_QUEUE_RUN_FAILED
    assert [item.state for item in view.items] == [QueueItemState.CAPTURED, QueueItemState.WAITING]
    assert view.items[0].run_outcome is CollectionOutcome.FAILED


def test_the_same_product_interval_counts_an_extension_capture(
    container: Container, clock: FakeClock
) -> None:
    # A single click captured the product just now; the queue may not read it again within 60 s.
    run_id = _capture(container, "9001", None)
    container.runner.run_next()
    assert container.collection.run(run_id).outcome is CollectionOutcome.RECORDED
    clock.advance(QUEUE_MIN_INTERVAL_S)
    queue_id = _declare(container, [_url("9001")]).view.queue_id
    waiting = container.extension_queues.next(queue_id)
    assert waiting.kind == "WAIT"
    assert waiting.wait_s == pytest.approx(60.0 - QUEUE_MIN_INTERVAL_S)
    clock.advance(60.0 - QUEUE_MIN_INTERVAL_S)
    assert _issue(container, queue_id).item is not None


def test_a_collected_product_is_skipped_without_a_read_when_the_operator_asks(
    container: Container, clock: FakeClock
) -> None:
    run_id = _capture(container, "9001", None)
    container.runner.run_next()
    assert container.collection.run(run_id).outcome is CollectionOutcome.RECORDED
    clock.advance(60.0)
    created = _declare(container, [_url("9001"), _url("9002")], skip_collected=True)
    assert [item.state for item in created.view.items] == [
        QueueItemState.SKIPPED,
        QueueItemState.WAITING,
    ]
    assert (created.count.skipped, created.count.queued) == (1, 1)
    assert _issue(container, created.view.queue_id).item.source_url == _url("9002")  # type: ignore[union-attr]


def test_one_open_queue_per_supplier_and_cancel_settles_every_unissued_read(
    container: Container,
) -> None:
    queue_id = _declare(container, [_url("9001"), _url("9002")]).view.queue_id
    _refused(lambda: _declare(container, [_url("9003")]), "EXTENSION_QUEUE_BUSY")
    _issue(container, queue_id)
    view = container.extension_queues.cancel(queue_id)
    assert view.state is QueueState.CANCELLED
    # The issued read was already counted and stays what it is; the unissued one is cancelled.
    assert [item.state for item in view.items] == [QueueItemState.ISSUED, QueueItemState.CANCELLED]
    assert container.extension_queues.next(queue_id).kind == "DONE"
    # A cancelled queue no longer holds the supplier.
    assert _declare(container, [_url("9003")]).view.state is QueueState.OPEN


@contextlib.contextmanager
def _process(config: AppConfig, clock: FakeClock, gateway: FakeGateway) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=TEST_JOBS,
            collection_gateway=gateway,
            collection_sessions=StubSessions(),
        )
        try:
            yield built
        finally:
            built.db.dispose()


def test_a_restart_loses_no_issued_read(config: AppConfig, clock: FakeClock) -> None:
    gateway = FakeGateway(documents=[])
    with _process(config, clock, gateway) as first:
        queue_id = _declare(first, [_url("9001"), _url("9002")]).view.queue_id
        issued = _issue(first, queue_id)
    with _process(config, clock, gateway) as second:
        view = second.extension_queues.read(queue_id)
        assert [item.state for item in view.items] == [
            QueueItemState.ISSUED,
            QueueItemState.WAITING,
        ]
        # Still one read in flight: the restart issues nothing new.
        assert second.extension_queues.next(queue_id).kind == "WAIT"
        _capture(second, "9001", issued.ticket)
        assert second.extension_queues.read(queue_id).items[0].state is QueueItemState.CAPTURED


# ---------------------------------------------------------------- the routes


def _send(
    client: TestClient, record: Any, method: str, path: str, payload: Any | None = None
) -> Any:
    body = b"" if payload is None else json.dumps(payload).encode()
    headers = signed(record, method=method, path=path, body=body)
    return client.request(method, path, content=body or None, headers=headers)


def test_the_queue_routes_need_the_pairing(client: TestClient) -> None:
    for method, path in (
        ("GET", QUEUE_POLICY),
        ("POST", QUEUES),
        ("POST", f"{QUEUES}/unknown/next"),
        ("POST", f"{QUEUES}/unknown/cancel"),
        ("GET", f"{QUEUES}/unknown"),
    ):
        response = client.request(method, path, headers={"X-ICBM-Client": "pytest-extension"})
        assert response.status_code in (401, 403), (method, path)


def test_one_queue_through_the_routes(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    record = pair(client.app.state.container)  # type: ignore[attr-defined]
    policy = _send(client, record, "GET", QUEUE_POLICY)
    assert policy.status_code == 200
    assert policy.json()["product_path"].startswith("/product/")
    secret_link = "https://kmretail.co.kr/product/token=synthetic-value/9002/"
    declared = _send(
        client,
        record,
        "POST",
        QUEUES,
        {
            "supplier_key": SUPPLIER,
            "links": [_url("9001"), secret_link],
            "max_products": 1,
            "interval_s": QUEUE_MIN_INTERVAL_S,
            "skip_collected": False,
        },
    )
    assert declared.status_code == 201
    assert declared.json()["count"]["refused"] == 1
    assert "synthetic-value" not in declared.text
    queue_id = declared.json()["queue"]["queue_id"]
    with caplog.at_level(logging.INFO):
        issued = _send(client, record, "POST", f"{QUEUES}/{queue_id}/next").json()
    assert issued["kind"] == "ISSUE" and issued["item"]["source_url"] == _url("9001")
    ticket = issued["ticket"]
    assert ticket not in "\n".join(json.dumps(vars(r), default=str) for r in caplog.records)
    assert _send(client, record, "POST", f"{QUEUES}/{queue_id}/next").json()["kind"] == "WAIT"
    capture = envelope(
        _page("9001"),
        transport=transport(url=_url("9001"), navigation_name=_url("9001")),
        queue_ticket=ticket,
    )
    accepted = _send(client, record, "POST", CAPTURES, capture)
    assert accepted.status_code == 202
    run = wait_for_outcome(client, accepted.json()["collection_run_id"])
    assert run["outcome"] == "RECORDED"
    view = _send(client, record, "GET", f"{QUEUES}/{queue_id}").json()
    assert view["items"][0]["state"] == "CAPTURED"
    assert view["items"][0]["run_outcome"] == "RECORDED"
    cancelled = _send(client, record, "POST", f"{QUEUES}/{queue_id}/cancel").json()
    assert cancelled["state"] in ("CANCELLED", "FINISHED")


def test_a_queue_declaration_is_the_exact_envelope(client: TestClient) -> None:
    record = pair(client.app.state.container)  # type: ignore[attr-defined]
    for payload in (
        {"supplier_key": SUPPLIER, "links": [_url("9001")], "max_products": 1},
        {
            "supplier_key": SUPPLIER,
            "links": [_url("9001")],
            "max_products": 1,
            "interval_s": QUEUE_MIN_INTERVAL_S,
            "skip_collected": False,
            "list_url": "https://kmretail.co.kr/category/synthetic/1/",
        },
    ):
        response = _send(client, record, "POST", QUEUES, payload)
        assert response.status_code == 422, payload
        assert response.json()["error"]["code"] == "EXTENSION_PAYLOAD_INVALID"
