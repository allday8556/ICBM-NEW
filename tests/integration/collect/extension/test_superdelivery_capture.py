"""슈퍼딜리버리 over the real application: a captured page only (ADR-0040 §1, §5; slice C1).

The real app serves the real 슈퍼딜리버리 collection and its reviewed capture policy; the collection
gateway is a script that sends nothing. The capture is one of the pinned fixtures, exactly as the
extension's ``captureInPage`` cut it in the owner's browser. Nothing is contacted.

What it proves: the extension learns the host from ICBM, the capture is recorded by the canonical
pipeline as a JPY revision whose images the server fetched from the reviewed image host, and no
other way in exists — no direct-URL submit, no list queue, no product page read by the server.
"""

import hashlib
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from automation.acceptance.m3.rehearsal.fake_shop import FakeGateway
from tests.support.extension_support import (
    REPO_ROOT,
    RUNS,
    envelope,
    pair,
    post_capture,
    signed,
    transport,
    wait_for_outcome,
)
from tests.support.shadow_support import rows

pytestmark = pytest.mark.integration

SUPPLIER = "superdelivery"
NUMBER = "12064384"
URL = f"https://www.superdelivery.com/p/r/pd_p/{NUMBER}/"
CAPTURE = REPO_ROOT / "tests" / "fixtures" / "suppliers" / "superdelivery" / f"{NUMBER}.html"
POLICY_FILE = REPO_ROOT / "integrations" / "suppliers" / SUPPLIER / "browser_capture_policy.json"
SUPPLIERS_PATH = "/api/v1/collect/extension/suppliers"
POLICY_PATH = f"/api/v1/collect/extension/capture-policies/{SUPPLIER}"
QUEUE_POLICY_PATH = f"/api/v1/collect/extension/queue-policies/{SUPPLIER}"
CLIENT = {"X-ICBM-Client": "pytest"}


def _container(client: TestClient) -> Container:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    return container


def _reference() -> dict[str, str]:
    canonical = json.dumps(
        json.loads(POLICY_FILE.read_text("utf-8")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "revision": "superdelivery-capture-1",
        "digest": hashlib.sha256(canonical).hexdigest(),
    }


def _capture() -> dict[str, Any]:
    return envelope(
        CAPTURE.read_text("utf-8"),
        supplier_key=SUPPLIER,
        policy=_reference(),
        transport=transport(url=URL, navigation_name=URL),
    )


def test_a_captured_page_is_recorded_as_a_jpy_revision(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
    record = pair(_container(client))
    # ADR-0030 §6: the extension learns the reviewed host from ICBM, not from a constant.
    hosts = client.get(SUPPLIERS_PATH, headers=signed(record, method="GET", path=SUPPLIERS_PATH))
    assert hosts.json()["suppliers"]["www.superdelivery.com"] == SUPPLIER
    served = client.get(POLICY_PATH, headers=signed(record, method="GET", path=POLICY_PATH))
    assert served.status_code == 200
    assert hashlib.sha256(served.content).hexdigest() == _reference()["digest"]

    response = post_capture(client, record, _capture())
    assert response.status_code == 202, response.json()
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("RECORDED", None)
    assert (run["supplier_key"], run["source_url"], run["transport_kind"]) == (
        SUPPLIER,
        URL,
        "EXTENSION",
    )
    # The price is undisclosed: the revision is held for review, never priced (OC-07).
    assert run["facts_status"] == "REVIEW_REQUIRED"
    assert rows(
        config,
        "SELECT source_product_id, source_currency, extractor_revision"
        " FROM product_facts_revisions",
    ) == [(NUMBER, "JPY", "superdelivery-2")]
    assert rows(
        config,
        "SELECT status FROM product_facts_fields f JOIN product_facts_revisions r"
        " ON f.revision_id = r.revision_id WHERE f.field_key = 'prices'",
    ) == [("REVIEW_REQUIRED",)]
    # The browser read the page; the server fetched only the images, from the image host: the
    # main image, then the image list, whose one entry is the main image again.
    image = (
        "https://c.superdelivery.com/ip/n/sa/600/600/www.superdelivery.com"
        "/product_image/012/064/384/12064384_1000.jpg"
    )
    assert gateway.document_reads == 0
    assert gateway.image_reads == [image, image]


def test_no_other_way_in(client: TestClient, config: AppConfig, gateway: FakeGateway) -> None:
    record = pair(_container(client))
    # OC-01: no direct-URL submit, refused before any job exists.
    submitted = client.post(
        RUNS, json={"supplier_key": SUPPLIER, "product_url": URL}, headers=CLIENT
    )
    assert submitted.json()["error"]["code"] == "COLLECT_DIRECT_URL_NOT_DECLARED"
    assert rows(config, "SELECT COUNT(*) FROM jobs") == [(0,)]
    # The submit screen does not offer it; the supplier is still collected (capture only).
    screen = client.get("/api/v1/screens/collect", headers=CLIENT).json()
    assert SUPPLIER not in screen["collection_supplier_keys"]
    assert SUPPLIER in _container(client).collection.supplier_keys()
    # ADR-0019 AC-28: no list queue, refused before any read.
    queue = client.get(
        QUEUE_POLICY_PATH, headers=signed(record, method="GET", path=QUEUE_POLICY_PATH)
    )
    assert queue.json()["error"]["code"] == "EXTENSION_QUEUE_NOT_DECLARED"
    # OC-02: no CONNECT definition, so no login and no credential.
    connect = client.get("/api/v1/connect/suppliers", headers=CLIENT).json()
    assert SUPPLIER not in {s["supplier_key"] for s in connect}
    assert _container(client).supplier_names[SUPPLIER] == "슈퍼딜리버리"
    assert gateway.document_reads == 0 and gateway.image_reads == []
