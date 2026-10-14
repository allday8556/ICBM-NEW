"""ADR-0039: the per-supplier sales policy changes enforcement only; its default changes nothing.

Pure rules: the channel verdict under a policy that does not apply restrictions (§2), the pricing
inputs under a policy that does not apply the minimum (§3), and the fingerprints and pins that must
stay exactly as they were for a supplier on the defaults (SP-02, SP-03).
"""

import pytest

from app.stages.collect.facts import (
    Evidence,
    EvidenceKind,
    FieldFact,
    FieldStatus,
    MoneyValue,
    QuantityTier,
    QuantityTiersValue,
    SalesChannelScope,
    SalesChannelsValue,
)
from app.stages.products.pricing import (
    DEPENDENCY_VERSION,
    MINIMUM_SALE_PRICE_NOT_OFFER_BOUND,
    MINIMUM_SALE_PRICE_UNRESOLVED,
    PRICING_RULE_VERSION,
    PriceBasis,
    PricingDependencies,
    SourceInputs,
    calculate,
    digest,
    offer_source_inputs,
    source_inputs,
)
from app.stages.products.quantity import OfferTerms
from app.stages.products.sales_policy import SalesPolicy, content_fingerprint
from app.stages.products.source_gates import (
    SOURCE_CHANNEL_FORBIDDEN,
    SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED,
    SOURCE_CHANNEL_UNRESOLVED,
    SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED,
    SOURCE_GATE_CODES,
    SOURCE_GATE_WARNINGS,
    channel_verdict,
)
from tests.support.collect_support import confirmed
from tests.support.product_support import context, minimum, product, review


def channels(scope: SalesChannelScope, *forbidden: str) -> SalesChannelsValue:
    return SalesChannelsValue(scope=scope, forbidden=forbidden, policy_text="공급사 문구")


# ---------------------------------------------------------------- §2 channel restrictions


@pytest.mark.parametrize(
    ("status", "reading", "applied", "not_applied"),
    [
        (FieldStatus.ABSENT, None, None, None),
        (FieldStatus.CONFIRMED, channels(SalesChannelScope.ALL_ALLOWED), None, None),
        (
            FieldStatus.CONFIRMED,
            channels(SalesChannelScope.CLOSED_MALL_ONLY),
            SOURCE_CHANNEL_FORBIDDEN,
            SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED,
        ),
        (
            FieldStatus.CONFIRMED,
            channels(SalesChannelScope.LISTED, "smartstore"),
            SOURCE_CHANNEL_FORBIDDEN,
            SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED,
        ),
        (FieldStatus.CONFIRMED, channels(SalesChannelScope.LISTED, "coupang"), None, None),
        (
            FieldStatus.REVIEW_REQUIRED,
            None,
            SOURCE_CHANNEL_UNRESOLVED,
            SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED,
        ),
    ],
)
def test_the_adr_0039_channel_table(
    status: FieldStatus, reading: object, applied: str | None, not_applied: str | None
) -> None:
    assert channel_verdict(status, reading, "smartstore") == applied
    assert channel_verdict(status, reading, "smartstore", apply_restrictions=True) == applied
    assert channel_verdict(status, reading, "smartstore", apply_restrictions=False) == not_applied


def test_the_two_warnings_are_known_gates_and_nothing_else_is_a_warning() -> None:
    assert {
        SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED,
        SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED,
    } == SOURCE_GATE_WARNINGS
    assert SOURCE_GATE_WARNINGS < SOURCE_GATE_CODES
    assert SOURCE_CHANNEL_FORBIDDEN not in SOURCE_GATE_WARNINGS


# ---------------------------------------------------------------- §3 the minimum sale price


def test_the_default_reads_the_minimum_exactly_as_before() -> None:
    fields = product(price=10000, minimum_sale_price=minimum(50000))
    applied = source_inputs(fields)
    assert applied == source_inputs(fields, apply_minimum=True)
    assert applied == SourceInputs(10000, 0, 50000)
    assert isinstance(applied, SourceInputs) and not applied.minimum_waived


def test_a_minimum_not_applied_is_no_input_and_is_kept_beside_the_price() -> None:
    inputs = source_inputs(
        product(price=10000, minimum_sale_price=minimum(50000)), apply_minimum=False
    )
    assert isinstance(inputs, SourceInputs)
    assert inputs.minimum_sale_price_krw is None
    assert (inputs.minimum_applied, inputs.stated_minimum_sale_price_krw) == (False, 50000)
    assert inputs.minimum_waived
    calculation = calculate(inputs, context())
    assert not isinstance(calculation, type(None))
    assert calculation.price_basis is PriceBasis.TARGET_MARGIN  # type: ignore[union-attr]
    assert calculation.final_sale_price_krw < 50000  # type: ignore[union-attr]


def test_an_unread_minimum_blocks_only_while_it_is_applied() -> None:
    fields = product(minimum_sale_price=review(".minimum-price"))
    applied = source_inputs(fields)
    assert isinstance(applied, tuple)
    assert [reason.code for reason in applied] == [MINIMUM_SALE_PRICE_UNRESOLVED]
    waived = source_inputs(fields, apply_minimum=False)
    assert isinstance(waived, SourceInputs)
    assert waived.minimum_sale_price_krw is None and waived.stated_minimum_sale_price_krw is None
    assert waived.minimum_unresolved and waived.minimum_waived


@pytest.mark.parametrize(
    "stated",
    [
        FieldFact(
            FieldStatus.CONFIRMED,
            MoneyValue(label="minimum", amount_krw=0),
            (Evidence(EvidenceKind.DOM_TEXT, ".minimum-price", FieldStatus.CONFIRMED),),
        ),
        None,
    ],
)
def test_a_minimum_no_rule_can_read_is_unresolved_when_not_applied(
    stated: FieldFact | None,
) -> None:
    fields = product()
    if stated is None:
        del fields["minimum_sale_price"]
    else:
        fields["minimum_sale_price"] = stated
    waived = source_inputs(fields, apply_minimum=False)
    assert isinstance(waived, SourceInputs)
    assert waived.stated_minimum_sale_price_krw is None and waived.minimum_unresolved


def test_an_absent_minimum_has_nothing_to_waive() -> None:
    waived = source_inputs(product(), apply_minimum=False)
    assert isinstance(waived, SourceInputs)
    assert not waived.minimum_applied and not waived.minimum_waived


def test_a_minimum_naming_no_offer_stops_an_offer_only_while_applied() -> None:
    tiers = confirmed(
        QuantityTiersValue(
            tiers=(QuantityTier(quantity=2, total_price_krw=19000, label="tier-2"),)
        ),
        ".tiers",
    )
    fields = product(minimum_sale_price=minimum(50000), quantity_tiers=tiers)
    offer = OfferTerms(tier_ordinal=0, quantity=2, total_price_krw=19000)
    applied = offer_source_inputs(fields, offer, "KRW")
    assert isinstance(applied, tuple)
    assert [reason.code for reason in applied] == [MINIMUM_SALE_PRICE_NOT_OFFER_BOUND]
    waived = offer_source_inputs(fields, offer, "KRW", apply_minimum=False)
    assert isinstance(waived, SourceInputs)
    assert (waived.purchase_cost_krw, waived.minimum_sale_price_krw) == (19000, None)
    assert waived.stated_minimum_sale_price_krw == 50000 and waived.minimum_waived


# ---------------------------------------------------------------- SP-02, SP-03 pins


DEPENDENCIES = PricingDependencies(
    item_id="i-1",
    product_group_id="g-1",
    composition_signature="c" * 64,
    signature_version="sig-1",
    membership_revision_id="m-1",
    source_binding_id="b-1",
    binding_kind="BASE_PRODUCT",
    quantity_offer_id=None,
    fulfillment_quantity=1,
    source_revision_id="r-1",
)


def test_the_default_dependency_fingerprint_is_the_one_before_adr_0039() -> None:
    pricing = context()
    before = digest(
        {
            "version": DEPENDENCY_VERSION,
            "pricing_rule_version": PRICING_RULE_VERSION,
            "item_id": "i-1",
            "product_group_id": "g-1",
            "composition_signature": "c" * 64,
            "signature_version": "sig-1",
            "membership_revision_id": "m-1",
            "source_binding_id": "b-1",
            "binding_kind": "BASE_PRODUCT",
            "quantity_offer_id": None,
            "fulfillment_quantity": 1,
            "source_revision_id": "r-1",
            "pricing_context_fingerprint": pricing.fingerprint,
            "fee_table_version": pricing.fee_table_version,
            "pricing_policy_version": pricing.pricing_policy_version,
        }
    )
    assert DEPENDENCIES.fingerprint(pricing) == before
    waived = PricingDependencies(**{**DEPENDENCIES.__dict__, "minimum_applied": False})
    assert waived.fingerprint(pricing) != before


def test_a_supplier_on_the_defaults_pins_nothing() -> None:
    assert SalesPolicy("kmretail").pin() is None
    configured = SalesPolicy(
        "kmretail", apply_channel_restrictions=False, revision_no=2, fingerprint="f" * 64
    )
    assert configured.pin() == ("kmretail", 2, "f" * 64)


def test_the_content_fingerprint_names_the_supplier_and_both_switches() -> None:
    one = content_fingerprint("kmretail", True, False)
    assert len(one) == 64
    assert one == content_fingerprint("kmretail", True, False)
    assert len({one, content_fingerprint("kmretail", False, True)}) == 2
    assert one != content_fingerprint("other", True, False)
