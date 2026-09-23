"""The COLLECT submit path the screen uses (Gate 1 G1-E, ADR-0015 §5, Issue #89 5794763664).

The real application is served over a scripted shop (``tests.collect_submit_support``): one POST
becomes one durable run and job, the worker executes it, and everything after the POST is a read.
Proven here:
- the screen contract names the collection-capable suppliers the server has, and only those;
- one submit is one run and one job, followed to RECORDED with its revision and facts status;
- a refused URL, an unknown supplier and a pacing refusal each create no run and no job;
- NO_REVISION and FAILED are terminal, carry the durable detail, and are never retried;
- the newest runs are listed from the run store itself, bounded and totally ordered;
- a RECORDED run hands off to the Product DB by its revision's source identity: MATERIALIZED,
  CURRENT_REVISION_DIFFERS or NOT_YET_VISIBLE, and any other run has nothing to hand off;
- none of these reads writes anything, and no G1-D row appears.
"""

import contextlib
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from tests.collect_submit_support import (
    FAILED_ID,
    NO_REVISION_ID,
    SUPPLIER_KEY,
    ScriptedShop,
    product_url,
    served,
)
from tests.product_support import Collections, raw

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
RUNS = "/api/v1/collect/collections"
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


@pytest.fixture
def shop() -> ScriptedShop:
    return ScriptedShop()


@pytest.fixture
def api(config: AppConfig, shop: ScriptedShop) -> Iterator[TestClient]:
    with served(config, shop) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served_container: Container = api.app.state.container
    return served_container


def submit(api: TestClient, number: str, supplier: str = SUPPLIER_KEY) -> Any:
    return api.post(
        RUNS, json={"supplier_key": supplier, "product_url": product_url(number)}, headers=CLIENT
    )


def settled(api: TestClient, run_id: str, timeout_s: float = 15.0) -> dict[str, Any]:
    """The run once the worker has given it an outcome."""
    deadline = time.monotonic() + timeout_s
    while True:
        run = api.get(f"{RUNS}/{run_id}", headers=CLIENT).json()
        if run["outcome"] != "PENDING" or time.monotonic() > deadline:
            return dict(run)
        time.sleep(0.05)


def everything(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def collect_jobs(config: AppConfig) -> list[tuple[str, int]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(
            "SELECT state, attempt_count FROM jobs WHERE job_type = 'collect.product'"
            " ORDER BY created_at"
        ).fetchall()


# ---------------------------------------------------------------- eligibility and submit


def test_the_screen_names_only_the_collection_capable_suppliers(api: TestClient) -> None:
    screen = api.get("/api/v1/screens/collect", headers=CLIENT).json()
    assert screen["collection_supplier_keys"] == [SUPPLIER_KEY]
    # CONNECT's own suppliers stay CONNECT's: one without a collection definition is not listed.
    assert {s["supplier_key"] for s in screen["suppliers"]} == {"kmretail"}
    assert screen["meta"]["state"] == "EMPTY" and screen["collection_jobs_total"] == 0


def test_one_submit_is_one_run_and_one_job_followed_to_recorded(
    api: TestClient, config: AppConfig, container: Container
) -> None:
    response = submit(api, "4242")
    assert response.status_code == 202, response.text
    submitted = response.json()
    assert set(submitted) == {"collection_run_id", "job_id", "correlation_id"}
    run = settled(api, submitted["collection_run_id"])
    assert run["outcome"] == "RECORDED"
    assert (run["job_id"], run["correlation_id"]) == (
        submitted["job_id"],
        submitted["correlation_id"],
    )
    stored = container.revisions.get(run["revision_id"])
    assert stored is not None and stored.collection_run_id == submitted["collection_run_id"]
    assert run["facts_status"] == stored.facts_status.value
    listed = api.get(RUNS, headers=CLIENT).json()
    assert [r["collection_run_id"] for r in listed["runs"]] == [submitted["collection_run_id"]]
    assert listed["runs"][0] == run and listed["limit"] == 10
    counts = everything(config)
    assert counts["collection_runs"] == 1 and collect_jobs(config) == [("SUCCEEDED", 1)]
    assert {t: counts[t] for t in NEVER_CREATED} == dict.fromkeys(NEVER_CREATED, 0)


def test_a_refused_submit_creates_no_run_and_no_job(api: TestClient, config: AppConfig) -> None:
    before = everything(config)
    cases = [
        (
            {"supplier_key": SUPPLIER_KEY, "product_url": "https://elsewhere.invalid/p/1/"},
            (422, "COLLECT_URL_REFUSED"),
        ),
        (
            {"supplier_key": "kmretail", "product_url": product_url("4242")},
            (404, "COLLECT_SUPPLIER_UNKNOWN"),
        ),
    ]
    for body, (status, code) in cases:
        response = api.post(RUNS, json=body, headers=CLIENT)
        assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert everything(config) == before
    # Same-product pacing: the product was just read, so a second request is refused whole.
    first = submit(api, "4242").json()
    assert settled(api, first["collection_run_id"])["outcome"] == "RECORDED"
    after_first = everything(config)
    again = submit(api, "4242")
    assert again.status_code == 429
    assert again.json()["error"]["code"] == "COLLECT_SAME_PRODUCT_TOO_SOON"
    assert everything(config) == after_first


def test_no_revision_and_failed_are_terminal_and_never_retried(
    api: TestClient, config: AppConfig, shop: ScriptedShop
) -> None:
    unresolved = submit(api, NO_REVISION_ID).json()
    failing = submit(api, FAILED_ID).json()
    no_revision = settled(api, unresolved["collection_run_id"])
    failed = settled(api, failing["collection_run_id"])
    assert (no_revision["outcome"], no_revision["revision_id"], no_revision["detail"]) == (
        "NO_REVISION",
        None,
        "the page declares no product number",
    )
    assert (failed["outcome"], failed["revision_id"], failed["detail"]) == (
        "FAILED",
        None,
        "SUPPLIER_SESSION_EXPIRED",
    )
    time.sleep(0.5)
    # One attempt each, and each page read once: nothing retried either run.
    assert sorted(collect_jobs(config)) == [("DEAD", 1), ("SUCCEEDED", 1)]
    assert sorted(shop.reads) == [NO_REVISION_ID, FAILED_ID]
    assert everything(config)["collection_runs"] == 2


def test_recent_runs_are_bounded_and_totally_ordered(api: TestClient, config: AppConfig) -> None:
    made = [submit(api, number).json()["collection_run_id"] for number in ("11", "12", "13")]
    for run_id in made:
        settled(api, run_id)
    with contextlib.closing(raw(config)) as connection:
        expected = [
            row[0]
            for row in connection.execute(
                "SELECT collection_run_id FROM collection_runs"
                " ORDER BY requested_at DESC, collection_run_id DESC"
            )
        ]
    listed = api.get(RUNS, params={"limit": 2}, headers=CLIENT).json()
    assert [r["collection_run_id"] for r in listed["runs"]] == expected[:2]
    assert listed["limit"] == 2
    whole = api.get(RUNS, headers=CLIENT).json()
    assert [r["collection_run_id"] for r in whole["runs"]] == expected
    for limit in (0, 51):
        response = api.get(RUNS, params={"limit": limit}, headers=CLIENT)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "COLLECT_RUN_LIMIT_INVALID"


# ---------------------------------------------------------------- Product DB handoff


def test_a_recorded_run_hands_off_to_the_product_it_materialized(
    api: TestClient, container: Container
) -> None:
    run = settled(api, submit(api, "4242").json()["collection_run_id"])
    handoff = api.get(f"{RUNS}/{run['collection_run_id']}/product", headers=CLIENT).json()
    product = api.get(f"/api/v1/products/by-source/{SUPPLIER_KEY}/4242", headers=CLIENT).json()
    assert handoff == {
        "supplier_key": SUPPLIER_KEY,
        "source_product_id": "4242",
        "revision_id": run["revision_id"],
        "state": "MATERIALIZED",
        "product_group_id": product["product_group_id"],
        "current_source_revision_id": run["revision_id"],
    }


def test_the_handoff_is_truthful_before_and_after_materialization(
    api: TestClient, config: AppConfig, container: Container
) -> None:
    # Runs recorded without the materializer behind them: a source no Product holds yet.
    sources = Collections.of(container, config)
    first, _ = sources.collect(source_product_id="5151")

    def handoff(run_id: str) -> dict[str, Any]:
        response = api.get(f"{RUNS}/{run_id}/product", headers=CLIENT)
        assert response.status_code == 200, response.text
        return dict(response.json())

    before = everything(config)
    waiting = handoff(first)
    assert (waiting["state"], waiting["product_group_id"]) == ("NOT_YET_VISIBLE", None)
    assert everything(config) == before, "a handoff read materializes nothing"
    container.materializer.materialize_run(first)
    assert handoff(first)["state"] == "MATERIALIZED"
    # A later run moves the Product's current revision: the earlier run says so.
    second, _ = sources.collect(source_product_id="5151")
    container.materializer.materialize_run(second)
    earlier, later = handoff(first), handoff(second)
    assert earlier["state"] == "CURRENT_REVISION_DIFFERS"
    assert earlier["current_source_revision_id"] == later["revision_id"]
    assert later["state"] == "MATERIALIZED"
    assert earlier["product_group_id"] == later["product_group_id"] is not None


def test_a_run_without_a_revision_has_nothing_to_hand_off(api: TestClient) -> None:
    for number in (NO_REVISION_ID, FAILED_ID):
        run = settled(api, submit(api, number).json()["collection_run_id"])
        response = api.get(f"{RUNS}/{run['collection_run_id']}/product", headers=CLIENT)
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "COLLECT_RUN_NOT_RECORDED"
        assert error["details"] == {"outcome": run["outcome"]}
    unknown = api.get(f"{RUNS}/00000000-0000-0000-0000-000000000000/product", headers=CLIENT)
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "COLLECT_RUN_UNKNOWN")


def test_the_follow_up_reads_write_nothing(api: TestClient, config: AppConfig) -> None:
    run = settled(api, submit(api, "4242").json()["collection_run_id"])
    before = everything(config)
    for _ in range(3):
        api.get(RUNS, headers=CLIENT)
        api.get(f"{RUNS}/{run['collection_run_id']}", headers=CLIENT)
        api.get(f"{RUNS}/{run['collection_run_id']}/product", headers=CLIENT)
        api.get("/api/v1/screens/collect", headers=CLIENT)
    assert everything(config) == before
