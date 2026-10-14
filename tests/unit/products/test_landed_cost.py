"""ADR-0038 §4 (O2): the landed cost of a 구매대행 unit, pure and exact.

Every amount is decimal text read into exact fractions, rounded up to the won only where the rule
says so; every boundary is an exact comparison. Every rate here is invented.
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from fractions import Fraction

import pytest

from app.capabilities.overseas.fx_rules import FxSnapshot
from app.capabilities.overseas.settings import OverseasSettings
from app.stages.collect.facts import (
    FieldFact,
    FieldStatus,
    ForeignMoneyValue,
    ForeignPrice,
    ForeignPricesValue,
    ForeignShippingValue,
    MoneyValue,
    PriceRole,
    ShippingKind,
)
from app.stages.products.atomic_sku_economics_store import (
    AtomicPricingDependencies,
    AtomicSourceBindingRecord,
)
from app.stages.products.atomic_sku_item_store import AtomicSKUItemRecord
from app.stages.products.landed_cost import (
    DUTY_FREE_LIMIT_USD,
    FX_STALE,
    FX_UNAVAILABLE,
    LANDED_COST_COLUMNS,
    LANDED_COST_POLICY_VERSION,
    OVERSEAS_SHIPPING_UNSET,
    DayRates,
    DutyDecision,
    ForeignSourceInputs,
    LandedCost,
    LandedCostError,
    OverseasCostTerms,
    OverseasShippingSource,
    cost_terms,
    landed_cost,
    pin_columns,
    same_day_rates,
)
from app.stages.products.model import ReadinessStatus, Reason
from app.stages.products.overseas_costs import OverseasCostOverride
from app.stages.products.pricing import (
    MINIMUM_SALE_PRICE_NOT_POSITIVE,
    MINIMUM_SALE_PRICE_UNRESOLVED,
    PURCHASE_PRICE_AMBIGUOUS,
    PURCHASE_PRICE_UNRESOLVED,
    SHIPPING_CONDITIONAL,
    SHIPPING_UNKNOWN,
    SHIPPING_UNRESOLVED,
    PricingDependencies,
    foreign_source_inputs,
    landed_source_inputs,
)
from tests.support.collect_support import absent, confirmed
from tests.support.product_support import context, product

FRI = date(2026, 9, 11)
AT = datetime(2026, 9, 11, 3, 0, tzinfo=UTC)


def snapshot(
    currency: str, tts: str, day: date = FRI, snapshot_id: str | None = None
) -> FxSnapshot:
    code, unit = {"CNY": ("CNH", 1), "JPY": ("JPY(100)", 100), "USD": ("USD", 1)}[currency]
    return FxSnapshot(
        snapshot_id=snapshot_id or f"fx-{currency}-{day.isoformat()}",
        publication_date=day,
        currency=currency,
        rate_type="TTS",
        source_code=code,
        unit=unit,
        tts=tts,
        deal_bas_r=tts,
        source="koreaexim/AP01",
        fetched_at=AT,
    )


def terms(
    *,
    margin_bp: int = 500,
    shipping: int | None = 8000,
    source: OverseasShippingSource | None = OverseasShippingSource.DEFAULT,
    included: bool = True,
    duty_bp: int = 800,
    override_revision: int | None = None,
) -> OverseasCostTerms:
    return OverseasCostTerms(
        fx_margin_bp=margin_bp,
        duties_included_over_usd150=included,
        duty_rate_bp=duty_bp,
        overseas_shipping_krw=shipping,
        overseas_shipping_source=source if shipping is not None else None,
        settings_revision_no=None,
        settings_fingerprint=None,
        override_revision_no=override_revision,
    )


def cny(price: str, local: str = "0", **kwargs: str | None) -> ForeignSourceInputs:
    return ForeignSourceInputs("CNY", price, local, **kwargs)  # type: ignore[arg-type]


def landed(
    inputs: ForeignSourceInputs,
    rate: FxSnapshot | None = None,
    usd: FxSnapshot | None = None,
    cost_terms_: OverseasCostTerms | None = None,
) -> LandedCost:
    found = landed_cost(
        inputs,
        rate or snapshot(inputs.currency, "195.12"),
        usd or snapshot("USD", "1399.9"),
        cost_terms_ or terms(),
    )
    assert isinstance(found, LandedCost), found
    return found


# ---------------------------------------------------------------- the supplier cost


def test_the_supplier_cost_is_exact_and_rounded_up_once() -> None:
    # 5.80 × 195.12 × 1.05 = 1188.2808 → 1189.
    cost = landed(cny("5.80"))
    assert cost.supplier_cost_krw == 1189
    assert cost.purchase_cost_krw == 1189 and cost.duties_krw == 0
    # Exactly a whole won stays that won: as floats, 1.10 × 100 is 110.00000000000001 (→ 111).
    exact = landed(cny("1.10"), rate=snapshot("CNY", "100"), cost_terms_=terms(margin_bp=0))
    assert exact.supplier_cost_krw == 110
    # A single fen: 0.01 × 195.12 × 1.05 = 2.04876 → 3.
    assert landed(cny("0.01")).supplier_cost_krw == 3


def test_the_margin_is_the_settings_and_is_exact() -> None:
    plain = landed(cny("10.00"), rate=snapshot("CNY", "200"), cost_terms_=terms(margin_bp=0))
    assert plain.supplier_cost_krw == 2000
    five = landed(cny("10.00"), rate=snapshot("CNY", "200"), cost_terms_=terms(margin_bp=500))
    assert five.supplier_cost_krw == 2100
    # 5.25%: 2000 × 1.0525 = 2105.
    odd = landed(cny("10.00"), rate=snapshot("CNY", "200"), cost_terms_=terms(margin_bp=525))
    assert odd.supplier_cost_krw == 2105


def test_jpy_is_read_per_one_yen_from_its_per_100_publication() -> None:
    # JPY(100) 938.4 → 9.384 per yen; 980 × 9.384 × 1.05 = 9656.136 → 9657.
    inputs = ForeignSourceInputs("JPY", "980", "0")
    cost = landed(inputs, rate=snapshot("JPY", "938.4"))
    assert cost.supplier_cost_krw == 9657
    assert cost.pin().source_price == "980" and cost.pin().source_currency == "JPY"


# ---------------------------------------------------------------- duties (FX-04)


def _at_boundary(total: str, *, included: bool = True) -> LandedCost:
    # CNY 200, USD 1000: a declared value of x yuan is x / 5 dollars, exactly.
    return landed(
        cny(total, "0"),
        rate=snapshot("CNY", "200"),
        usd=snapshot("USD", "1000"),
        cost_terms_=terms(included=included),
    )


def test_exactly_usd_150_is_duty_free_and_a_cent_more_is_not() -> None:
    at = _at_boundary("750.00")
    assert at.declared_value_usd == DUTY_FREE_LIMIT_USD == Fraction(150)
    assert at.duty_decision is DutyDecision.NOT_OVER_150 and at.duties_krw == 0
    assert at.pin().declared_value_usd == "150.00"
    over = _at_boundary("750.05")
    assert over.declared_value_usd == Fraction(15001, 100)
    assert over.duty_decision is DutyDecision.INCLUDED
    # customs value 750.05 × 200 = 150010; duty ceil(150010 × 8%) = 12001;
    # VAT ceil((150010 + 12001) × 10%) = 16202.
    assert over.duties_krw == 12001 + 16202
    assert over.purchase_cost_krw == over.supplier_cost_krw + 28203
    assert over.pin().declared_value_usd == "150.01"


def test_the_declared_value_adds_the_stated_local_delivery_without_the_margin() -> None:
    # 740.00 + 10.05 delivery = 750.05 yuan: above 150 although the price alone is not.
    cost = landed(
        cny("740.00", "10.05"),
        rate=snapshot("CNY", "200"),
        usd=snapshot("USD", "1000"),
        cost_terms_=terms(margin_bp=10000),
    )
    assert cost.duty_decision is DutyDecision.INCLUDED
    # The local delivery is never a cost of its own: the supplier cost is the price alone.
    assert cost.supplier_cost_krw == 740 * 200 * 2


def test_excluded_duties_are_zero_and_recorded_as_excluded() -> None:
    excluded = _at_boundary("750.05", included=False)
    assert excluded.duty_decision is DutyDecision.EXCLUDED and excluded.duties_krw == 0
    assert excluded.purchase_cost_krw == excluded.supplier_cost_krw
    assert excluded.pin().duty_decision is DutyDecision.EXCLUDED
    # At or under the limit the user's choice changes nothing.
    assert _at_boundary("750.00", included=False).duty_decision is DutyDecision.NOT_OVER_150


def test_the_duty_rate_is_the_terms() -> None:
    cost = landed(
        cny("750.05"),
        rate=snapshot("CNY", "200"),
        usd=snapshot("USD", "1000"),
        cost_terms_=terms(duty_bp=0),
    )
    # No duty, only VAT: ceil(150010 × 10%) = 15001.
    assert cost.duties_krw == 15001 and cost.pin().duty_rate_bp == 0


# ---------------------------------------------------------------- shipping and terms


def test_the_products_own_inputs_come_before_the_settings_defaults() -> None:
    settings = OverseasSettings(overseas_shipping_default_krw=8000, duty_rate_bp=800)
    default = cost_terms(settings, OverseasCostOverride("sp-1"))
    assert (default.overseas_shipping_krw, default.overseas_shipping_source) == (
        8000,
        OverseasShippingSource.DEFAULT,
    )
    assert default.duty_rate_bp == 800 and default.override_revision_no is None
    own = cost_terms(
        settings, OverseasCostOverride("sp-1", overseas_shipping_krw=12000, revision_no=2)
    )
    assert (own.overseas_shipping_krw, own.overseas_shipping_source) == (
        12000,
        OverseasShippingSource.PRODUCT,
    )
    assert own.duty_rate_bp == 800 and own.override_revision_no == 2
    duty_only = cost_terms(settings, OverseasCostOverride("sp-1", duty_rate_bp=1300, revision_no=1))
    assert duty_only.overseas_shipping_source is OverseasShippingSource.DEFAULT
    assert duty_only.duty_rate_bp == 1300
    # Zero is a stated shipping, not an unset one.
    free = cost_terms(
        settings, OverseasCostOverride("sp-1", overseas_shipping_krw=0, revision_no=1)
    )
    assert free.overseas_shipping_krw == 0
    assert free.overseas_shipping_source is OverseasShippingSource.PRODUCT


def test_no_overseas_shipping_at_all_is_review_required() -> None:
    unset = cost_terms(OverseasSettings(), OverseasCostOverride("sp-1"))
    assert unset.overseas_shipping_krw is None and unset.overseas_shipping_source is None
    found = landed_cost(cny("5.80"), snapshot("CNY", "195.12"), snapshot("USD", "1399.9"), unset)
    assert found == (
        Reason(OVERSEAS_SHIPPING_UNSET, ReadinessStatus.REVIEW_REQUIRED, "overseas_shipping"),
    )


def test_the_landed_cost_enters_the_calculation_unchanged() -> None:
    cost = landed(cny("750.05"), rate=snapshot("CNY", "200"), usd=snapshot("USD", "1000"))
    inputs = landed_source_inputs(cost)
    assert inputs.purchase_cost_krw == cost.supplier_cost_krw + cost.duties_krw
    assert inputs.supplier_shipping_krw == 8000
    assert inputs.minimum_sale_price_krw is None and inputs.minimum_applied
    assert inputs.landed_cost is cost


def test_a_stated_minimum_is_converted_at_the_tts_without_the_margin() -> None:
    # 100 yuan × 195.12 = 19512 exactly; 0.01 yuan = 1.9512 → 2.
    assert landed(cny("5.80", minimum="100")).minimum_sale_price_krw == 19512
    assert landed(cny("5.80", minimum="0.01")).minimum_sale_price_krw == 2
    waived = landed(
        ForeignSourceInputs("CNY", "5.80", "0", None, False, "100", False),
    )
    assert waived.minimum_sale_price_krw is None and waived.stated_minimum_sale_price_krw == 19512
    inputs = landed_source_inputs(waived)
    assert not inputs.minimum_applied and inputs.minimum_waived


# ---------------------------------------------------------------- the rates (FX-03)

TODAY = date(2026, 9, 13)  # a Sunday


def _newest(*held: FxSnapshot):  # type: ignore[no-untyped-def]
    def newest(currency: str, day: date) -> FxSnapshot | None:
        found = [s for s in held if s.currency == currency and s.publication_date <= day]
        return max(found, key=lambda s: s.publication_date, default=None)

    return newest


def test_both_rates_of_one_day_are_read() -> None:
    rate, usd = snapshot("CNY", "195.12"), snapshot("USD", "1399.9")
    assert same_day_rates("CNY", TODAY, _newest(rate, usd)) == DayRates(rate, usd)


def test_a_missing_rate_is_unavailable_for_its_own_currency() -> None:
    usd = snapshot("USD", "1399.9")
    assert same_day_rates("CNY", TODAY, _newest(usd)) == (
        Reason(FX_UNAVAILABLE, ReadinessStatus.REVIEW_REQUIRED, "fx:CNY"),
    )
    assert same_day_rates("JPY", TODAY, _newest(snapshot("JPY", "938.4"))) == (
        Reason(FX_UNAVAILABLE, ReadinessStatus.REVIEW_REQUIRED, "fx:USD"),
    )
    assert same_day_rates("CNY", TODAY, _newest()) == (
        Reason(FX_UNAVAILABLE, ReadinessStatus.REVIEW_REQUIRED, "fx:CNY"),
        Reason(FX_UNAVAILABLE, ReadinessStatus.REVIEW_REQUIRED, "fx:USD"),
    )


def test_a_rate_older_than_three_business_days_is_stale_for_usd_too() -> None:
    monday = date(2026, 9, 7)
    old = (snapshot("CNY", "195", monday), snapshot("USD", "1399", monday))
    # Mon 7 → Thu 10 is three business days later: still fresh.
    assert isinstance(same_day_rates("CNY", date(2026, 9, 10), _newest(*old)), DayRates)
    # Fri 11 is the fourth: stale, for each currency.
    assert same_day_rates("CNY", date(2026, 9, 11), _newest(*old)) == (
        Reason(FX_STALE, ReadinessStatus.REVIEW_REQUIRED, "fx:CNY"),
        Reason(FX_STALE, ReadinessStatus.REVIEW_REQUIRED, "fx:USD"),
    )
    fresh_cny = snapshot("CNY", "196", FRI)
    assert same_day_rates("CNY", date(2026, 9, 11), _newest(*old, fresh_cny)) == (
        Reason(FX_STALE, ReadinessStatus.REVIEW_REQUIRED, "fx:USD"),
    )


def test_a_pair_is_always_of_one_publication_day() -> None:
    thursday = date(2026, 9, 10)
    rate_thu, usd_thu = snapshot("CNY", "195", thursday), snapshot("USD", "1399", thursday)
    rate_fri = snapshot("CNY", "196", FRI)
    # Friday's USD is missing: the pair is Thursday's, never Friday's CNY with Thursday's USD.
    assert same_day_rates("CNY", TODAY, _newest(rate_thu, usd_thu, rate_fri)) == DayRates(
        rate_thu, usd_thu
    )
    # No common day at all: unavailable, for the currency that lacks the older day.
    assert same_day_rates("CNY", TODAY, _newest(usd_thu, rate_fri)) == (
        Reason(FX_UNAVAILABLE, ReadinessStatus.REVIEW_REQUIRED, "fx:CNY"),
    )
    with pytest.raises(LandedCostError):
        landed_cost(cny("1"), rate_fri, usd_thu, terms())


# ---------------------------------------------------------------- the source's own inputs


def _foreign(**overrides: FieldFact) -> dict[str, FieldFact]:
    fields: dict[str, FieldFact] = {
        "prices": confirmed(
            ForeignPricesValue(prices=(ForeignPrice(label="价格", currency="CNY", amount="5.80"),)),
            ".price",
        ),
        "shipping": confirmed(
            ForeignShippingValue(kind=ShippingKind.FREE, policy_text="包邮", currency="CNY"),
            ".delivery",
        ),
        **overrides,
    }
    return product(**fields)


def test_the_purchase_price_and_delivery_are_read_as_stated() -> None:
    assert foreign_source_inputs(_foreign(), "CNY") == ForeignSourceInputs("CNY", "5.80", "0")
    tiered = ForeignPricesValue(
        prices=(
            ForeignPrice(label="原价", currency="CNY", amount="9.90", role=PriceRole.LIST),
            ForeignPrice(label="价格", currency="CNY", amount="5.80", role=PriceRole.PURCHASE),
        )
    )
    fixed = ForeignShippingValue(
        kind=ShippingKind.FIXED, policy_text="运费 12.50", currency="CNY", fee="12.50"
    )
    found = foreign_source_inputs(
        _foreign(prices=confirmed(tiered, ".p"), shipping=confirmed(fixed, ".s")), "CNY"
    )
    assert found == ForeignSourceInputs("CNY", "5.80", "12.50")
    free_over = ForeignShippingValue(
        kind=ShippingKind.CONDITIONAL,
        policy_text="满99包邮",
        currency="CNY",
        fee="8",
        free_over="99",
    )
    assert foreign_source_inputs(_foreign(shipping=confirmed(free_over, ".s")), "CNY") == (
        ForeignSourceInputs("CNY", "5.80", "8")
    )


def _codes(found: object) -> set[str]:
    assert isinstance(found, tuple)
    return {reason.code for reason in found}


def test_unreadable_inputs_are_reasons_never_guesses() -> None:
    alone_list = ForeignPricesValue(
        prices=(ForeignPrice(label="原价", currency="CNY", amount="9.90", role=PriceRole.LIST),)
    )
    assert _codes(foreign_source_inputs(_foreign(prices=confirmed(alone_list, ".p")), "CNY")) == {
        PURCHASE_PRICE_AMBIGUOUS
    }
    # A won price, or another currency, is never read as this one.
    assert _codes(foreign_source_inputs(product(), "CNY")) == {
        PURCHASE_PRICE_AMBIGUOUS,
        SHIPPING_UNRESOLVED,
    }
    assert _codes(foreign_source_inputs(_foreign(), "JPY")) == {
        PURCHASE_PRICE_AMBIGUOUS,
        SHIPPING_UNRESOLVED,
    }
    assert _codes(foreign_source_inputs(_foreign(prices=absent(".p")), "CNY")) == {
        PURCHASE_PRICE_UNRESOLVED
    }
    conditional = ForeignShippingValue(
        kind=ShippingKind.CONDITIONAL, policy_text="按地区", currency="CNY"
    )
    unknown = ForeignShippingValue(kind=ShippingKind.UNKNOWN, policy_text="?", currency="CNY")
    assert _codes(
        foreign_source_inputs(_foreign(shipping=confirmed(conditional, ".s")), "CNY")
    ) == {SHIPPING_CONDITIONAL}
    review = FieldFact(FieldStatus.REVIEW_REQUIRED, unknown, confirmed(unknown, ".s").evidence)
    assert _codes(foreign_source_inputs(_foreign(shipping=review), "CNY")) == {SHIPPING_UNRESOLVED}
    assert _codes(foreign_source_inputs(_foreign(shipping=confirmed(unknown, ".s")), "CNY")) == {
        SHIPPING_UNKNOWN
    }


def test_the_minimum_follows_the_sales_policy() -> None:
    stated = confirmed(ForeignMoneyValue(label="最低价", currency="CNY", amount="100"), ".m")
    found = foreign_source_inputs(_foreign(minimum_sale_price=stated), "CNY")
    assert isinstance(found, ForeignSourceInputs) and found.minimum == "100"
    zero = confirmed(ForeignMoneyValue(label="最低价", currency="CNY", amount="0.00"), ".m")
    assert _codes(foreign_source_inputs(_foreign(minimum_sale_price=zero), "CNY")) == {
        MINIMUM_SALE_PRICE_NOT_POSITIVE
    }
    won = confirmed(MoneyValue(label="최저", amount_krw=1000), ".m")
    assert _codes(foreign_source_inputs(_foreign(minimum_sale_price=won), "CNY")) == {
        MINIMUM_SALE_PRICE_UNRESOLVED
    }
    waived = foreign_source_inputs(_foreign(minimum_sale_price=stated), "CNY", apply_minimum=False)
    assert waived == ForeignSourceInputs("CNY", "5.80", "0", None, False, "100", False)
    unread = foreign_source_inputs(_foreign(minimum_sale_price=won), "CNY", apply_minimum=False)
    assert unread == ForeignSourceInputs("CNY", "5.80", "0", None, False, None, True)


def test_an_atomic_unit_price_replaces_the_generic_prices() -> None:
    found = foreign_source_inputs(_foreign(prices=absent(".p")), "CNY", unit_price="7.25")
    assert found == ForeignSourceInputs("CNY", "7.25", "0")


# ---------------------------------------------------------------- pins and fingerprints


def test_a_krw_price_records_no_pin_and_a_foreign_one_every_pin() -> None:
    assert pin_columns(None, purchase_cost_krw=1, supplier_shipping_krw=0) == dict.fromkeys(
        LANDED_COST_COLUMNS
    )
    cost = landed(cny("750.05"), rate=snapshot("CNY", "200"), usd=snapshot("USD", "1000"))
    columns = pin_columns(
        cost.pin(), purchase_cost_krw=cost.purchase_cost_krw, supplier_shipping_krw=8000
    )
    assert columns == {
        "source_currency": "CNY",
        "source_price": "750.05",
        "fx_snapshot_id": "fx-CNY-2026-09-11",
        "fx_publication_date": "2026-09-11",
        "usd_fx_snapshot_id": "fx-USD-2026-09-11",
        "fx_margin_bp": 500,
        "overseas_settings_revision_no": None,
        "overseas_cost_override_revision_no": None,
        "overseas_shipping_krw": 8000,
        "overseas_shipping_source": "DEFAULT",
        "duty_decision": "INCLUDED",
        "duty_rate_bp": 800,
        "duties_krw": 28203,
        "declared_value_usd": "150.01",
    }


def test_the_store_rule_refuses_pins_that_do_not_describe_the_row() -> None:
    cost = landed(cny("750.05"), rate=snapshot("CNY", "200"), usd=snapshot("USD", "1000"))
    pin = cost.pin()
    purchase = cost.purchase_cost_krw
    with pytest.raises(LandedCostError, match="overseas shipping"):
        pin_columns(pin, purchase_cost_krw=purchase, supplier_shipping_krw=0)
    with pytest.raises(LandedCostError, match="purchase cost"):
        pin_columns(pin, purchase_cost_krw=1, supplier_shipping_krw=8000)
    with pytest.raises(LandedCostError, match="included"):
        pin_columns(
            replace(pin, duty_decision=DutyDecision.EXCLUDED),
            purchase_cost_krw=purchase,
            supplier_shipping_krw=8000,
        )
    with pytest.raises(LandedCostError, match="names its revision"):
        pin_columns(
            replace(pin, overseas_shipping_source=OverseasShippingSource.PRODUCT),
            purchase_cost_krw=purchase,
            supplier_shipping_krw=8000,
        )


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


def test_krw_fingerprints_are_byte_identical_to_before_o2() -> None:
    # Golden values computed on the O1 base (a9a26d77) before any O2 change.
    pricing = context()
    assert DEPENDENCIES.fingerprint(pricing) == (
        "73baf11e788192fbe122eafa2150368900587a22ead23793f269291b812ee97f"
    )
    assert replace(DEPENDENCIES, minimum_applied=False).fingerprint(pricing) == (
        "2409b9a41b1b6c2aebcc21df9385f472a64c0778e10646d9e42b74a95d0d23b3"
    )


def test_every_landed_cost_input_moves_a_foreign_fingerprint() -> None:
    pricing = context()
    base = landed(cny("5.80"))
    fingerprints = {
        replace(DEPENDENCIES, landed_cost=cost.dependency()).fingerprint(pricing)
        for cost in (
            base,
            landed(cny("5.80"), rate=snapshot("CNY", "195.12", snapshot_id="fx-new")),
            landed(cny("5.80"), usd=snapshot("USD", "1399.9", snapshot_id="usd-new")),
            landed(cny("5.80"), cost_terms_=terms(margin_bp=600)),
            landed(cny("5.80"), cost_terms_=terms(shipping=9000)),
            landed(cny("5.80"), cost_terms_=terms(duty_bp=900)),
            landed(cny("5.80"), cost_terms_=terms(override_revision=1)),
            landed(cny("5.80", "1.00")),
            landed(cny("5.80"), cost_terms_=replace(terms(), settings_revision_no=1)),
            landed(cny("5.80"), cost_terms_=replace(terms(), settings_fingerprint="f" * 64)),
        )
    }
    assert len(fingerprints) == 10
    assert DEPENDENCIES.fingerprint(pricing) not in fingerprints
    assert dict(base.dependency())["policy_version"] == LANDED_COST_POLICY_VERSION


def test_atomic_krw_fingerprints_are_byte_identical_to_before_o2() -> None:
    # Golden values computed on the O1 base (a9a26d77) before any O2 change.
    item = AtomicSKUItemRecord("a-1", "g-1", "comp-1", "c" * 64, "sku-1", "d" * 64)
    binding = AtomicSourceBindingRecord(
        "ab-1",
        "a-1",
        "rm-1",
        "gm-1",
        "r-1",
        "BASE_PRICE_PLUS_OPTION_DELTA",
        10000,
        500,
        10500,
        datetime(2026, 10, 14, tzinfo=UTC),
        None,
    )
    atomic = AtomicPricingDependencies(
        item=item,
        membership_revision_id="m-1",
        binding=binding,
        source_revision_id="r-1",
        atomic_sku_set_revision_id="s-1",
    )
    pricing = context()
    assert atomic.fingerprint(pricing) == (
        "eced546992155bb0cb3a7c7e700f3fe25587eac2746d9196bc116da96f601e2f"
    )
    assert replace(atomic, minimum_applied=False).fingerprint(pricing) == (
        "0a83b4dce9e67535b40c8831fe61f5b1e6857f6763d571f276020eac31c33415"
    )
    foreign = replace(
        atomic,
        binding=replace(binding, source_currency="CNY", source_unit_price="9.50"),
        landed_cost=landed(cny("9.50")).dependency(),
    )
    assert foreign.fingerprint(pricing) != atomic.fingerprint(pricing)
