"""M6.5-A fulfillment routes (ADR-0025 §3, §4, §8): the documented carriers are served, and an
order ICBM does not hold is refused. Nothing reaches a provider or a supplier."""

import pytest
from fastapi.testclient import TestClient

from integrations.marketplaces.smartstore.delivery_companies import DELIVERY_COMPANIES

pytestmark = pytest.mark.integration


def test_the_routes_serve_carriers_and_refuse_an_unknown_order(client: TestClient) -> None:
    headers = {"X-ICBM-Client": "icbm-web"}
    carriers = client.get("/api/v1/operate/carriers").json()["carriers"]
    assert {"code": "CJGLS", "name": "CJ대한통운"} in carriers
    assert len(carriers) == len(DELIVERY_COMPANIES)
    assert client.get("/api/v1/operate/orders/po-x/fulfillment").status_code == 404
    response = client.put(
        "/api/v1/operate/orders/po-x/supplier-order",
        json={"supplier_order_ref": "KM-1", "purchase_amount": 1000},
        headers=headers,
    )
    assert response.status_code == 404
    assert client.get("/api/v1/operate/orders").json()["orders"] == []
