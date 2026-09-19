"""Product-level quantity offers and their pricing inputs, pure (Issue #80 PR-Q kickoff 5738854211;
rulings 5737762202 and 5738760913).

No database: every field is an invented source fact.
"""

import dataclasses

import pytest

from app.collect.facts import CURRENCY, FieldFact, OptionsValue, QuantityTier, QuantityTiersValue
from app.products.model import Reason
from app.products.pricing import (
    DEPENDENCY_VERSION,
    MINIMUM_SALE_PRICE_NOT_OFFER_BOUND,
    MINIMUM_SALE_PRICE_UNRESOLVED,
    QUANTITY_OFFER_UNRESOLVED,
    SHIPPING_CONDITIONAL,
    PricingDependencies,
    SourceInputs,
    offer_source_inputs,
    source_inputs,
)
from app.products.quantity import OfferTerms, QuantityOfferEvidence, product_level_offers
from tests.collect_support import absent, confirmed
from tests.product_support import conditional, context, fixed, minimum, product, review

CANONICAL = ((1, 19900), (2, 37900), (3, 53900))
TERMS = (OfferTerms(0, 1, 19900), OfferTerms(1, 2, 37900), OfferTerms(2, 3, 53900))


def tiers(*pairs: tuple[int, int]) -> FieldFact:
    return confirmed(
        QuantityTiersValue(
            tiers=tuple(QuantityTier(quantity=q, total_price_krw=t, label=f"{q}") for q, t in pairs)
        ),
        ".tiers",
    )


def tiered(*pairs: tuple[int, int], **overrides: object) -> dict[str, FieldFact]:
    return product(quantity_tiers=tiers(*(pairs or CANONICAL)), **overrides)  # type: ignore[arg-type]


def codes(result: SourceInputs | tuple[Reason, ...]) -> set[str]:
    assert isinstance(result, tuple), result
    return {reason.code for reason in result}


def test_confirmed_tiers_with_no_options_are_product_level_offers() -> None:
    assert product_level_offers(tiered(), CURRENCY) == (QuantityOfferEvidence.PRODUCT_LEVEL, TERMS)


def test_source_order_is_kept_and_nothing_is_sorted_or_derived() -> None:
    evidence, terms = product_level_offers(tiered((3, 53900), (1, 19900)), CURRENCY)
    assert evidence is QuantityOfferEvidence.PRODUCT_LEVEL
    assert terms == (OfferTerms(0, 3, 53900), OfferTerms(1, 1, 19900))


@pytest.mark.parametrize(
    "options",
    [absent(".options"), confirmed(OptionsValue(axes=()), ".options"), review(".options")],
    ids=["options absent", "options stated", "options under review"],
)
def test_absent_tiers_state_no_offer_whatever_the_options(options: FieldFact) -> None:
    assert product_level_offers(product(options=options), CURRENCY) == (
        QuantityOfferEvidence.NONE_STATED,
        (),
    )


NOT_PROVEN = {
    "tiers under review": product(quantity_tiers=review(".tiers")),
    "options stated": tiered(options=confirmed(OptionsValue(axes=()), ".options")),
    "options under review": tiered(options=review(".options")),
    "no tiers field": {k: v for k, v in tiered().items() if k != "quantity_tiers"},
    "no options field": {k: v for k, v in tiered().items() if k != "options"},
}


@pytest.mark.parametrize("fields", NOT_PROVEN.values(), ids=NOT_PROVEN.keys())
def test_anything_else_is_never_read_as_an_offer(fields: dict[str, FieldFact]) -> None:
    assert product_level_offers(fields, CURRENCY) == (QuantityOfferEvidence.NOT_PROVEN, ())


def test_another_currency_is_never_an_offer() -> None:
    assert product_level_offers(tiered(), "USD") == (QuantityOfferEvidence.NOT_PROVEN, ())


@pytest.mark.parametrize("price", [(10000,), (1, 2, 3), (53900, 19900)])
def test_an_offer_costs_its_own_total_and_never_a_generic_price(price: tuple[int, ...]) -> None:
    fields = tiered(price=price)
    for term in TERMS:
        assert offer_source_inputs(fields, term, CURRENCY) == SourceInputs(
            term.total_price_krw, 0, None
        )


def test_the_base_product_reading_of_several_prices_is_unchanged() -> None:
    assert codes(source_inputs(tiered(price=(1, 2, 3)))) == {"PRICING_PURCHASE_PRICE_AMBIGUOUS"}
    assert source_inputs(product(price=12900)) == SourceInputs(12900, 0, None)


def test_offer_shipping_follows_the_same_rules() -> None:
    assert offer_source_inputs(tiered(shipping=fixed(3000)), TERMS[1], CURRENCY) == SourceInputs(
        37900, 3000, None
    )
    assert codes(offer_source_inputs(tiered(shipping=conditional()), TERMS[1], CURRENCY)) == {
        SHIPPING_CONDITIONAL
    }


def test_a_generic_minimum_names_no_offer() -> None:
    for stated in (minimum(15000), review(".minimum-price")):
        fields = tiered(minimum_sale_price=stated)
        for term in TERMS:
            assert codes(offer_source_inputs(fields, term, CURRENCY)) == {
                MINIMUM_SALE_PRICE_NOT_OFFER_BOUND
            }
    missing = {k: v for k, v in tiered().items() if k != "minimum_sale_price"}
    assert codes(offer_source_inputs(missing, TERMS[0], CURRENCY)) == {
        MINIMUM_SALE_PRICE_UNRESOLVED
    }


def test_an_offer_its_revision_does_not_state_exactly_is_unresolved() -> None:
    for term in (OfferTerms(1, 2, 39800), OfferTerms(2, 2, 37900), OfferTerms(3, 4, 69900)):
        assert codes(offer_source_inputs(tiered(), term, CURRENCY)) == {QUANTITY_OFFER_UNRESOLVED}
    under_review = product(quantity_tiers=review(".tiers"))
    assert codes(offer_source_inputs(under_review, TERMS[1], CURRENCY)) == {
        QUANTITY_OFFER_UNRESOLVED
    }


def test_the_dependency_fingerprint_names_the_binding_kind_offer_and_quantity() -> None:
    assert DEPENDENCY_VERSION == "pricing-dependency/v2"
    base = PricingDependencies(
        item_id="item",
        product_group_id="group",
        composition_signature="0" * 64,
        signature_version="composition-signature/v1",
        membership_revision_id="membership",
        source_binding_id="binding",
        binding_kind="SOURCE_OFFER",
        quantity_offer_id="offer",
        fulfillment_quantity=2,
        source_revision_id="revision",
    )
    ctx = context()
    changes: list[dict[str, object]] = [
        {"binding_kind": "BASE_PRODUCT", "quantity_offer_id": None, "fulfillment_quantity": 1},
        {"quantity_offer_id": "another offer"},
        {"fulfillment_quantity": 3},
    ]
    for change in changes:
        assert dataclasses.replace(base, **change).fingerprint(ctx) != base.fingerprint(ctx)  # type: ignore[arg-type]
