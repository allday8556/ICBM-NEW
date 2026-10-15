"""The latest recorded price of each Product, per marketplace, for the Product DB screen.

A display read over M4's pricing snapshots: the newest one of each Product for each marketplace,
amounts exactly as stored. It prices nothing and records nothing.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import InputValidationError
from app.stages.products.latest_prices import MAX_PRODUCTS, LatestPrices
from tests.support.product_support import Collections, context, count, fixed, product

pytestmark = pytest.mark.integration


def _item(container: Container, config: AppConfig, shipping: int = 0) -> str:
    fields = product(shipping=fixed(shipping)) if shipping else None
    run_id, _revision = Collections.of(container, config).collect(fields)
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None, result
    return result.item_id


def test_the_newest_snapshot_per_marketplace_is_read_as_recorded(
    container: Container, config: AppConfig
) -> None:
    item = _item(container, config, shipping=3000)
    snapshot = container.pricing.price(item, context()).snapshot
    assert snapshot is not None
    product_group_id = snapshot.product_group_id
    before = count(config, "pricing_snapshots")
    (latest,) = LatestPrices(container.product_store).latest([product_group_id])
    assert count(config, "pricing_snapshots") == before  # a read records nothing
    assert (latest.product_group_id, latest.marketplace_key, latest.item_id) == (
        product_group_id,
        snapshot.marketplace_key,
        item,
    )
    assert (latest.purchase_cost_krw, latest.supplier_shipping_krw) == (10000, 3000)
    assert latest.final_sale_price_krw == snapshot.final_sale_price_krw
    assert latest.price_basis == snapshot.price_basis.value
    assert latest.minimum_sale_price_krw is None and latest.source_currency is None


def test_a_product_without_a_snapshot_reads_nothing_and_the_read_is_bounded(
    container: Container, config: AppConfig
) -> None:
    _item(container, config)
    assert LatestPrices(container.product_store).latest([]) == []
    with pytest.raises(InputValidationError):
        LatestPrices(container.product_store).latest([str(n) for n in range(MAX_PRODUCTS + 1)])


def test_the_route_is_a_read(client: TestClient) -> None:
    assert client.get("/api/v1/pricing/latest?product_group_id=unknown").json() == []
    assert client.get("/api/v1/pricing/latest").json() == []
