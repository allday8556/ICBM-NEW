"""A scripted supplier shop for serving the real COLLECT submit path (Gate 1 G1-E).

The application is served with the synthetic ``fakeshop`` collection of ``scripts.m3collect`` and
a transport that answers by product number, so one test can drive every durable outcome through
the real job, run store, revision store and materializer:

- ``/product/sample/<n>/`` — an ordinary product page declaring product ``n``: RECORDED;
- product ``0`` — a page that declares no product number: NO_REVISION;
- product ``9009`` — CONNECT's session is refused: a non-retryable AUTH failure, FAILED;
- product ``7001`` — the read waits until the test releases it: the run stays PENDING.

Nothing here can reach a network: the gateway knows no host and sends nothing.
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from app.collect.collection import RegisteredCollection
from app.config import AppConfig
from app.core.errors import AuthError
from app.main import create_app
from app.products.materialization import Materialization, ProductMaterializer
from integrations.suppliers.collection import CollectionProfile, DocumentView, ReadKind
from integrations.suppliers.transport.collection import RequestBudget
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    DETAIL_URL,
    EXTRACTOR_FINGERPRINT,
    EXTRACTOR_REVISION,
    HOST,
    PRIMARY_BYTES,
    PRIMARY_URL,
    SUPPLIER_KEY,
    FakeGateway,
    StubSessions,
    collection,
    document,
    page,
)
from tests.conftest import LOCAL

NO_REVISION_ID = "0"
FAILED_ID = "9009"
HELD_ID = "7001"
HELD_TIMEOUT_S = 30.0
RUNS = "/api/v1/collect/collections"
CLIENT = {"X-ICBM-Client": "pytest"}


def product_url(number: str) -> str:
    return f"https://{HOST}/product/sample/{number}/"


def registered() -> RegisteredCollection:
    return RegisteredCollection(
        collection=collection(),
        extractor_revision=EXTRACTOR_REVISION,
        extractor_fingerprint=EXTRACTOR_FINGERPRINT,
    )


@dataclass
class ScriptedShop(FakeGateway):
    """Answers each product page by the number in its URL, as the module docstring lists."""

    release: threading.Event = field(default_factory=threading.Event)
    reads: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.images.update({PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES})

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        budget.reserve(kind, url)
        number = [part for part in url.split("/") if part][-1]
        self.reads.append(number)
        self.document_reads += 1
        if number == FAILED_ID:
            raise AuthError("SUPPLIER_SESSION_EXPIRED", "the supplier refused the stored session")
        if number == HELD_ID:
            self.release.wait(HELD_TIMEOUT_S)
        return document(page(product_id="" if number == NO_REVISION_ID else number))


TERMINAL_JOB_STATES = frozenset({"SUCCEEDED", "DEAD"})


def settled_and_job_terminal(
    api: TestClient, run_id: str, *, timeout_s: float = 15.0
) -> dict[str, Any]:
    """The run once its outcome is durable **and** its ``collect.product`` job has ended.

    A run's outcome is committed before the same job hands a RECORDED run to the M4 materializer,
    so ``RECORDED`` with its Product ``NOT_YET_VISIBLE`` is a legitimate moment. A test that
    asserts on what the whole job did — its final state, the materialized Product, a baseline
    nothing may change afterwards — waits here for the job to end as well; a test about the run's
    own outcome only needs the outcome.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        run = api.get(f"{RUNS}/{run_id}", headers=CLIENT).json()
        job = api.get(f"/api/v1/system/jobs/{run['job_id']}", headers=CLIENT).json()
        if run["outcome"] != "PENDING" and job["state"] in TERMINAL_JOB_STATES:
            return dict(run)
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run['outcome']} / job {job['state']} did not settle")
        time.sleep(0.05)


@contextmanager
def gated_materialization() -> Iterator[threading.Event]:
    """Hold every M4 materialization until the returned event is set.

    It must wrap the application's construction: the collection service keeps the materializer's
    bound method it was built with. It only delays the materializer the product code already
    calls, in the order it already calls it, so the ``RECORDED`` / ``NOT_YET_VISIBLE`` window
    becomes wide and deterministic instead of a few milliseconds of luck.
    """
    gate = threading.Event()
    original = ProductMaterializer.materialize_run

    def held(
        self: ProductMaterializer, collection_run_id: str, *, correlation_id: str | None = None
    ) -> Materialization:
        gate.wait(HELD_TIMEOUT_S)
        return original(self, collection_run_id, correlation_id=correlation_id)

    ProductMaterializer.materialize_run = held  # type: ignore[method-assign]
    try:
        yield gate
    finally:
        gate.set()
        ProductMaterializer.materialize_run = original  # type: ignore[method-assign]


@contextmanager
def served(config: AppConfig, shop: ScriptedShop) -> Iterator[TestClient]:
    """The real application, collecting from the scripted shop. A held read is released before
    the application stops, so its worker never waits out the hold."""
    app = create_app(
        config,
        collection_gateway=shop,
        collection_sessions=StubSessions(),
        collections=(registered(),),
    )
    try:
        with TestClient(app, base_url=LOCAL) as client:
            try:
                yield client
            finally:
                shop.release.set()
    finally:
        shop.release.set()


__all__ = [
    "FAILED_ID",
    "HELD_ID",
    "NO_REVISION_ID",
    "SUPPLIER_KEY",
    "ScriptedShop",
    "gated_materialization",
    "product_url",
    "registered",
    "served",
    "settled_and_job_terminal",
]
