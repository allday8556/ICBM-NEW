"""The M4 pricing rule: explicit context, exact arithmetic, one canonical rule (ADR-0013 §7).

Pure: no database, no clock, no I/O. Issue #80 PR-D (kickoff 5737440897).

**The pricing context is supplied, never assumed.** No marketplace fee table is accepted in the
repository, so nothing here knows a marketplace's fee. :class:`PricingContextInput` carries the
effective fee and policy inputs its caller supplies. Its fingerprint covers every effective value,
not only the version labels, so one label reused with different values can never alias.

**Exact arithmetic.** Money is whole KRW (``int``); rates are :class:`~fractions.Fraction` parsed
from decimal text. A ``float`` is refused wherever money, a rate or a margin could enter, and every
gate is an exact rational comparison. Rounding is declared, not implied: v1 knows only
``CEIL_KRW_1``, ceiling to the next whole KRW.

**The canonical rule** (CLAUDE.md §6.1)::

    if minimum_sale_price exists:
        final_sale_price = minimum_sale_price
        price_basis = MINIMUM_SALE_PRICE
    else:
        final_sale_price = target_margin_price
        price_basis = TARGET_MARGIN

**Source inputs fail closed.** A purchase cost, a supplier shipping fee and a minimum sale price
are read only where the current source revision states them unambiguously. Anything else is a
reason, never a guess: no price is chosen by label or order, no conditional shipping is flattened
(the one exception is the owner's: a free-over policy is priced at its base fee, ADR-0034 §2
FO-02), and nothing is multiplied by a quantity or divided into a unit price.

**Two procurements** (ADR-0013 §6–§7):
- A ``BASE_PRODUCT`` binding reads the revision's one explicit base price, its shipping and its
  minimum sale price.
- A ``SOURCE_OFFER`` binding (PR-Q, ruling 5738760913) reads the bound offer's own total as the
  purchase cost, and never a generic ``prices`` entry. Shipping follows the same rules. The generic
  ``minimum_sale_price`` names no offer, so a SOURCE_OFFER is priced only when it is ABSENT: a
  stated or unsettled one is REVIEW_REQUIRED. It is never applied to every quantity and never
  multiplied by one.

**The supplier's sales policy** (ADR-0039 §3). Whether the minimum sale price is applied is the
user's choice, per supplier (``app.stages.products.sales_policy``). With ``apply_minimum`` false
the minimum is not a pricing input: :class:`SourceInputs` carries ``minimum_sale_price_krw=None``,
the price is the target-margin price, and an unread or offer-unbound minimum no longer stops
pricing. The minimum the source stated is kept beside the price (``stated_minimum_sale_price_krw``)
and the snapshot records ``minimum_applied=False``; a statement it could not read is
``minimum_unresolved``. Either is the warning ``PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED`` (SP-04).
The applied path, and every fingerprint it produces, is unchanged.

**A source currency is priced at its landed cost** (ADR-0038 §4, slice O2). A revision that states
its prices in CNY or JPY is read by :func:`foreign_source_inputs` — its PURCHASE price (or, for an
AtomicSKU Item, its configuration's own unit price), its stated local delivery and its minimum, as
decimal text in its currency — and the landed cost (``app.stages.products.landed_cost``) becomes the
pricing inputs (:func:`landed_source_inputs`): the supplier cost and any included duties are the
purchase cost, the overseas shipping is the supplier shipping, and the calculation below is
unchanged. The dependency fingerprint then names the landed cost's every input
(``PricingDependencies.landed_cost``); a KRW price names none, so every KRW fingerprint is
byte-identical. A ``SOURCE_OFFER`` (quantity offer) of such a revision is still
``PRICING_SOURCE_CURRENCY_UNSUPPORTED`` (:func:`source_currency_reasons`): no foreign offer is ever
materialized, and none is priced.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final

from app.stages.collect.facts import (
    FieldStatus,
    ForeignMoneyValue,
    ForeignPricesValue,
    ForeignShippingValue,
    MoneyValue,
    PricesValue,
    ShippingKind,
    ShippingValue,
)

# ADR-0038 §3, §4 (O2): the landed cost's own reasons are pricing's as well — no rate that may
# price (the FX owner's FX-03 codes) and no overseas shipping at all. Each is REVIEW_REQUIRED.
from app.stages.products.landed_cost import FX_STALE as FX_STALE
from app.stages.products.landed_cost import FX_UNAVAILABLE as FX_UNAVAILABLE
from app.stages.products.landed_cost import OVERSEAS_SHIPPING_UNSET as OVERSEAS_SHIPPING_UNSET
from app.stages.products.landed_cost import ForeignSourceInputs, LandedCost
from app.stages.products.model import ReadinessStatus, Reason
from app.stages.products.quantity import (
    OfferField,
    OfferTerms,
    QuantityOfferEvidence,
    product_level_offers,
)

PRICING_RULE_VERSION: Final = "pricing-rule/v1"
CONTEXT_VERSION: Final = "pricing-context/v1"
# v2 (PR-Q): the dependency names the binding kind, the exact offer and the fulfillment quantity.
DEPENDENCY_VERSION: Final = "pricing-dependency/v2"
# Policy v1 (Issue #80 §7, kickoff 5737440897 §B): exact fractions, never floats.
TARGET_NET_MARGIN: Final = Fraction(35, 100)
MINIMUM_NET_MARGIN: Final = Fraction(10, 100)

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")


class PricingError(ValueError):
    """A pricing context or input that cannot be priced exactly."""


class Rounding(StrEnum):
    """How an amount becomes whole KRW. v1 declares one rule: ceiling to the next whole KRW."""

    CEIL_KRW_1 = "CEIL_KRW_1"


class PriceBasis(StrEnum):
    MINIMUM_SALE_PRICE = "MINIMUM_SALE_PRICE"
    TARGET_MARGIN = "TARGET_MARGIN"


class PriceGuard(StrEnum):
    OK = "OK"
    LOSS = "LOSS"
    BELOW_MIN_MARGIN = "BELOW_MIN_MARGIN"


class PricingMoveReason(StrEnum):
    """Why the current snapshot of one `(Item, pricing context)` moved."""

    INITIAL = "INITIAL"
    REPRICED = "REPRICED"


class GuardReason(StrEnum):
    """Every guard that applies. The primary guard is LOSS when a loss applies, but a loss is also
    below the minimum margin, and that is never hidden."""

    PRICE_LOSS = "PRICE_LOSS"
    PRICE_BELOW_MIN_MARGIN = "PRICE_BELOW_MIN_MARGIN"


def rate(text: str) -> Fraction:
    """An exact rate from decimal text, such as ``"0.055"``. A float is refused: ``0.055`` as a
    float is not 55/1000."""
    if not isinstance(text, str) or not _DECIMAL.fullmatch(text):
        raise PricingError("a rate is decimal text such as '0.055', never a float")
    value = Fraction(text)
    if not 0 <= value < 1:
        raise PricingError("a rate is at least 0 and below 1")
    return value


def _krw(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PricingError(f"{name} is a non-negative whole KRW amount")
    return value


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def digest(structure: object) -> str:
    text = json.dumps(structure, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class PricingContextInput:
    """One explicit pricing context and its effective inputs (ADR-0013 §7).

    Every field is required: there is no default fee, no default policy and no default
    marketplace. ``account_id`` is ``None`` only to say explicitly that the fee and policy do not
    vary by account. The platform fee is ``ceil(fee_rate × price) + fee_fixed_krw``; the other
    policy costs are ``ceil(other_cost_rate × price) + other_cost_fixed_krw``. Both depend on the
    sale price they are charged on.
    """

    marketplace_key: str
    account_id: str | None
    fee_table_version: str
    pricing_policy_version: str
    fee_rate: str
    fee_fixed_krw: int
    other_cost_rate: str
    other_cost_fixed_krw: int
    cost_rounding: Rounding
    price_rounding: Rounding

    def __post_init__(self) -> None:
        if not isinstance(self.marketplace_key, str) or not _KEY.fullmatch(self.marketplace_key):
            raise PricingError("marketplace_key is a marketplace key")
        if self.account_id is not None and (
            not isinstance(self.account_id, str) or not _LABEL.fullmatch(self.account_id)
        ):
            raise PricingError("account_id is an account identifier, or None for no variation")
        for name in ("fee_table_version", "pricing_policy_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _LABEL.fullmatch(value):
                raise PricingError(f"{name} is a version label")
        rate(self.fee_rate)
        rate(self.other_cost_rate)
        _krw("fee_fixed_krw", self.fee_fixed_krw)
        _krw("other_cost_fixed_krw", self.other_cost_fixed_krw)
        for name in ("cost_rounding", "price_rounding"):
            if not isinstance(getattr(self, name), Rounding):
                raise PricingError(f"{name} is a declared rounding rule")

    @property
    def fee_rate_value(self) -> Fraction:
        return rate(self.fee_rate)

    @property
    def other_cost_rate_value(self) -> Fraction:
        return rate(self.other_cost_rate)

    def canonical(self) -> dict[str, object]:
        """Every effective input, normalized: equal effective values give equal structures."""
        return {
            "version": CONTEXT_VERSION,
            "marketplace_key": self.marketplace_key,
            "account_id": self.account_id,
            "fee_table_version": self.fee_table_version,
            "pricing_policy_version": self.pricing_policy_version,
            "fee": {
                "rate": _fraction_text(self.fee_rate_value),
                "fixed_krw": self.fee_fixed_krw,
            },
            "other_cost": {
                "rate": _fraction_text(self.other_cost_rate_value),
                "fixed_krw": self.other_cost_fixed_krw,
            },
            "cost_rounding": self.cost_rounding.value,
            "price_rounding": self.price_rounding.value,
        }

    @property
    def fingerprint(self) -> str:
        return digest(self.canonical())

    def platform_fee(self, price: int) -> int:
        return _ceil(self.fee_rate_value * price) + self.fee_fixed_krw

    def other_policy_cost(self, price: int) -> int:
        return _ceil(self.other_cost_rate_value * price) + self.other_cost_fixed_krw


# ---------------------------------------------------------------- source inputs

# Reason codes: our own, never page content.
PURCHASE_PRICE_UNRESOLVED = "PRICING_PURCHASE_PRICE_UNRESOLVED"
PURCHASE_PRICE_AMBIGUOUS = "PRICING_PURCHASE_PRICE_AMBIGUOUS"
SHIPPING_UNRESOLVED = "PRICING_SHIPPING_UNRESOLVED"
SHIPPING_CONDITIONAL = "PRICING_SHIPPING_CONDITIONAL"
SHIPPING_UNKNOWN = "PRICING_SHIPPING_UNKNOWN"
MINIMUM_SALE_PRICE_UNRESOLVED = "PRICING_MINIMUM_SALE_PRICE_UNRESOLVED"
MINIMUM_SALE_PRICE_NOT_POSITIVE = "PRICING_MINIMUM_SALE_PRICE_NOT_POSITIVE"
TARGET_MARGIN_UNREACHABLE = "PRICING_TARGET_MARGIN_UNREACHABLE"
# PR-Q: a SOURCE_OFFER whose offer its revision no longer states exactly, and a generic minimum
# sale price that names no offer.
QUANTITY_OFFER_UNRESOLVED = "PRICING_QUANTITY_OFFER_UNRESOLVED"
MINIMUM_SALE_PRICE_NOT_OFFER_BOUND = "PRICING_MINIMUM_SALE_PRICE_NOT_OFFER_BOUND"
# ADR-0039 §3: a minimum the source states (or could not be read) that the supplier's sales policy
# does not apply. A non-blocking warning, never a reason not to price.
MINIMUM_SALE_PRICE_NOT_APPLIED = "PRICING_MINIMUM_SALE_PRICE_NOT_APPLIED"
# ADR-0038 §1, O1a: a quantity offer of a revision whose prices are stated in a source currency
# (CNY, JPY). Such an offer is never materialized, and the offer path never prices one (O2 prices
# the base and AtomicSKU paths at their landed cost).
SOURCE_CURRENCY_UNSUPPORTED = "PRICING_SOURCE_CURRENCY_UNSUPPORTED"

SourceField = OfferField


@dataclass(frozen=True)
class SourceInputs:
    """What the bound current source revision states for the bound procurement, exactly: a
    BASE_PRODUCT's base price, or a SOURCE_OFFER's own offer total."""

    purchase_cost_krw: int
    supplier_shipping_krw: int
    minimum_sale_price_krw: int | None
    # ADR-0039 §3: False when the supplier's sales policy does not apply the minimum. Then
    # ``minimum_sale_price_krw`` is None, ``stated_minimum_sale_price_krw`` is the minimum the
    # source stated (None when none could be read) and ``minimum_unresolved`` says a statement
    # exists that no rule could read.
    minimum_applied: bool = True
    stated_minimum_sale_price_krw: int | None = None
    minimum_unresolved: bool = False
    # ADR-0038 §4 (O2): the landed cost these inputs were made from, for a source-currency price;
    # None for a KRW price. The snapshot records its pins, and only then.
    landed_cost: LandedCost | None = None

    @property
    def minimum_waived(self) -> bool:
        """Whether a minimum the source states, read or not, goes unapplied (the warning)."""
        return not self.minimum_applied and (
            self.stated_minimum_sale_price_krw is not None or self.minimum_unresolved
        )


def _review(code: str, subject: str) -> Reason:
    return Reason(code, ReadinessStatus.REVIEW_REQUIRED, subject)


def source_currency_reasons(source_currency: str | None) -> tuple[Reason, ...]:
    """ADR-0038 §1: the reason a quantity offer (SOURCE_OFFER) of a revision stated in a source
    currency is not priced, or none for a KRW revision. The offer path asks this first, before it
    reads a single amount, so no CNY or JPY offer total is ever read as won."""
    if source_currency is None:
        return ()
    return (_review(SOURCE_CURRENCY_UNSUPPORTED, "prices"),)


def _supplier_shipping(fields: Mapping[str, SourceField], reasons: list[Reason]) -> int | None:
    """The supplier shipping fee the revision states unambiguously, or ``None`` with its reason."""
    shipping = fields.get("shipping")
    if (
        shipping is None
        or shipping.status is not FieldStatus.CONFIRMED
        or not isinstance(shipping.value, ShippingValue)
    ):
        reasons.append(_review(SHIPPING_UNRESOLVED, "shipping"))
    elif shipping.value.kind is ShippingKind.FREE:
        return 0
    elif shipping.value.kind is ShippingKind.FIXED and shipping.value.fee_krw is not None:
        return shipping.value.fee_krw
    elif (
        shipping.value.kind is ShippingKind.CONDITIONAL
        and shipping.value.fee_krw is not None
        and shipping.value.free_over_krw is not None
    ):
        # ADR-0034 §2 (Issue #219 6088081139): a free-over policy is priced at its base fee; a
        # consignment order of one or two units almost never reaches the threshold.
        return shipping.value.fee_krw
    elif shipping.value.kind is ShippingKind.CONDITIONAL:
        # Any other condition (no base fee, a quantity tier, a region) is not priced.
        reasons.append(_review(SHIPPING_CONDITIONAL, "shipping"))
    else:
        reasons.append(_review(SHIPPING_UNKNOWN, "shipping"))
    return None


def _stated_minimum(fields: Mapping[str, SourceField]) -> tuple[int | None, bool]:
    """ADR-0039 §3: the minimum a source states, for a policy that does not apply it — the
    positive amount it states exactly, and whether a statement exists that could not be read."""
    stated = fields.get("minimum_sale_price")
    if stated is not None and stated.status is FieldStatus.ABSENT:
        return None, False
    if (
        stated is not None
        and stated.status is FieldStatus.CONFIRMED
        and isinstance(stated.value, MoneyValue)
        and stated.value.amount_krw > 0
    ):
        return stated.value.amount_krw, False
    return None, True


def source_inputs(
    fields: Mapping[str, SourceField], *, apply_minimum: bool = True
) -> SourceInputs | tuple[Reason, ...]:
    """The BASE_PRODUCT pricing inputs, or every reason they cannot be read unambiguously.

    With ``apply_minimum`` false (ADR-0039 §3) the minimum sale price is not an input: it neither
    sets the price nor stops pricing, and the minimum the source stated is carried beside it."""
    reasons: list[Reason] = []

    purchase = None
    prices = fields.get("prices")
    if prices is None or prices.status is not FieldStatus.CONFIRMED:
        reasons.append(_review(PURCHASE_PRICE_UNRESOLVED, "prices"))
    elif not isinstance(prices.value, PricesValue) or prices.value.purchase() is None:
        # ADR-0032 §3: the purchase cost is the one declared PURCHASE price or the only price
        # without a role; it is never chosen by label, name or order, and a LIST price never is.
        reasons.append(_review(PURCHASE_PRICE_AMBIGUOUS, "prices"))
    else:
        chosen = prices.value.purchase()
        assert chosen is not None
        purchase = chosen.amount_krw

    shipping_fee = _supplier_shipping(fields, reasons)

    if not apply_minimum:
        if reasons or purchase is None or shipping_fee is None:
            return tuple(reasons)
        amount, unresolved = _stated_minimum(fields)
        return SourceInputs(purchase, shipping_fee, None, False, amount, unresolved)

    minimum = None
    stated = fields.get("minimum_sale_price")
    if stated is not None and stated.status is FieldStatus.CONFIRMED:
        if not isinstance(stated.value, MoneyValue):
            reasons.append(_review(MINIMUM_SALE_PRICE_UNRESOLVED, "minimum_sale_price"))
        elif stated.value.amount_krw <= 0:
            reasons.append(_review(MINIMUM_SALE_PRICE_NOT_POSITIVE, "minimum_sale_price"))
        else:
            minimum = stated.value.amount_krw
    elif stated is None or stated.status is not FieldStatus.ABSENT:
        reasons.append(_review(MINIMUM_SALE_PRICE_UNRESOLVED, "minimum_sale_price"))

    if reasons or purchase is None or shipping_fee is None:
        return tuple(reasons)
    return SourceInputs(purchase, shipping_fee, minimum)


def offer_source_inputs(
    fields: Mapping[str, SourceField],
    offer: OfferTerms,
    currency: str,
    *,
    apply_minimum: bool = True,
) -> SourceInputs | tuple[Reason, ...]:
    """The SOURCE_OFFER pricing inputs, or every reason they cannot be read unambiguously.

    The purchase cost is the bound offer's own total, and only while the revision still states
    that exact tier as a product-level offer. The generic ``prices`` field is never read. With
    ``apply_minimum`` false (ADR-0039 §3) the generic minimum is not an input either, so it no
    longer stops pricing; the minimum the source stated is carried beside the price.
    """
    reasons: list[Reason] = []
    evidence, stated = product_level_offers(fields, currency)
    if evidence is not QuantityOfferEvidence.PRODUCT_LEVEL or offer not in stated:
        reasons.append(_review(QUANTITY_OFFER_UNRESOLVED, "quantity_tiers"))

    shipping_fee = _supplier_shipping(fields, reasons)

    if not apply_minimum:
        if reasons or shipping_fee is None:
            return tuple(reasons)
        amount, unresolved = _stated_minimum(fields)
        return SourceInputs(offer.total_price_krw, shipping_fee, None, False, amount, unresolved)

    minimum = fields.get("minimum_sale_price")
    if minimum is None:
        reasons.append(_review(MINIMUM_SALE_PRICE_UNRESOLVED, "minimum_sale_price"))
    elif minimum.status is not FieldStatus.ABSENT:
        # The generic minimum names no offer: it is never applied to every quantity, and never
        # multiplied by one (ruling 5738760913 §7, ADR-0013 ruling C).
        reasons.append(_review(MINIMUM_SALE_PRICE_NOT_OFFER_BOUND, "minimum_sale_price"))

    if reasons or shipping_fee is None:
        return tuple(reasons)
    return SourceInputs(offer.total_price_krw, shipping_fee, None)


# ---------------------------------------------------------------- source currency (ADR-0038 §4)


def _local_delivery(
    fields: Mapping[str, SourceField], currency: str, reasons: list[Reason]
) -> str | None:
    """The supplier's local delivery a source-currency revision states unambiguously, as decimal
    text in its currency (``"0"`` when free), or ``None`` with its reason. The same rules as a KRW
    shipping fee: a free-over policy is read at its base fee (ADR-0034 §2), any other condition and
    an unknown policy are not read. It enters the declared value only, never the cost."""
    shipping = fields.get("shipping")
    if (
        shipping is None
        or shipping.status is not FieldStatus.CONFIRMED
        or not isinstance(shipping.value, ForeignShippingValue)
        or shipping.value.currency != currency
    ):
        reasons.append(_review(SHIPPING_UNRESOLVED, "shipping"))
    elif shipping.value.kind is ShippingKind.FREE:
        return "0"
    elif shipping.value.fee is not None and (
        shipping.value.kind is ShippingKind.FIXED
        or (
            shipping.value.kind is ShippingKind.CONDITIONAL and shipping.value.free_over is not None
        )
    ):
        return shipping.value.fee
    elif shipping.value.kind is ShippingKind.CONDITIONAL:
        reasons.append(_review(SHIPPING_CONDITIONAL, "shipping"))
    else:
        reasons.append(_review(SHIPPING_UNKNOWN, "shipping"))
    return None


def _positive(amount: str) -> bool:
    """Whether decimal text states more than zero."""
    return any(digit not in "0." for digit in amount)


def _stated_foreign_minimum(
    fields: Mapping[str, SourceField], currency: str
) -> tuple[str | None, bool]:
    """ADR-0039 §3 for a source currency: the positive minimum a source states, and whether a
    statement exists that could not be read, for a policy that does not apply it."""
    stated = fields.get("minimum_sale_price")
    if stated is not None and stated.status is FieldStatus.ABSENT:
        return None, False
    if (
        stated is not None
        and stated.status is FieldStatus.CONFIRMED
        and isinstance(stated.value, ForeignMoneyValue)
        and stated.value.currency == currency
        and _positive(stated.value.amount)
    ):
        return stated.value.amount, False
    return None, True


def foreign_source_inputs(
    fields: Mapping[str, SourceField],
    currency: str,
    *,
    apply_minimum: bool = True,
    unit_price: str | None = None,
) -> ForeignSourceInputs | tuple[Reason, ...]:
    """The inputs of a source-currency revision (ADR-0038 §4), as stated in ``currency``, or every
    reason they cannot be read unambiguously. Nothing is converted here.

    The source price is the one PURCHASE-role foreign price (ADR-0032 §3: never chosen by label,
    name or order; a LIST price never is), or ``unit_price`` — an AtomicSKU configuration's own
    price — when one is given, and then the generic prices are not read. A value in another
    currency, or in won, is never read as this one. The minimum follows ADR-0039 §3 exactly as
    :func:`source_inputs` does."""
    reasons: list[Reason] = []
    price = unit_price
    if price is None:
        prices = fields.get("prices")
        if prices is None or prices.status is not FieldStatus.CONFIRMED:
            reasons.append(_review(PURCHASE_PRICE_UNRESOLVED, "prices"))
        elif (
            not isinstance(prices.value, ForeignPricesValue)
            or prices.value.currency != currency
            or prices.value.purchase() is None
        ):
            reasons.append(_review(PURCHASE_PRICE_AMBIGUOUS, "prices"))
        else:
            chosen = prices.value.purchase()
            assert chosen is not None
            price = chosen.amount

    local_delivery = _local_delivery(fields, currency, reasons)

    if not apply_minimum:
        if reasons or price is None or local_delivery is None:
            return tuple(reasons)
        amount, unresolved = _stated_foreign_minimum(fields, currency)
        return ForeignSourceInputs(currency, price, local_delivery, None, False, amount, unresolved)

    minimum = None
    stated = fields.get("minimum_sale_price")
    if stated is not None and stated.status is FieldStatus.CONFIRMED:
        if not isinstance(stated.value, ForeignMoneyValue) or stated.value.currency != currency:
            reasons.append(_review(MINIMUM_SALE_PRICE_UNRESOLVED, "minimum_sale_price"))
        elif not _positive(stated.value.amount):
            reasons.append(_review(MINIMUM_SALE_PRICE_NOT_POSITIVE, "minimum_sale_price"))
        else:
            minimum = stated.value.amount
    elif stated is None or stated.status is not FieldStatus.ABSENT:
        reasons.append(_review(MINIMUM_SALE_PRICE_UNRESOLVED, "minimum_sale_price"))

    if reasons or price is None or local_delivery is None:
        return tuple(reasons)
    return ForeignSourceInputs(currency, price, local_delivery, minimum)


def landed_source_inputs(landed: LandedCost) -> SourceInputs:
    """The pricing inputs of a landed cost: the supplier cost and any included duties are the
    purchase cost, the overseas shipping is the supplier shipping, and a stated minimum is the one
    the landed cost converted (or kept beside, ADR-0039 §3)."""
    inputs = landed.inputs
    return SourceInputs(
        purchase_cost_krw=landed.purchase_cost_krw,
        supplier_shipping_krw=landed.overseas_shipping_krw,
        minimum_sale_price_krw=landed.minimum_sale_price_krw,
        minimum_applied=inputs.minimum_applied,
        stated_minimum_sale_price_krw=landed.stated_minimum_sale_price_krw,
        minimum_unresolved=inputs.minimum_unresolved,
        landed_cost=landed,
    )


# ---------------------------------------------------------------- the calculation


@dataclass(frozen=True)
class Calculation:
    """One exact pricing result. ``expected_net_margin`` is exact; ``expected_net_margin_bp`` is
    its floor in basis points, for display only, and never decides a guard."""

    platform_fee_krw: int
    other_policy_cost_krw: int
    target_margin_price_krw: int
    final_sale_price_krw: int
    price_basis: PriceBasis
    expected_net_profit_krw: int
    expected_net_margin: Fraction
    expected_net_margin_bp: int
    price_guard: PriceGuard
    guard_reasons: tuple[GuardReason, ...]


def _net_profit(inputs: SourceInputs, context: PricingContextInput, price: int) -> int:
    return (
        price
        - inputs.purchase_cost_krw
        - inputs.supplier_shipping_krw
        - context.platform_fee(price)
        - context.other_policy_cost(price)
    )


def meets_target(inputs: SourceInputs, context: PricingContextInput, price: int) -> bool:
    return Fraction(_net_profit(inputs, context, price), price) >= TARGET_NET_MARGIN


def target_margin_price(inputs: SourceInputs, context: PricingContextInput) -> int | None:
    """The least whole-KRW price whose exact net margin is at least 35%, or ``None`` when the
    context's rates leave no price that reaches it.

    With ``K`` the fixed costs and ``d = 1 − 35% − fee rate − other rate``, a price ``P`` meets the
    target only if ``d·P ≥ K`` (each ceiling is at least its argument), and it certainly does once
    ``d·P ≥ K + 2`` (each ceiling adds less than one). The search runs upwards from the first
    bound, so the price found is the least, and the one below it fails.
    """
    headroom = 1 - TARGET_NET_MARGIN - context.fee_rate_value - context.other_cost_rate_value
    if headroom <= 0:
        return None
    fixed = (
        inputs.purchase_cost_krw
        + inputs.supplier_shipping_krw
        + context.fee_fixed_krw
        + context.other_cost_fixed_krw
    )
    price = max(1, _ceil(Fraction(fixed) / headroom))
    ceiling = _ceil(Fraction(fixed + 2) / headroom) + 1
    while not meets_target(inputs, context, price):
        price += 1
        if price > ceiling:  # pragma: no cover - impossible by the bound above
            raise PricingError("the target-margin search passed its proven bound")
    return price


def calculate(inputs: SourceInputs, context: PricingContextInput) -> Calculation | Reason:
    """Apply the canonical rule and both guards to one Item under one context."""
    target = target_margin_price(inputs, context)
    if target is None:
        return _review(TARGET_MARGIN_UNREACHABLE, "pricing_context")
    if inputs.minimum_sale_price_krw is not None:
        final_sale_price = inputs.minimum_sale_price_krw
        price_basis = PriceBasis.MINIMUM_SALE_PRICE
    else:
        final_sale_price = target
        price_basis = PriceBasis.TARGET_MARGIN
    platform_fee = context.platform_fee(final_sale_price)
    other_costs = context.other_policy_cost(final_sale_price)
    profit = _net_profit(inputs, context, final_sale_price)
    margin = Fraction(profit, final_sale_price)
    loss = (
        inputs.purchase_cost_krw + inputs.supplier_shipping_krw + platform_fee >= final_sale_price
    )
    below = margin < MINIMUM_NET_MARGIN
    reasons = tuple(
        reason
        for reason, applies in (
            (GuardReason.PRICE_LOSS, loss),
            (GuardReason.PRICE_BELOW_MIN_MARGIN, below),
        )
        if applies
    )
    guard = PriceGuard.LOSS if loss else PriceGuard.BELOW_MIN_MARGIN if below else PriceGuard.OK
    return Calculation(
        platform_fee_krw=platform_fee,
        other_policy_cost_krw=other_costs,
        target_margin_price_krw=target,
        final_sale_price_krw=final_sale_price,
        price_basis=price_basis,
        expected_net_profit_krw=profit,
        expected_net_margin=margin,
        expected_net_margin_bp=(profit * 10000) // final_sale_price,
        price_guard=guard,
        guard_reasons=reasons,
    )


# ---------------------------------------------------------------- dependencies


@dataclass(frozen=True)
class PricingDependencies:
    """Everything a snapshot was computed against. Any change makes the snapshot historical."""

    item_id: str
    product_group_id: str
    composition_signature: str
    signature_version: str
    membership_revision_id: str
    source_binding_id: str
    binding_kind: str
    quantity_offer_id: str | None
    fulfillment_quantity: int
    source_revision_id: str
    # ADR-0039 §3 (SP-03): whether the supplier's sales policy applies the minimum sale price.
    minimum_applied: bool = True
    # ADR-0038 §4 (FX-02): every input of a source-currency price's landed cost
    # (``LandedCost.dependency``); None for a KRW price.
    landed_cost: tuple[tuple[str, object], ...] | None = None

    def fingerprint(self, context: PricingContextInput) -> str:
        return digest(
            {
                # Named only when the minimum is not applied, so every snapshot priced under the
                # default policy keeps its fingerprint, and a policy change reprices the Item.
                **({} if self.minimum_applied else {"minimum_applied": False}),
                # Named only for a source-currency price, so every KRW fingerprint is unchanged,
                # and a new rate, setting or product input reprices a foreign one.
                **({} if self.landed_cost is None else {"landed_cost": dict(self.landed_cost)}),
                "version": DEPENDENCY_VERSION,
                "pricing_rule_version": PRICING_RULE_VERSION,
                "item_id": self.item_id,
                "product_group_id": self.product_group_id,
                "composition_signature": self.composition_signature,
                "signature_version": self.signature_version,
                "membership_revision_id": self.membership_revision_id,
                "source_binding_id": self.source_binding_id,
                "binding_kind": self.binding_kind,
                "quantity_offer_id": self.quantity_offer_id,
                "fulfillment_quantity": self.fulfillment_quantity,
                "source_revision_id": self.source_revision_id,
                "pricing_context_fingerprint": context.fingerprint,
                "fee_table_version": context.fee_table_version,
                "pricing_policy_version": context.pricing_policy_version,
            }
        )
