"""1688 over the real application: a captured page and its embedded data only (ADR-0040; slice C2).

The real app serves the real 1688 collection and its reviewed capture policy; the collection
gateway is a script that sends nothing. The capture is one of the pinned fixtures, exactly as the
extension's ``captureInPage`` cut it in the owner's browser, with its projected embedded-data
member. Nothing is contacted.

What it proves: the capture is recorded by the canonical pipeline as a CNY revision whose gallery
images the server fetched from the reviewed image host; the description is not captured, so it
waits for review and nothing else is read; the member is never stored; a member outside the
allowlist is refused fail-closed; a secret in it refuses the capture; and no other way in exists —
no direct-URL submit, no list queue, no product page read by the server.
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
    return {"revision": "ali1688-capture-3", "digest": hashlib.sha256(canonical).hexdigest()}


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
    ) == [(NUMBER, "CNY", "ali1688-4")]
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
    # The browser read the page; the server fetched only the gallery's 17 images, from the
    # gallery host. The description is not captured and nothing reads it (ADR-0040 OC-01).
    assert gateway.document_reads == 0
    assert len(gateway.image_reads) == 17
    assert {url.split("/")[2] for url in gateway.image_reads} == {"cbu01.alicdn.com"}
    assert rows(
        config,
        "SELECT role, COUNT(*) FROM product_facts_image_refs GROUP BY role ORDER BY role",
    ) == [("DETAIL", 16), ("REPRESENTATIVE", 1)]
    assert _field(config, "detail_description") == ("REVIEW_REQUIRED", None)
    # Tier prices are never made into totals: this is a per-SKU page, so it states none.
    assert _field(config, "quantity_tiers") == ("ABSENT", None)
    # Evidence cites the member's paths under the existing EMBEDDED_JSON kind; the member itself,
    # the SKU map and the description document are never stored.
    kinds = {kind for (kind,) in rows(config, "SELECT DISTINCT kind FROM product_facts_evidence")}
    assert "EMBEDDED_JSON" in kinds and "EMBEDDED_DATA" not in kinds
    stored = json.dumps(rows(config, "SELECT * FROM product_facts_evidence"), ensure_ascii=False)
    for never in ('"skuInfoMap"', "itemcdn", "detailUrl", "5960515164202:0."):
        assert never not in stored, never


def test_every_skus_stated_count_is_recoverable_from_the_stored_revision(
    client: TestClient, config: AppConfig
) -> None:
    response = post_capture(
        client, pair(_container(client)), _capture({"projection": _projection()})
    )
    assert wait_for_outcome(client, response.json()["collection_run_id"])["outcome"] == "RECORDED"
    prefix = "embedded:result.data.Root.fields.dataJson.skuModel.skuInfoMap."
    stored = {
        locator.removeprefix(prefix).removesuffix(".canBookCount"): observed
        for locator, observed in rows(
            config,
            "SELECT locator, observed FROM product_facts_evidence"
            " WHERE field_key = 'stock' AND locator LIKE ?",
            prefix + "%.canBookCount",
        )
    }
    stored.pop("*")  # the aggregate entry, derived from the counts
    skus = _projection()["result"]["data"]["Root"]["fields"]["dataJson"]["skuModel"]["skuInfoMap"]
    assert len(skus) == 20
    assert stored == {key: str(entry["canBookCount"]) for key, entry in skus.items()}


def test_every_stated_tier_of_a_promotion_page_is_recoverable_from_the_stored_revision(
    client: TestClient, config: AppConfig
) -> None:
    projection = json.loads((FIXTURES / "970942992948.embedded.json").read_text("utf-8"))
    model = projection["result"]["data"]["mainPrice"]["fields"]["priceModel"]
    model["originalPrices"] = [
        {"price": "6.20", "beginAmount": 1},
        {"price": "5.70", "beginAmount": 90},
    ]
    url = "https://detail.1688.com/offer/970942992948.html"
    body = envelope(
        (FIXTURES / "970942992948.html").read_text("utf-8"),
        supplier_key=SUPPLIER,
        policy=_reference(),
        transport=transport(url=url, navigation_name=url),
    )
    body["embedded_data"] = {"projection": projection}
    response = post_capture(client, pair(_container(client)), body)
    assert wait_for_outcome(client, response.json()["collection_run_id"])["outcome"] == "RECORDED"
    stored = rows(
        config,
        "SELECT locator, observed, normalized FROM product_facts_evidence"
        " WHERE field_key = 'prices' AND locator LIKE '%Prices[%' ORDER BY ordinal",
    )
    prefix = "embedded:result.data.mainPrice.fields.priceModel."
    assert [
        (locator.removeprefix(prefix), observed, kind) for locator, observed, kind in stored
    ] == [
        ("originalPrices[0]", "beginAmount 1个 price 6.20 CNY", "originalPrices: original/line"),
        ("originalPrices[1]", "beginAmount 90个 price 5.70 CNY", "originalPrices: original/line"),
        (
            "currentPrices[0]",
            "beginAmount 1个 price 5.80 CNY",
            "currentPrices: current/promotional",
        ),
        (
            "currentPrices[1]",
            "beginAmount 90个 price 5.30 CNY",
            "currentPrices: current/promotional",
        ),
    ]
    status, prices = _field(config, "prices")
    assert status == "CONFIRMED" and prices is not None
    # The purchase price is the first original tier; the first current tier stays a no-role fact.
    assert json.loads(prices)["prices"] == [
        {"label": "原价 ≥1个", "currency": "CNY", "amount": "6.20", "role": "PURCHASE"},
        {"label": "现价 ≥1个", "currency": "CNY", "amount": "5.80"},
    ]


def test_a_member_outside_the_allowlist_is_refused_and_its_fields_wait_for_review(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
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


@pytest.mark.parametrize(
    ("detail_url", "outcome"),
    [
        # A plain URL reaches the allowlist re-check: the whole member is refused.
        ("https://itemcdn.tmall.com/1688offer/icoss5072039755", ("RECORDED", None)),
        # One with a credential or a secret query is refused with the whole capture.
        (
            "https://user:pw@itemcdn.tmall.com/1688offer/icoss1?token=abcdefgh12345678",
            ("FAILED", "EXTENSION_FINAL_SCAN_REFUSED"),
        ),
    ],
)
def test_a_member_that_carries_the_description_url_is_refused(
    client: TestClient,
    config: AppConfig,
    gateway: FakeGateway,
    detail_url: str,
    outcome: tuple[str, str | None],
) -> None:
    # OC-09: the description's URL is a browser-only read; the policy never projects it.
    projection = _projection()
    projection["result"]["data"]["description"] = {"fields": {"detailUrl": detail_url}}
    response = post_capture(client, pair(_container(client)), _capture({"projection": projection}))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert run["outcome"] == outcome[0]
    if outcome[1] is None:
        assert run["facts_status"] == "REVIEW_REQUIRED"
        reasons = {
            normalized
            for (normalized,) in rows(
                config, "SELECT normalized FROM product_facts_evidence WHERE locator = 'embedded'"
            )
        }
        assert reasons == {"EMBEDDED_PATH_NOT_ALLOWED"}
    else:
        assert run["detail"] == outcome[1]
        assert rows(config, "SELECT COUNT(*) FROM product_facts_revisions") == [(0,)]
    assert gateway.document_reads == 0
    assert not any("itemcdn" in url for url in gateway.image_reads)
    stored = json.dumps(rows(config, "SELECT * FROM product_facts_evidence"), ensure_ascii=False)
    for never in ("itemcdn", "detailUrl", "token="):
        assert never not in stored, never


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
    assert gateway.image_reads == []


DESCRIBED_NUMBER = "1051005117944"


def _described_capture(images: dict[str, Any] | None) -> dict[str, Any]:
    url = f"https://detail.1688.com/offer/{DESCRIBED_NUMBER}.html"
    body = envelope(
        (FIXTURES / f"{DESCRIBED_NUMBER}.html").read_text("utf-8"),
        supplier_key=SUPPLIER,
        policy=_reference(),
        transport=transport(url=url, navigation_name=url),
    )
    projection = json.loads((FIXTURES / f"{DESCRIBED_NUMBER}.embedded.json").read_text("utf-8"))
    body["embedded_data"] = {"projection": projection}
    if images is not None:
        body["description_images"] = images
    return body


def _described() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / f"{DESCRIBED_NUMBER}.description_images.json").read_text("utf-8")
    )
    return loaded


def test_the_description_images_become_detail_images_fetched_by_the_server(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
    # ADR-0040 §3.2 (OC-09): the extension read the description within the capture and sent its
    # 13 image references; the server re-checked them and fetched each one by the ordinary policed
    # image fetch, after the gallery's 9. It read no document: no description, no product page.
    response = post_capture(client, pair(_container(client)), _described_capture(_described()))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("RECORDED", None)
    described = _described()["images"]
    assert gateway.document_reads == 0
    assert gateway.image_reads[-len(described) :] == described
    assert len(gateway.image_reads) == 9 + 13
    assert {url.split("/")[2] for url in gateway.image_reads} == {"cbu01.alicdn.com"}
    assert rows(
        config,
        "SELECT role, COUNT(*) FROM product_facts_image_refs GROUP BY role ORDER BY role",
    ) == [("DETAIL", 8 + 13), ("REPRESENTATIVE", 1)]
    # The description's text is not captured; its images are recorded as what they were.
    assert _field(config, "detail_description") == ("REVIEW_REQUIRED", None)
    assert rows(
        config,
        "SELECT observed, normalized FROM product_facts_evidence"
        " WHERE field_key = 'detail_description' ORDER BY ordinal",
    ) == [(None, "DESCRIPTION_TEXT_NOT_CAPTURED"), ("13 of 13", "DESCRIPTION_IMAGES")]
    stored = json.dumps(rows(config, "SELECT * FROM product_facts_evidence"), ensure_ascii=False)
    assert "offer_details" not in stored


@pytest.mark.parametrize(
    ("images", "reason"),
    [
        (
            {"images": ["https://img.alicdn.com/tfs/icon.png"], "total": 1},
            "DESCRIPTION_IMAGES_REFUSED DESCRIPTION_IMAGES_NOT_ALLOWED",
        ),
        (
            {"images": ["https://cbu01.alicdn.com/a.jpg?x=1"], "total": 1},
            "DESCRIPTION_IMAGES_REFUSED DESCRIPTION_IMAGES_NOT_ALLOWED",
        ),
        (
            {"images": ["http://cbu01.alicdn.com/a.jpg"], "total": 1},
            "DESCRIPTION_IMAGES_REFUSED DESCRIPTION_IMAGES_NOT_ALLOWED",
        ),
        (
            {"images": [f"https://cbu01.alicdn.com/{n}.jpg" for n in range(41)], "total": 41},
            "DESCRIPTION_IMAGES_REFUSED DESCRIPTION_IMAGES_OVER_BOUND",
        ),
        ({"refused": "DESCRIPTION_STATUS"}, "DESCRIPTION_IMAGES_REFUSED DESCRIPTION_STATUS"),
        (None, "DESCRIPTION_IMAGES_REFUSED DESCRIPTION_IMAGES_ABSENT"),
    ],
)
def test_a_refused_description_member_leaves_no_detail_image_and_a_recorded_reason(
    client: TestClient,
    config: AppConfig,
    gateway: FakeGateway,
    images: dict[str, Any] | None,
    reason: str,
) -> None:
    response = post_capture(client, pair(_container(client)), _described_capture(images))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert run["outcome"] == "RECORDED"
    # Only the gallery's 9 images were fetched; nothing off the declared host ever was.
    assert len(gateway.image_reads) == 9
    assert rows(
        config,
        "SELECT normalized FROM product_facts_evidence"
        " WHERE field_key = 'detail_description' AND locator = 'description_images'",
    ) == [(reason,)]


def test_a_secret_in_a_description_image_refuses_the_capture(
    client: TestClient, config: AppConfig, gateway: FakeGateway
) -> None:
    member = {
        "images": ["https://cbu01.alicdn.com/a.jpg?access_token=abcdef0123456789"],
        "total": 1,
    }
    response = post_capture(client, pair(_container(client)), _described_capture(member))
    run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert (run["outcome"], run["detail"]) == ("FAILED", "EXTENSION_FINAL_SCAN_REFUSED")
    assert gateway.image_reads == []


def test_a_description_member_for_a_policy_that_declares_none_refuses_the_capture(
    client: TestClient, config: AppConfig
) -> None:
    km = envelope(description_images={"images": [], "total": 0})
    response = post_capture(client, pair(_container(client)), km)
    assert response.json()["error"]["code"] == "CAPTURE_DESCRIPTION_NOT_DECLARED"
    assert rows(config, "SELECT COUNT(*) FROM collection_runs") == [(0,)]


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
