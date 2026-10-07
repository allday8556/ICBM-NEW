"""Supplier common images: detection, the owner's seed and the operator's decisions (Issue #219).

A file a supplier repeats in the detail images of three or more different products is a candidate,
``REVIEW`` until an operator decides ``BLOCK`` or ``KEEP``; the decision is remembered and appended,
the owner's own decisions on eight KM통상 files apply until an operator decides otherwise, and a
synthetic test product reads its template supplier's verdicts without counting towards detection.
"""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.stages.collect.assets import SourceAssetStore
from app.stages.collect.facts import FieldStatus, ImageReference, ImageRole
from app.stages.collect.models import ProductFactsRevision
from app.stages.products.common_images import (
    DETECTION_MIN_PRODUCTS,
    OWNER_SEED,
    CommonImageVerdict,
    effective_supplier,
    verdicts,
)
from app.stages.products.image_models import SupplierCommonImageDecision
from tests.support.collect_support import PNG
from tests.support.jobs_support import FakeClock
from tests.support.product_support import SUPPLIER, Collections, FakeDecoder, count, product

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
SHIPPING_NOTICE = "0cf0a06abcfc2782d4c25ee89d47fb95745fc9c011200022329787b4b4cf2f62"
BRAND_BANNER = "e251a58accb130763f07a5d57d3047b71308c158b541caca24428cd226641d22"


def _services(client: TestClient) -> Container:
    return client.app.state.container  # type: ignore[attr-defined,no-any-return]


def _stored(client: TestClient, config: AppConfig, marker: str) -> str:
    """Distinct stored source bytes; their SHA-256."""
    services = _services(client)
    assets = SourceAssetStore(config.source_assets_dir, services.db, FakeDecoder(), FakeClock())
    return assets.put(PNG + marker.encode()).sha256


def _detail(ordinal: int, sha256: str) -> ImageReference:
    return ImageReference(
        role=ImageRole.DETAIL,
        ordinal=ordinal,
        host="img.shop.example",
        provenance=f".detail img:nth-of-type({ordinal})",
        status=FieldStatus.CONFIRMED,
        sha256=sha256,
    )


def _collect(client: TestClient, config: AppConfig, source_id: str, *shas: str) -> str:
    """One collected product of the supplier showing these detail images; its revision id."""
    images = tuple(_detail(ordinal, sha) for ordinal, sha in enumerate(shas, start=1))
    _run, revision = Collections.of(_services(client), config).collect(
        product(), source_product_id=source_id, extra_images=images
    )
    return revision.revision_id


def _verdicts(client: TestClient, supplier: str, *shas: str) -> dict[str, CommonImageVerdict]:
    with _services(client).db.read() as session:
        return dict(verdicts(session, supplier, shas))


def test_a_file_repeated_in_three_products_is_a_review_candidate(
    client: TestClient, config: AppConfig
) -> None:
    repeated = _stored(client, config, "repeated")
    twice = _stored(client, config, "twice")
    for source_id in ("p1", "p2"):
        _collect(client, config, source_id, repeated, twice)
    assert _verdicts(client, SUPPLIER, repeated, twice) == {}
    # A second revision of the same product is still one product.
    _collect(client, config, "p2", repeated)
    assert _verdicts(client, SUPPLIER, repeated) == {}
    _collect(client, config, "p3", repeated)
    assert DETECTION_MIN_PRODUCTS == 3
    assert _verdicts(client, SUPPLIER, repeated, twice) == {repeated: CommonImageVerdict.REVIEW}
    listed = client.get(f"/api/v1/products/supplier-common-images/{SUPPLIER}").json()
    candidate = next(i for i in listed["images"] if i["sha256"] == repeated)
    assert candidate == {
        "sha256": repeated,
        "verdict": "REVIEW",
        "decided": False,
        "product_count": 3,
        "decision": None,
    }


def test_the_owner_seed_applies_until_an_operator_decides(
    client: TestClient, config: AppConfig
) -> None:
    assert len(OWNER_SEED) == 8
    assert sum(v is CommonImageVerdict.BLOCK for v, _ in OWNER_SEED.values()) == 6
    assert _verdicts(client, "kmretail", SHIPPING_NOTICE, BRAND_BANNER) == {
        SHIPPING_NOTICE: CommonImageVerdict.BLOCK,
        BRAND_BANNER: CommonImageVerdict.KEEP,
    }
    # Another supplier's same bytes are another image.
    assert _verdicts(client, "another-supplier", SHIPPING_NOTICE) == {}


def test_an_operator_decision_is_remembered_appended_and_audited(
    client: TestClient, config: AppConfig
) -> None:
    repeated = _stored(client, config, "repeated")
    for source_id in ("p1", "p2", "p3"):
        _collect(client, config, source_id, repeated)
    path = f"/api/v1/products/supplier-common-images/{SUPPLIER}/{repeated}"
    kept = client.post(path, json={"verdict": "KEEP", "actor": "operator"}, headers=CLIENT)
    assert kept.status_code == 200, kept.text
    assert kept.json()["verdict"] == "KEEP" and kept.json()["revision_no"] == 1
    # The same verdict again is the same decision.
    again = client.post(path, json={"verdict": "KEEP", "actor": "operator"}, headers=CLIENT)
    assert again.json()["decision_id"] == kept.json()["decision_id"]
    blocked = client.post(
        path, json={"verdict": "BLOCK", "actor": "operator", "reason": "판매 경고"}, headers=CLIENT
    )
    assert blocked.json()["revision_no"] == 2 and blocked.json()["reason"] == "판매 경고"
    assert _verdicts(client, SUPPLIER, repeated) == {repeated: CommonImageVerdict.BLOCK}
    assert count(config, "supplier_common_image_decisions") == 2
    assert count(config, "audit_events", "event_type = ?", "SUPPLIER_COMMON_IMAGE_DECIDED") == 2


def test_an_operator_decision_supersedes_the_owner_seed(
    client: TestClient, config: AppConfig
) -> None:
    services = _services(client)
    with services.db.write() as session:
        session.add(
            SupplierCommonImageDecision(
                decision_id=str(uuid.uuid4()),
                supplier_key="kmretail",
                sha256=SHIPPING_NOTICE,
                revision_no=1,
                verdict="KEEP",
                reason=None,
                decided_by="operator",
                correlation_id="cid-decision",
                created_at=FakeClock().now(),
            )
        )
    assert _verdicts(client, "kmretail", SHIPPING_NOTICE, BRAND_BANNER) == {
        SHIPPING_NOTICE: CommonImageVerdict.KEEP,
        BRAND_BANNER: CommonImageVerdict.KEEP,
    }
    _collect(client, config, "p1", _stored(client, config, "own"))
    listed = client.get("/api/v1/products/supplier-common-images/kmretail").json()["images"]
    by_sha = {image["sha256"]: image for image in listed}
    # The seed is listed as the owner's decision, the operator's own as theirs.
    assert by_sha[BRAND_BANNER]["decision"]["decided_by"] == "owner-decision-2026-10-03"
    assert by_sha[SHIPPING_NOTICE]["decision"]["decided_by"] == "operator"
    assert by_sha[SHIPPING_NOTICE]["verdict"] == "KEEP"


@pytest.mark.parametrize(
    ("supplier", "body", "status", "code"),
    [
        ("never-collected", {"verdict": "BLOCK"}, 404, "PRODUCTS_COMMON_IMAGE_SUPPLIER_UNKNOWN"),
        ("icbm-synthetic", {"verdict": "BLOCK"}, 422, "PRODUCTS_COMMON_IMAGE_SUPPLIER_SYNTHETIC"),
        (SUPPLIER, {"verdict": "REVIEW"}, 422, "PRODUCTS_COMMON_IMAGE_VERDICT_INVALID"),
        (SUPPLIER, {"verdict": "BLOCK"}, 404, "PRODUCTS_COMMON_IMAGE_UNSEEN"),
    ],
)
def test_a_decision_is_refused_unless_it_names_a_file_the_supplier_showed(
    client: TestClient,
    config: AppConfig,
    supplier: str,
    body: dict[str, Any],
    status: int,
    code: str,
) -> None:
    _collect(client, config, "p1", _stored(client, config, "own"))
    unseen = "f" * 64
    response = client.post(
        f"/api/v1/products/supplier-common-images/{supplier}/{unseen}",
        json={**body, "actor": "operator"},
        headers=CLIENT,
    )
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert count(config, "supplier_common_image_decisions") == 0


def test_a_synthetic_copy_reads_its_template_supplier_and_never_counts(
    client: TestClient, config: AppConfig
) -> None:
    repeated = _stored(client, config, "repeated")
    template = _collect(client, config, "p1", repeated)
    _collect(client, config, "p2", repeated)
    services = _services(client)
    for label in ("[테스트] 테스트상품1", "[테스트] 테스트상품2"):
        services.synthetic_products.create(
            template_revision_id=template,
            label=label,
            reason=None,
            actor="operator",
            correlation_id="cid-synthetic",
        )
    # Two collected products and two copies: still only two products of the supplier.
    assert _verdicts(client, SUPPLIER, repeated) == {}
    with services.db.read() as session:
        copies = (
            session.query(ProductFactsRevision)
            .filter(ProductFactsRevision.supplier_key == "icbm-synthetic")
            .all()
        )
        assert copies and all(effective_supplier(session, copy) == SUPPLIER for copy in copies)


def test_a_shown_file_previews_as_its_stored_bytes_and_nothing_else_does(
    client: TestClient, config: AppConfig
) -> None:
    """Issue #231: the read-only preview of one file a supplier has shown."""
    shown = _stored(client, config, "shown-once")
    unseen = _stored(client, config, "stored-but-never-shown")
    _collect(client, config, "p1", shown)
    other = "other-supplier"
    tables = ("supplier_common_image_decisions", "audit_events", "source_assets")
    before = {table: count(config, table) for table in tables}
    path = "/api/v1/products/supplier-common-images"
    preview = client.get(f"{path}/{SUPPLIER}/{shown}/image")
    assert preview.status_code == 200, preview.text
    assert preview.content == PNG + b"shown-once"
    assert preview.headers["content-type"].startswith("image/png")
    assert preview.headers["cache-control"] == "no-store"
    assert preview.headers["x-content-type-options"] == "nosniff"
    # Shown once is enough to preview: the verdict (here none) does not matter.
    refused = {
        f"{path}/{SUPPLIER}/{unseen}/image": "PRODUCTS_COMMON_IMAGE_UNSEEN",
        f"{path}/{SUPPLIER}/{'0' * 64}/image": "PRODUCTS_COMMON_IMAGE_UNSEEN",
        f"{path}/{SUPPLIER}/{shown.upper()}/image": "PRODUCTS_COMMON_IMAGE_SHA_INVALID",
        f"{path}/{SUPPLIER}/{shown[:-1]}/image": "PRODUCTS_COMMON_IMAGE_SHA_INVALID",
        f"{path}/{other}/{shown}/image": "PRODUCTS_COMMON_IMAGE_SUPPLIER_UNKNOWN",
        f"{path}/icbm-synthetic/{shown}/image": "PRODUCTS_COMMON_IMAGE_SUPPLIER_SYNTHETIC",
    }
    for url, code in refused.items():
        answer = client.get(url)
        assert answer.status_code in (404, 422), (url, answer.status_code)
        assert answer.json()["error"]["code"] == code, url
    # Read-only: nothing was written.
    assert {table: count(config, table) for table in tables} == before


def test_a_configured_supplier_that_collected_nothing_has_no_common_images(
    client: TestClient,
) -> None:
    """The 수집관리 supplier card reads the list on a fresh install: a configured supplier
    with no collection yet answers an empty list, never "unknown"; an unconfigured one is still
    unknown."""
    configured = _services(client).collection.supplier_keys()
    assert configured
    for key in configured:
        listed = client.get(f"/api/v1/products/supplier-common-images/{key}")
        assert listed.status_code == 200, (key, listed.text)
        assert listed.json()["images"] == []
    unknown = client.get("/api/v1/products/supplier-common-images/not-a-configured-supplier")
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "PRODUCTS_COMMON_IMAGE_SUPPLIER_UNKNOWN"
