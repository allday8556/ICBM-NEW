"""ADR-0038 O1a: a revision in a source currency fails closed in M4 until O2.

Nothing converts a CNY or JPY amount into won: the base and offer pricing paths name
``PRICING_SOURCE_CURRENCY_UNSUPPORTED`` before they read an amount, and the materializer opens no
QuantityOffer for such a revision. The AtomicSKU path is covered beside its own economics tests.
"""

import contextlib
from dataclasses import replace

import pytest

from app.config import AppConfig
from app.container import Container
from app.stages.collect.facts import (
    FieldFact,
    ForeignPrice,
    ForeignPricesValue,
    ForeignQuantityTier,
    ForeignQuantityTiersValue,
    ForeignShippingValue,
    QuantityTier,
    QuantityTiersValue,
    ShippingKind,
)
from app.stages.collect.revisions import ProductFactsRevisionStore, StoredRevision
from app.stages.products import pricing_service
from app.stages.products.materialization import BaseProductEvidence, MaterializationStatus
from app.stages.products.model import ReadinessStatus, Reason
from app.stages.products.pricing import SOURCE_CURRENCY_UNSUPPORTED
from app.stages.products.pricing_service import PricingOutcome
from app.stages.products.quantity import QuantityOfferEvidence
from tests.support.collect_support import confirmed
from tests.support.product_support import Collections, context, product, raw

pytestmark = pytest.mark.integration

UNSUPPORTED = Reason(SOURCE_CURRENCY_UNSUPPORTED, ReadinessStatus.REVIEW_REQUIRED, "prices")


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


def _cny(**overrides: FieldFact) -> dict[str, FieldFact]:
    """A base product whose page states CNY: one price and FREE shipping, no options or tiers."""
    return product(
        prices=confirmed(
            ForeignPricesValue(prices=(ForeignPrice(label="价格", currency="CNY", amount="5.80"),)),
            ".price",
        ),
        shipping=confirmed(
            ForeignShippingValue(kind=ShippingKind.FREE, policy_text="包邮", currency="CNY"),
            ".delivery",
        ),
        **overrides,
    )


def _offers(config: AppConfig) -> int:
    with contextlib.closing(raw(config)) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM quantity_offers").fetchone()[0])


def test_a_base_product_in_a_source_currency_is_not_priced(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    run_id, revision = sources.collect(_cny())
    assert revision.source_currency == "CNY" and revision.currency == "KRW"
    result = container.materializer.materialize_run(run_id)
    # The Item and its binding are procurement, not price: they exist, and pricing refuses.
    assert result.base_product is BaseProductEvidence.PROVEN and result.item_id is not None
    ctx = context()
    priced = container.pricing.price(result.item_id, ctx)
    assert priced.outcome is PricingOutcome.NOT_PRICED and priced.reasons == (UNSUPPORTED,)
    evaluation = container.pricing.evaluate(result.item_id, ctx)
    assert evaluation.inputs is None and evaluation.calculation is None
    readiness = container.product_readiness.pricing_readiness(result.item_id, ctx)
    assert readiness.status is ReadinessStatus.REVIEW_REQUIRED
    assert (SOURCE_CURRENCY_UNSUPPORTED, "prices") in [
        (r.code, r.subject) for r in readiness.reasons
    ]
    with contextlib.closing(raw(config)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pricing_snapshots").fetchone() == (0,)


def test_tiers_in_a_source_currency_open_no_quantity_offer(
    container: Container, config: AppConfig, sources: Collections
) -> None:
    tiers = confirmed(
        ForeignQuantityTiersValue(
            tiers=(
                ForeignQuantityTier(quantity=1, currency="CNY", total_price="5.80"),
                ForeignQuantityTier(quantity=90, currency="CNY", total_price="477.00"),
            )
        ),
        ".tiers",
    )
    run_id, revision = sources.collect(_cny(quantity_tiers=tiers))
    assert revision.source_currency == "CNY"
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED
    assert result.quantity_offers is QuantityOfferEvidence.NOT_PROVEN
    assert result.quantity_offer_ids == () and _offers(config) == 0


def test_the_offer_path_reads_no_amount_of_a_source_currency_revision(
    container: Container, config: AppConfig, sources: Collections, monkeypatch: pytest.MonkeyPatch
) -> None:
    tiers = confirmed(
        QuantityTiersValue(
            tiers=(
                QuantityTier(quantity=1, total_price_krw=19900),
                QuantityTier(quantity=2, total_price_krw=37900),
            )
        ),
        ".tiers",
    )
    run_id, _revision = sources.collect(product(quantity_tiers=tiers))
    result = container.materializer.materialize_run(run_id)
    assert result.quantity_offers is QuantityOfferEvidence.PRODUCT_LEVEL
    readback = container.products.product(str(result.product_group_id))
    pair = next(item for item in readback.items if item.composition.quantity == 2)
    assert pair.current_binding is not None and pair.current_binding.quantity_offer_id is not None
    # The bound revision as a source-currency revision reads back: no offer is ever opened on one
    # (above), so the read is what changes here.
    read = ProductFactsRevisionStore.get

    def as_cny(self: ProductFactsRevisionStore, revision_id: str) -> StoredRevision | None:
        stored = read(self, revision_id)
        return None if stored is None else replace(stored, source_currency="CNY")

    def no_amount_is_read(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an amount of a source-currency revision was read")

    monkeypatch.setattr(ProductFactsRevisionStore, "get", as_cny)
    monkeypatch.setattr(pricing_service, "offer_source_inputs", no_amount_is_read)
    monkeypatch.setattr(pricing_service, "source_inputs", no_amount_is_read)
    priced = container.pricing.price(pair.item_id, context())
    assert priced.outcome is PricingOutcome.NOT_PRICED and priced.reasons == (UNSUPPORTED,)
    with contextlib.closing(raw(config)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pricing_snapshots").fetchone() == (0,)
