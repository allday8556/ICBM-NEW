"""1688 over the real application: a captured page and its embedded data only (ADR-0040; slice C2).

The real app serves the real 1688 collection and its reviewed capture policy; the collection
gateway is a script that sends nothing. The capture is one of the pinned fixtures, exactly as the
extension's ``captureInPage`` cut it in the owner's browser, with its projected embedded-data
member; the description document is the one that page named. Nothing is contacted.

What it proves: the capture is recorded by the canonical pipeline as a CNY revision whose gallery
and description images the server fetched from the reviewed image hosts after reading the
description document itself; the member is never stored; a member outside the allowlist is refused
fail-closed; a secret in it refuses the capture; and no other way in exists — no direct-URL submit,
no list queue, no product page read by the server.
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

SUPPLIER = "ali1688"
NUMBER = "990041659965"
URL = f"https://detail.1688.com/offer/{NUMBER}.html"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "suppliers" / SUPPLIER
POLICY_FILE = REPO_ROOT / "integrations" / "suppliers" / SUPPLIER / "browser_capture_policy.json"
DESCRIPTION_URL = "https://itemcdn.tmall.com/1688offer/icoss5072039755d496fa52ec17612"
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
    return {"revision": "ali1688-capture-1", "digest": hashlib.sha256(canonical).hexdigest()}


def _projection() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{NUMBER}.embedded.json").read_text("utf-8"))
    return loaded


def _capture(member: dict[str, Any] | None) -> dict[str, Any]:
    body = envelope(
        (FIXTURES / f"{NUMBER}.html").read_text("utf-8"),
        supplier_key=SUPPLIER,
        policy=_reference(),
        transport=transport(url=URL, navigation_name=URL),
    )
    if member is not None:
        body["embedded_data"] = member
    return body


def _serve_description(gateway: FakeGateway) -> None:
    gateway.descriptions[DESCRIPTION_URL] = (FIXTURES / f"{NUMBER}.description.txt").read_text(
        "utf-8"
    )


def _field(config: AppConfig, key: str) -> tuple[str, str | None]:
    (found,) = rows(
        config,
        "SELECT f.status, f.value_json FROM product_facts_fields f"
        " JOIN product_facts_revisions r ON f.revision_id = r.revision_id"
        " WHERE f.field_key = ?",
        key,
    )
    return found[0], found[1]


def test_a_captured_page_is_recorded_as_a_cny_revision(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
    _serve_description(gateway)
    record = pair(_container(client))
    # ADR-0030 §6: the extension learns the reviewed host from ICBM, not from a constant.
    hosts = client.get(SUPPLIERS_PATH, headers=signed(record, method="GET", path=SUPPLIERS_PATH))
    assert hosts.json()["suppliers"]["detail.1688.com"] == SUPPLIER
    served = client.get(POLICY_PATH, headers=signed(record, method="GET", path=POLICY_PATH))
    assert served.status_code == 200
    assert hashlib.sha256(served.content).hexdigest() == _reference()["digest"]
    assert json.loads(served.content)["embedded_data"]["bare_integer_keys"] is True

    response = post_capture(client, record, _capture({"projection": _projection()}))
    assert response.status_code == 202, response.json()
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("RECORDED", None)
    assert (run["supplier_key"], run["source_url"], run["transport_kind"]) == (
        SUPPLIER,
        URL,
        "EXTENSION",
    )
    assert rows(
        config,
        "SELECT source_product_id, source_currency, extractor_revision"
        " FROM product_facts_revisions",
    ) == [(NUMBER, "CNY", "ali1688-1")]
    status, prices = _field(config, "prices")
    assert status == "CONFIRMED" and prices is not None
    assert json.loads(prices) == {
        "prices": [
            {"label": "SKU最低原价", "currency": "CNY", "amount": "9.50", "role": "PURCHASE"}
        ]
    }
    status, options = _field(config, "options")
    assert status == "CONFIRMED" and len(json.loads(options or "")["configurations"]) == 20
    assert _field(config, "shipping")[0] == "CONFIRMED"
    # The browser read the page; the server read only the description document it named, from the
    # description host, and then the gallery's 17 images and the description's 9.
    assert gateway.document_reads == 0
    assert gateway.description_reads == [DESCRIPTION_URL]
    assert len(gateway.image_reads) == 17 + 9
    assert {url.split("/")[2] for url in gateway.image_reads} == {
        "cbu01.alicdn.com",
        "img.alicdn.com",
    }
    assert rows(
        config,
        "SELECT role, COUNT(*) FROM product_facts_image_refs GROUP BY role ORDER BY role",
    ) == [("DETAIL", 25), ("REPRESENTATIVE", 1)]
    # Evidence cites the member's paths under the existing EMBEDDED_JSON kind; the member itself,
    # the SKU map and the description document are never stored.
    kinds = {kind for (kind,) in rows(config, "SELECT DISTINCT kind FROM product_facts_evidence")}
    assert "EMBEDDED_JSON" in kinds and "EMBEDDED_DATA" not in kinds
    stored = json.dumps(rows(config, "SELECT * FROM product_facts_evidence"), ensure_ascii=False)
    for never in ('"skuInfoMap"', "offer_details", "content", "5960515164202:"):
        assert never not in stored, never


def test_a_member_outside_the_allowlist_is_refused_and_its_fields_wait_for_review(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
    _serve_description(gateway)
    projection = _projection()
    # OC-05: a receive address that reached the server is refused with the whole member.
    freight = projection["result"]["data"]["shippingServices"]["fields"]["freightInfo"]
    freight["recieveAddress"] = "某省某市某区某路 1 号"
    response = post_capture(client, pair(_container(client)), _capture({"projection": projection}))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert run["outcome"] == "RECORDED" and run["facts_status"] == "REVIEW_REQUIRED"
    for key in ("prices", "options", "stock", "quantity_tiers", "shipping"):
        assert _field(config, key) == ("REVIEW_REQUIRED", None), key
    reasons = {
        normalized
        for (normalized,) in rows(
            config, "SELECT normalized FROM product_facts_evidence WHERE locator = 'embedded'"
        )
    }
    assert reasons == {"EMBEDDED_PATH_NOT_ALLOWED"}
    stored = json.dumps(rows(config, "SELECT * FROM product_facts_evidence"), ensure_ascii=False)
    assert "某省某市" not in stored
    # No member means no description URL: nothing was read for it.
    assert gateway.description_reads == []


def test_a_browser_refusal_is_recorded_as_its_code(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
    response = post_capture(
        client, pair(_container(client)), _capture({"refused": "EMBEDDED_NOT_JSON"})
    )
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert run["outcome"] == "RECORDED" and run["facts_status"] == "REVIEW_REQUIRED"
    assert _field(config, "prices") == ("REVIEW_REQUIRED", None)
    assert rows(config, "SELECT normalized FROM product_facts_evidence WHERE locator = 'embedded'")[
        0
    ] == ("EMBEDDED_NOT_JSON",)


def test_a_secret_in_the_member_refuses_the_capture(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
    projection = _projection()
    projection["result"]["data"]["shippingServices"]["fields"]["freightInfo"]["location"] = (
        "token=abcdefgh12345678"
    )
    response = post_capture(client, pair(_container(client)), _capture({"projection": projection}))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("FAILED", "EXTENSION_FINAL_SCAN_REFUSED")
    assert rows(config, "SELECT COUNT(*) FROM product_facts_revisions") == [(0,)]
    assert gateway.image_reads == [] and gateway.description_reads == []


def test_an_unreadable_description_leaves_the_detail_images_out(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
    gateway.descriptions[DESCRIPTION_URL] = "var offer_details=<html>;"
    response = post_capture(
        client, pair(_container(client)), _capture({"projection": _projection()})
    )
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert run["outcome"] == "RECORDED" and run["facts_status"] == "REVIEW_REQUIRED"
    assert _field(config, "detail_description") == ("REVIEW_REQUIRED", None)
    assert rows(
        config,
        "SELECT normalized FROM product_facts_evidence WHERE status = 'REVIEW_REQUIRED'"
        " AND locator = 'embedded:result.data.description.fields.detailUrl'",
    ) == [("description:DESCRIPTION_NOT_JSON",)]
    # Only the gallery's images were fetched.
    assert len(gateway.image_reads) == 17


def test_a_member_for_a_policy_that_declares_none_refuses_the_capture(
    client: TestClient, config: AppConfig
) -> None:
    km = envelope(embedded_data={"refused": "EMBEDDED_NOT_JSON"})
    response = post_capture(client, pair(_container(client)), km)
    assert response.json()["error"]["code"] == "CAPTURE_EMBEDDED_DATA_NOT_DECLARED"
    assert rows(config, "SELECT COUNT(*) FROM collection_runs") == [(0,)]


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
    assert _container(client).supplier_names[SUPPLIER] == "1688"
    assert gateway.document_reads == 0 and gateway.image_reads == []
