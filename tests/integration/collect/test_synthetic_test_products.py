"""An operator's synthetic test product (owner decision 2026-10-03).

A copy of one collected revision's facts, unchanged, under the reserved ``icbm-synthetic``
namespace, labelled durably before anything else of it exists. It materializes like a collected
product; nothing of it is invented, and no acquisition entry point can write into its namespace.
"""

import contextlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import InputValidationError, NotFoundError
from app.stages.collect.models import SYNTHETIC_SUPPLIER_KEY
from tests.support.product_support import Collections, product, raw

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
CID = "cid-synthetic"


def _template(container: Container, config: AppConfig) -> str:
    run_id, revision = Collections.of(container, config).collect(product())
    container.materializer.materialize_run(run_id)
    return revision.revision_id


def _create(container: Container, template: str, label: str = "테스트상품1") -> object:
    return container.synthetic_products.create(
        template_revision_id=template,
        label=label,
        reason="canary test listing",
        actor="operator",
        correlation_id=CID,
    )


def test_a_copy_keeps_every_fact_and_materializes_under_the_reserved_namespace(
    container: Container, config: AppConfig
) -> None:
    template_id = _template(container, config)
    record = _create(container, template_id)
    assert record.supplier_key == SYNTHETIC_SUPPLIER_KEY
    template = container.revisions.get(template_id)
    copy = container.revisions.latest(SYNTHETIC_SUPPLIER_KEY, record.source_product_id)
    assert template is not None and copy is not None
    # Nothing is invented: the same facts, the same images, so the same source fingerprint.
    assert copy.source_fingerprint == template.source_fingerprint
    assert copy.fingerprints_intact()
    assert copy.images == template.images
    assert {k: f.value_json for k, f in copy.fields.items()} == {
        k: f.value_json for k, f in template.fields.items()
    }
    # It is its own Product, and the collected one is untouched.
    view = container.products.product_of_source(SYNTHETIC_SUPPLIER_KEY, record.source_product_id)
    original = container.products.product_of_source(
        template.supplier_key, template.source_product_id
    )
    assert view.product_group_id != original.product_group_id
    # No document was acquired, so the copy's run states no transport.
    with contextlib.closing(raw(config)) as connection:
        (kind,) = connection.execute(
            "SELECT transport_kind FROM collection_runs WHERE collection_run_id = ?",
            (record.collection_run_id,),
        ).fetchone()
    assert kind is None and copy.transport_kind is None


def test_the_label_is_durable_and_audited(container: Container, config: AppConfig) -> None:
    record = _create(container, _template(container, config))
    (listed,) = container.synthetic_products.list()
    assert listed == record and listed.label == "테스트상품1"
    events = [
        e
        for e in container.audit.list_events(limit=50)
        if e.action == "create_synthetic_test_product"
    ]
    assert len(events) == 1 and events[0].details["label"] == "테스트상품1"


def test_refusals(container: Container, config: AppConfig) -> None:
    template_id = _template(container, config)
    copy = _create(container, template_id)
    with pytest.raises(InputValidationError, match="already used"):
        _create(container, template_id)
    with pytest.raises(InputValidationError):
        _create(container, template_id, label="x" * 41)
    with pytest.raises(NotFoundError):
        _create(container, "no-such-revision", label="테스트상품2")
    # A copy of a copy is refused: a test product copies a collected revision.
    synthetic = container.revisions.latest(SYNTHETIC_SUPPLIER_KEY, copy.source_product_id)
    assert synthetic is not None
    with pytest.raises(InputValidationError) as refused:
        _create(container, synthetic.revision_id, label="테스트상품3")
    assert refused.value.code == "SYNTHETIC_TEMPLATE_IS_SYNTHETIC"


def test_the_label_table_is_append_only_and_bound_to_the_namespace(
    container: Container, config: AppConfig
) -> None:
    _create(container, _template(container, config))
    with contextlib.closing(raw(config)) as connection:
        for sql in (
            "UPDATE synthetic_test_products SET label = 'other'",
            "DELETE FROM synthetic_test_products",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(sql)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO synthetic_test_products SELECT 'x', 'kmretail', source_product_id,"
                " template_revision_id, template_supplier_key, template_source_product_id,"
                " collection_run_id, 'other', reason, created_by, correlation_id, created_at"
                " FROM synthetic_test_products"
            )


def test_no_acquisition_entry_point_writes_into_the_namespace(client: TestClient) -> None:
    response = client.post(
        "/api/v1/collect/collections",
        json={
            "supplier_key": SYNTHETIC_SUPPLIER_KEY,
            "product_url": "https://example.com/product/1",
        },
        headers=CLIENT,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "COLLECT_SUPPLIER_UNKNOWN"


def test_the_route_creates_and_lists(client: TestClient, config: AppConfig) -> None:
    services: Container = client.app.state.container  # type: ignore[attr-defined]
    template_id = _template(services, config)
    created = client.post(
        "/api/v1/collect/synthetic-test-products",
        json={"template_revision_id": template_id, "label": "테스트상품1", "actor": "operator"},
        headers=CLIENT,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["supplier_key"] == SYNTHETIC_SUPPLIER_KEY and body["label"] == "테스트상품1"
    listed = client.get("/api/v1/collect/synthetic-test-products").json()
    assert [entry["label"] for entry in listed] == ["테스트상품1"]
    product = client.get(
        f"/api/v1/products/by-source/{SYNTHETIC_SUPPLIER_KEY}/{body['source_product_id']}"
    )
    assert product.status_code == 200
