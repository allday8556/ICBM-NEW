"""Helpers for the extension capture transport tests (ADR-0019 E1).

Everything is synthetic: an invented product on a KM통상-shaped page, an invented extension
identity and a pairing that lives in a memory secret store. Nothing here can reach a network; the
supplier's own host appears only as text in URLs that nothing fetches.
"""

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from httpx import Response

from app.config import AppConfig, database_path
from app.container import Container
from app.stages.collect.extension.pairing import (
    BODY_DIGEST_HEADER,
    EXTENSION_ID_HEADER,
    GENERATION_HEADER,
    NONCE_HEADER,
    PAIRING_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    PairingRecord,
    canonical_request,
    sign,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "extension" / "km_product.html"
EXTENSION_ROOT = REPO_ROOT / "ui" / "extension"
KM_POLICY = REPO_ROOT / "integrations" / "suppliers" / "kmretail" / "browser_capture_policy.json"

SUPPLIER = "kmretail"
EXTENSION_ID = "abcdefghijklmnopabcdefghijklmnop"
ORIGIN = f"chrome-extension://{EXTENSION_ID}"
ICBM_ORIGIN = "http://127.0.0.1:8790"
PRODUCT_NUMBER = "9001"
PRODUCT_URL = f"https://kmretail.co.kr/product/synthetic-sample/{PRODUCT_NUMBER}/"
CAPTURES = "/api/v1/collect/extension/captures"
POLICY = f"/api/v1/collect/extension/capture-policies/{SUPPLIER}"
RUNS = "/api/v1/collect/collections"
CLIENT = "pytest-extension"

HEAD = (
    f'<meta property="product:productId" content="{PRODUCT_NUMBER}">'
    f'<meta property="product:retailer_item_id" content="{PRODUCT_NUMBER}">'
    f'<link rel="canonical" href="{PRODUCT_URL}">'
)
BODY = (
    '<div class="xans-product-detail">'
    '<div class="xans-product-image"><div class="keyImg">'
    '<img src="/web/product/big/synthetic-9001.jpg"></div></div>'
    '<div class="infoArea"><table><tbody>'
    "<tr><th>상품명</th><td>합성 샘플 상품 1kg</td></tr>"
    "<tr><th>판매가</th><td>12,000원</td></tr>"
    "<tr><th>배송비</th><td>3,000원</td></tr>"
    "</tbody></table>"
    '<div class="xans-product-action"><span id="btnBuy" class="btnBuy">구매하기</span></div>'
    "</div></div>"
    '<div id="prdDetail"><p>합성 샘플 상품 설명입니다.</p>'
    '<img ec-data-src="/web/upload/synthetic/detail-1.jpg"></div>'
)
# Every browser these tests launch resolves no host name at all: only the literal loopback address
# is reachable. A page is served by answering its requests in the test, which happens before any
# name is resolved, so tests still load pages "at" the supplier's host — and a request the test
# did not answer (a redirect follow-up is never offered to a route handler) fails in the resolver
# instead of leaving the machine. Without this argument a routed 302 sends a real request.
NETWORK_BLOCK = "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"

# Every table an accepted extension capture may touch. Everything else must stay exactly as it is.
RUN_OWNED_TABLES = frozenset({"collection_runs", "jobs", "job_attempts", "audit_events"})


def frame(head: str = HEAD, body: str = BODY) -> str:
    """The frame the extension builds around a capture."""
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


def policy_reference() -> dict[str, str]:
    """The reviewed KM policy's revision and digest, recomputed from the repository file."""
    document = json.loads(KM_POLICY.read_text("utf-8"))
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {"revision": document["revision"], "digest": hashlib.sha256(canonical).hexdigest()}


def transport(**overrides: Any) -> dict[str, Any]:
    observed: dict[str, Any] = {
        "url": PRODUCT_URL,
        "navigation_name": PRODUCT_URL,
        "response_status": 200,
        "redirect_count": 0,
        "content_type": "text/html",
        "character_set": "UTF-8",
    }
    observed.update(overrides)
    return observed


def envelope(html: str | None = None, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "supplier_key": SUPPLIER,
        "policy": policy_reference(),
        "transport": transport(),
        "html": frame() if html is None else html,
    }
    body.update(overrides)
    return body


def pair(container: Container) -> PairingRecord:
    """Pair the synthetic extension and return the record, secret included, to sign with."""
    return container.extension_pairing.pair(EXTENSION_ID, origin=ICBM_ORIGIN).record


def signed(
    record: PairingRecord,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    now: datetime | float | None = None,
    nonce: str | None = None,
    origin: str | None = ORIGIN,
    client_header: str | None = CLIENT,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The headers the extension would send for one request, or a deliberately wrong set."""
    if now is None:
        now = time.time()
    timestamp = str(int(now.timestamp() if isinstance(now, datetime) else now))
    nonce = nonce or secrets.token_urlsafe(24)
    body_sha256 = hashlib.sha256(body).hexdigest()
    signature = sign(
        record.secret,
        canonical_request(
            method=method,
            path=path,
            extension_id=record.extension_id,
            pairing_id=record.pairing_id,
            generation=str(record.generation),
            timestamp=timestamp,
            nonce=nonce,
            body_sha256=body_sha256,
        ),
    )
    headers = {
        EXTENSION_ID_HEADER: record.extension_id,
        PAIRING_ID_HEADER: record.pairing_id,
        GENERATION_HEADER: str(record.generation),
        TIMESTAMP_HEADER: timestamp,
        NONCE_HEADER: nonce,
        BODY_DIGEST_HEADER: body_sha256,
        SIGNATURE_HEADER: signature,
        "Content-Type": "application/json",
    }
    if client_header is not None:
        headers["X-ICBM-Client"] = client_header
    if origin is not None:
        headers["Origin"] = origin
    headers.update(overrides or {})
    return headers


def post_capture(
    client: TestClient,
    record: PairingRecord,
    payload: Mapping[str, Any] | None = None,
    **signing: Any,
) -> Response:
    body = json.dumps(envelope() if payload is None else payload, ensure_ascii=False).encode()
    return client.post(
        CAPTURES,
        content=body,
        headers=signed(record, method="POST", path=CAPTURES, body=body, **signing),
    )


def wait_for_outcome(client: TestClient, run_id: str, *, timeout_s: float = 15.0) -> dict[str, Any]:
    """Read the canonical run back until it has left PENDING."""
    deadline = time.monotonic() + timeout_s
    while True:
        run: dict[str, Any] = client.get(f"{RUNS}/{run_id}").json()
        if run["outcome"] != "PENDING":
            return run
        if time.monotonic() > deadline:
            raise AssertionError(f"run {run_id} is still PENDING")
        time.sleep(0.05)


def table_counts(config: AppConfig) -> dict[str, int]:
    """How many rows every table holds, read from the database file itself."""
    with closing(sqlite3.connect(database_path(config.data_dir))) as raw:
        names = [
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {name: raw.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}


def untouched(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, tuple[int, int]]:
    """Every table outside the run's own that changed: what a zero-write proof must find empty."""
    return {
        name: (before[name], after[name])
        for name in before
        if name not in RUN_OWNED_TABLES and before[name] != after[name]
    }
