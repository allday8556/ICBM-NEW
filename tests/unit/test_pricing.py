"""The M4 pricing rule, pure (Issue #80 PR-D kickoff 5737440897 §B–§E; ADR-0013 §7).

Exact integer and rational arithmetic only: every boundary here is decided by an exact comparison,
never a float. No marketplace fee is known: every context is an invented placeholder.
"""

import dataclasses
from fractions import Fraction

import pytest

from app.collect.facts import FieldStatus
from app.products.pricing import (
    MINIMUM_NET_MARGIN,
    TARGET_NET_MARGIN,
    Calculation,
    GuardReason,
    PriceBasis,
    PriceGuard,
    PricingContextInput,
    PricingError,
    Rounding,
    SourceInputs,
    calculate,
    meets_target,
    rate,
    source_inputs,
    target_margin_price,
)
from tests.collect_support import absent
from tests.product_support import (
    conditional,
    context,
    fixed,
    minimum,
    product,
    review,
    unknown_shipping,
)


def inputs(purchase: int = 10000, shipping: int = 0, minimum: int | None = None) -> SourceInputs:
    return SourceInputs(purchase, shipping, minimum)


def priced(result: object) -> Calculation:
    assert isinstance(result, Calculation), result
    return result


# ---------------------------------------------------------------- the pricing context


def test_a_rate_is_exact_decimal_text_and_never_a_float() -> None:
    assert rate("0.055") == Fraction(55, 1000)
    assert rate("0.050") == rate("0.05")
    for bad in (0.055, "1", "1.5", "-0.1", "0.1e1", " 0.1", "", True):
        with pytest.raises(PricingError):
            rate(bad)  # type: ignore[arg-type]


def test_every_context_input_is_required_and_strictly_typed() -> None:
    # No default fee, policy or marketplace exists to fall back on.
    assert all(
        f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        for f in dataclasses.fields(PricingContextInput)
    )
    for bad in (
        {"fee_rate": 0.1},
        {"fee_fixed_krw": 1.5},
        {"fee_fixed_krw": True},
        {"other_cost_fixed_krw": -1},
        {"marketplace_key": ""},
        {"account_id": ""},
        {"fee_table_version": ""},
        {"cost_rounding": "CEIL_KRW_1"},
    ):
        with pytest.raises(PricingError):
            context(**bad)


def test_the_context_fingerprint_covers_every_effective_input() -> None:
    # Kickoff §11.11–13: marketplace, account, versions and effective values all distinguish.
    base = context()
    variants = [
        context(marketplace_key="market_b"),
        context(account_id="acct-1"),
        context(account_id="acct-2"),
        context(fee_table_version="fee-test-2"),
        context(pricing_policy_version="policy-test-2"),
        context(fee_rate="0.11"),
        context(fee_fixed_krw=1),
        context(other_cost_rate="0.01"),
        context(other_cost_fixed_krw=1),
    ]
    fingerprints = {base.fingerprint, *(v.fingerprint for v in variants)}
    assert len(fingerprints) == len(variants) + 1
    # Reusing a version label with different effective values can never alias.
    assert context(fee_rate="0.12").fingerprint != base.fingerprint
    # Equal effective values are one context, however the rate is written.
    assert context(fee_rate="0.10").fingerprint == base.fingerprint


# ---------------------------------------------------------------- source inputs (fail closed)


def test_one_confirmed_price_free_shipping_and_no_minimum_are_read_exactly() -> None:
    assert source_inputs(product(price=10000)) == SourceInputs(10000, 0, None)
    assert source_inputs(product(shipping=fixed(3000))) == SourceInputs(10000, 3000, None)
    assert source_inputs(product(minimum_sale_price=minimum(15000))) == SourceInputs(
        10000, 0, 15000
    )


def _codes(result: object) -> set[str]:
    assert isinstance(result, tuple), result
    return {reason.code for reason in result}


def test_several_source_prices_are_never_chosen_between() -> None:
    # Kickoff §11.7: not by label, name or order.
    assert _codes(source_inputs(product(price=(10000, 12000)))) == {
        "PRICING_PURCHASE_PRICE_AMBIGUOUS"
    }


@pytest.mark.parametrize(
    ("shipping", "code"),
    [
        (conditional(), "PRICING_SHIPPING_CONDITIONAL"),
        (unknown_shipping(), "PRICING_SHIPPING_UNRESOLVED"),
        (review(".delivery"), "PRICING_SHIPPING_UNRESOLVED"),
        (absent(".delivery"), "PRICING_SHIPPING_UNRESOLVED"),
    ],
    ids=["conditional", "unknown", "under review", "absent"],
)
def test_shipping_that_is_not_free_or_fixed_is_never_guessed(shipping: object, code: str) -> None:
    # Kickoff §11.8: no 3,000 KRW default, no threshold guess.
    assert _codes(source_inputs(product(shipping=shipping))) == {code}  # type: ignore[arg-type]


def test_a_minimum_sale_price_under_review_is_never_guessed() -> None:
    # Kickoff §11.9.
    fields = product(minimum_sale_price=review(".minimum-price"))
    assert _codes(source_inputs(fields)) == {"PRICING_MINIMUM_SALE_PRICE_UNRESOLVED"}


def test_every_unresolved_input_is_reported_together() -> None:
    fields = product(
        price=(1, 2), shipping=conditional(), minimum_sale_price=review(".minimum-price")
    )
    assert _codes(source_inputs(fields)) == {
        "PRICING_PURCHASE_PRICE_AMBIGUOUS",
        "PRICING_SHIPPING_CONDITIONAL",
        "PRICING_MINIMUM_SALE_PRICE_UNRESOLVED",
    }
    under_review = product(prices=review(".price"))
    assert _codes(source_inputs(under_review)) == {"PRICING_PURCHASE_PRICE_UNRESOLVED"}
    assert under_review["prices"].status is FieldStatus.REVIEW_REQUIRED


# ---------------------------------------------------------------- the canonical rule


def test_without_a_minimum_the_target_margin_price_is_final() -> None:
    # Kickoff §11.1.
    result = priced(calculate(inputs(), context()))
    assert result.price_basis is PriceBasis.TARGET_MARGIN
    assert result.final_sale_price_krw == result.target_margin_price_krw == 18184
    assert result.platform_fee_krw == 1819 and result.expected_net_profit_krw == 6365
    assert result.expected_net_margin >= TARGET_NET_MARGIN
    assert result.price_guard is PriceGuard.OK and result.guard_reasons == ()


def test_fixed_supplier_shipping_is_included_exactly() -> None:
    # Kickoff §11.2.
    free_price = priced(calculate(inputs(shipping=0), context()))
    with_fee = priced(calculate(inputs(shipping=3000), context()))
    assert with_fee.expected_net_profit_krw == (
        with_fee.final_sale_price_krw - 10000 - 3000 - with_fee.platform_fee_krw
    )
    assert with_fee.final_sale_price_krw > free_price.final_sale_price_krw
    assert meets_target(inputs(shipping=3000), context(), with_fee.final_sale_price_krw)


@pytest.mark.parametrize("stated", [15000, 20000], ids=["below target", "above target"])
def test_a_minimum_sale_price_is_final_exactly_never_a_maximum(stated: int) -> None:
    # Kickoff §11.3–4: the target is 18,184 KRW here. Below it, a maximum would have chosen the
    # target; the canonical rule takes the minimum exactly, either way.
    result = priced(calculate(inputs(minimum=stated), context()))
    assert result.target_margin_price_krw == 18184
    assert result.final_sale_price_krw == stated
    assert result.price_basis is PriceBasis.MINIMUM_SALE_PRICE


def test_a_minimum_sale_price_can_be_a_loss_and_every_guard_is_kept() -> None:
    # Kickoff §11.5: 10,000 + 0 + 1,050 >= 10,500. A loss is also below the minimum margin, and
    # that reason is not hidden behind the primary guard.
    result = priced(calculate(inputs(minimum=10500), context()))
    assert result.price_guard is PriceGuard.LOSS
    assert result.guard_reasons == (GuardReason.PRICE_LOSS, GuardReason.PRICE_BELOW_MIN_MARGIN)


def test_a_minimum_sale_price_can_be_below_the_minimum_margin() -> None:
    # Kickoff §11.6: 11,500 − 10,000 − 575 = 925, which is 8.04%: no loss, but below 10%.
    result = priced(calculate(inputs(minimum=11500), context(fee_rate="0.05")))
    assert (result.platform_fee_krw, result.expected_net_profit_krw) == (575, 925)
    assert result.price_guard is PriceGuard.BELOW_MIN_MARGIN
    assert result.guard_reasons == (GuardReason.PRICE_BELOW_MIN_MARGIN,)


def test_nothing_is_multiplied_by_a_quantity_or_divided_into_a_unit_price() -> None:
    # Kickoff §11.10: the stated minimum and purchase cost enter exactly as the source states them.
    result = priced(calculate(inputs(purchase=9999, minimum=12345), context(fee_rate="0")))
    assert result.final_sale_price_krw == 12345
    assert result.expected_net_profit_krw == 12345 - 9999


def test_a_context_that_leaves_no_price_reaching_the_target_is_not_priced() -> None:
    result = calculate(inputs(), context(fee_rate="0.4", other_cost_rate="0.25"))
    assert not isinstance(result, Calculation)
    assert result.code == "PRICING_TARGET_MARGIN_UNREACHABLE"


# ---------------------------------------------------------------- exact arithmetic


CASES = [
    (purchase, shipping, fee, fixed_fee, other, other_fixed)
    for purchase in (0, 1, 999, 10000, 12345, 987654)
    for shipping in (0, 2500)
    for fee, fixed_fee in (("0", 0), ("0.055", 0), ("0.1", 300), ("0.3", 1000))
    for other, other_fixed in (("0", 0), ("0.013", 90))
]


@pytest.mark.parametrize(
    ("purchase", "shipping", "fee", "fixed_fee", "other", "other_fixed"), CASES
)
def test_the_target_price_is_the_least_whole_krw_reaching_35_percent(
    purchase: int, shipping: int, fee: str, fixed_fee: int, other: str, other_fixed: int
) -> None:
    # Kickoff §11.22–23.
    source = inputs(purchase=purchase, shipping=shipping)
    ctx = context(
        fee_rate=fee,
        fee_fixed_krw=fixed_fee,
        other_cost_rate=other,
        other_cost_fixed_krw=other_fixed,
    )
    price = target_margin_price(source, ctx)
    assert price is not None and price >= 1
    assert meets_target(source, ctx, price)
    if price > 1:
        assert not meets_target(source, ctx, price - 1)


def test_exactly_ten_percent_is_not_below_the_minimum_margin() -> None:
    # Kickoff §11.24–25: 3/30 is exactly 10%. A float computed as 1 − 27/30 is 0.0999…, below it.
    at_boundary = priced(calculate(inputs(purchase=27, minimum=30), context(fee_rate="0")))
    assert at_boundary.expected_net_margin == MINIMUM_NET_MARGIN
    assert at_boundary.price_guard is PriceGuard.OK
    just_below = priced(calculate(inputs(purchase=27, minimum=29), context(fee_rate="0")))
    assert just_below.price_guard is PriceGuard.BELOW_MIN_MARGIN
    assert 1 - 27 / 30 < 0.1, "the float trap this rule must never fall into"


def test_the_display_margin_is_a_floor_and_never_decides() -> None:
    result = priced(calculate(inputs(purchase=27, minimum=30), context(fee_rate="0")))
    assert result.expected_net_margin_bp == 1000
    rounded_down = priced(calculate(inputs(purchase=10000, minimum=11500), context(fee_rate="0")))
    assert rounded_down.expected_net_margin_bp == 1304  # 1500/11500 = 13.043…%


def test_the_rounding_rule_is_declared_and_only_ceiling_to_one_krw_exists() -> None:
    assert [r.value for r in Rounding] == ["CEIL_KRW_1"]
    # A fee of 5.5% on 11,500 KRW is 632.5 KRW, charged as 633.
    assert context(fee_rate="0.055").platform_fee(11500) == 633
