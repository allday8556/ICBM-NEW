"""The landed cost of one purchase-agency unit (ADR-0038 §4; FX-02, FX-03, FX-04): exact, pure.

The pricing owner (ADR-0013 §7) reads the FX snapshots, the overseas settings and the product's
own inputs in its unit of work and hands them here; this module decides the landed cost and the
pins a snapshot records, and nothing else. The result enters the one canonical calculation
unchanged (``app.stages.products.pricing``).

**The formula** (§4)::

    supplier_cost_krw = ceil(source_price × TTS_per_unit × (1 + fx_margin%))
    landed_cost_krw   = supplier_cost_krw + overseas_shipping_krw + duties_and_taxes_krw

- ``source_price`` is the price an order of one unit pays now: the PURCHASE-role foreign price of
  the revision (ADR-0038 §9 amendment: Super Delivery's consumption tax is added at extraction,
  so the recorded price is used as recorded), or, for an AtomicSKU Item, its configuration's own
  unit price.
- ``TTS_per_unit`` is the snapshot's TTS divided by its publication unit (100 for JPY), exactly.
- The calculation enters it as ``purchase_cost_krw = supplier_cost_krw + duties_and_taxes_krw``
  and ``supplier_shipping_krw = overseas_shipping_krw``, so the existing profit, loss and margin
  rules keep their meaning: the cost of the goods, and the cost of bringing one unit to the buyer.

**The source's own local delivery is not added to the cost.** ``overseas_shipping_krw`` already
covers the supplier's local delivery, the freight forwarder and the international freight (§4) —
it is the user's own figure, per product or the settings default. The delivery the page states
(in the source currency) is read for one purpose only: the declared value of the USD 150 test.

**Duties and taxes** (§4, FX-04). The declared value is the source price plus the stated local
delivery, both in the source currency, at the same day's TTS, through won into US dollars — exact
fractions, no margin. At or under USD 150 (:data:`DUTY_FREE_LIMIT_USD`, ICBM's versioned policy
constant, owner ``6109161005``) duties are 0 (``NOT_OVER_150``). Above it, and included by the
settings (the default): ``customs_value_krw = ceil(declared value in won)`` (the same TTS, without
the FX margin: the margin is the seller's own buffer, never part of a customs value), then
``duty = ceil(customs_value_krw × duty_rate)`` and ``VAT = ceil((customs_value_krw + duty) ×
10%)`` (``INCLUDED``). Above it and excluded by the user: 0, and the snapshot records ``EXCLUDED``
so the listing can say the buyer pays them (O3).

**A stated minimum sale price** in the source currency (ADR-0039 §3) is converted at the same TTS
without the margin, rounded up to the next won, and then applied — or kept beside the price —
exactly as a KRW minimum is: a stated minimum is never undercut by a currency, and whether it
applies stays the supplier sales policy's choice.

**Exact arithmetic.** Every amount is a :class:`~fractions.Fraction` read from decimal text; a
float never enters. Rounding is the pricing rule's own: up to the next whole won.

**Fail closed** (FX-03). A missing or stale rate — of the source currency or of USD — is the FX
owner's reason (``PRICING_FX_UNAVAILABLE``, ``PRICING_FX_STALE``); no overseas shipping at all is
``PRICING_OVERSEAS_SHIPPING_UNSET``. Each is REVIEW_REQUIRED, and nothing is priced on a guess.
"""

from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import date
from enum import StrEnum
from fractions import Fraction
from typing import Final, Protocol

from app.capabilities.overseas.fx_rules import (
    PRICING_FX_STALE,
    PRICING_FX_UNAVAILABLE,
    FxSnapshot,
    fx_reason,
)
from app.capabilities.overseas.settings import OverseasSettings
from app.stages.products.model import ReadinessStatus, Reason

# Versioned with the formula, the USD 150 threshold and the import VAT rate; pinned in every
# foreign price's dependency fingerprint, so a patch to any of them reprices.
LANDED_COST_POLICY_VERSION: Final = "landed-cost/v1"
DUTY_FREE_LIMIT_USD: Final = Fraction(150)
IMPORT_VAT_RATE: Final = Fraction(10, 100)
USD: Final = "USD"
_BP: Final = 10000

# Reason codes: our own, never page content.
OVERSEAS_SHIPPING_UNSET: Final = "PRICING_OVERSEAS_SHIPPING_UNSET"
FX_STALE: Final = PRICING_FX_STALE
FX_UNAVAILABLE: Final = PRICING_FX_UNAVAILABLE


class LandedCostError(ValueError):
    """A landed-cost pin that does not state one consistent foreign price."""


class OverseasShippingSource(StrEnum):
    """Where the overseas shipping of a price came from: the product's own input, or the
    settings default."""

    PRODUCT = "PRODUCT"
    DEFAULT = "DEFAULT"


class DutyDecision(StrEnum):
    """FX-04: how duties entered a price."""

    NOT_OVER_150 = "NOT_OVER_150"  # declared value at or under USD 150: duty-free list clearance
    INCLUDED = "INCLUDED"  # above USD 150, included in the cost (the default)
    EXCLUDED = "EXCLUDED"  # above USD 150, excluded by the user: the buyer pays them


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _review(code: str, subject: str) -> Reason:
    return Reason(code, ReadinessStatus.REVIEW_REQUIRED, subject)


def fx_subject(currency: str) -> str:
    """The subject a rate's reason names: ``fx:CNY``, ``fx:USD``."""
    return f"fx:{currency}"


@dataclass(frozen=True)
class ForeignSourceInputs:
    """What a source-currency revision states for pricing, exactly as stated (decimal text in
    its currency). ``local_delivery`` is the stated supplier's delivery (``"0"`` when free), read
    only for the declared value. ``minimum`` is the stated minimum the policy applies; with the
    policy not applying it, ``stated_minimum`` (or ``minimum_unresolved``) is kept beside."""

    currency: str
    source_price: str
    local_delivery: str
    minimum: str | None = None
    minimum_applied: bool = True
    stated_minimum: str | None = None
    minimum_unresolved: bool = False


@dataclass(frozen=True)
class OverseasCostTerms:
    """The user's terms for one landed cost: the overseas settings, with the product's own inputs
    over their defaults. ``overseas_shipping_krw`` is ``None`` when neither states one."""

    fx_margin_bp: int
    duties_included_over_usd150: bool
    duty_rate_bp: int
    overseas_shipping_krw: int | None
    overseas_shipping_source: OverseasShippingSource | None
    settings_revision_no: int | None
    settings_fingerprint: str | None
    override_revision_no: int | None


class ProductCostInputs(Protocol):
    """A source product's own landed-cost inputs (``overseas_costs.OverseasCostOverride``)."""

    @property
    def overseas_shipping_krw(self) -> int | None: ...

    @property
    def duty_rate_bp(self) -> int | None: ...

    @property
    def revision_no(self) -> int | None: ...


def cost_terms(settings: OverseasSettings, override: ProductCostInputs) -> OverseasCostTerms:
    """The product's own overseas shipping and duty rate where it states them, else the settings'
    defaults (§4, §8 phase 1)."""
    if override.overseas_shipping_krw is not None:
        shipping: int | None = override.overseas_shipping_krw
        source: OverseasShippingSource | None = OverseasShippingSource.PRODUCT
    elif settings.overseas_shipping_default_krw is not None:
        shipping, source = settings.overseas_shipping_default_krw, OverseasShippingSource.DEFAULT
    else:
        shipping, source = None, None
    return OverseasCostTerms(
        fx_margin_bp=settings.fx_margin_bp,
        duties_included_over_usd150=settings.duties_included_over_usd150,
        duty_rate_bp=(
            settings.duty_rate_bp if override.duty_rate_bp is None else override.duty_rate_bp
        ),
        overseas_shipping_krw=shipping,
        overseas_shipping_source=source,
        settings_revision_no=settings.revision_no,
        settings_fingerprint=settings.fingerprint,
        override_revision_no=override.revision_no,
    )


@dataclass(frozen=True)
class DayRates:
    """The source currency's and USD's snapshots of one publication day."""

    rate: FxSnapshot
    usd: FxSnapshot


def same_day_rates(
    currency: str, today: date, newest: Callable[[str, date], FxSnapshot | None]
) -> DayRates | tuple[Reason, ...]:
    """The source currency's and USD's rates of one publication day, the newest that may price
    (FX-03), or every reason they cannot.

    ``newest(currency, day)`` is the FX owner's read: the newest snapshot published on or before
    the day. Each currency is judged on its own newest rate first; when both may price but were
    published on different days (one publication is recorded whole, so only a partial day does
    this), the older day is the pair's, and a currency without a rate of that very day is
    ``PRICING_FX_UNAVAILABLE`` — a declared value is never read across two days.
    """
    rate, usd = newest(currency, today), newest(USD, today)
    reasons = tuple(
        _review(code, fx_subject(name))
        for name, snapshot in ((currency, rate), (USD, usd))
        if (code := fx_reason(snapshot, today)) is not None
    )
    if reasons:
        return reasons
    assert rate is not None and usd is not None  # fx_reason names a missing rate
    if rate.publication_date != usd.publication_date:
        day = min(rate.publication_date, usd.publication_date)
        rate, usd = newest(currency, day), newest(USD, day)
        missing = tuple(
            _review(FX_UNAVAILABLE, fx_subject(name))
            for name, snapshot in ((currency, rate), (USD, usd))
            if snapshot is None or snapshot.publication_date != day
        )
        if missing:
            return missing
        assert rate is not None and usd is not None
    return DayRates(rate, usd)


def _per_unit(snapshot: FxSnapshot) -> Fraction:
    """The won price of one unit of the currency, exactly (the unit is 1, or 100 for JPY)."""
    return Fraction(snapshot.tts) / snapshot.unit


def _cents_up(value: Fraction) -> str:
    """A dollar amount as decimal text, rounded up to the cent. Rounding up keeps the threshold's
    verdict: 150 is a whole cent, so the text is above 150 exactly when the value is."""
    cents = _ceil(value * 100)
    return f"{cents // 100}.{cents % 100:02d}"


@dataclass(frozen=True)
class LandedCostPin:
    """What a pricing snapshot of a foreign price records (FX-02), column for column, in
    ``pricing_snapshots`` and ``atomic_sku_pricing_snapshots`` (migration 0065). A KRW price
    records none of it: every column stays NULL."""

    source_currency: str
    source_price: str
    fx_snapshot_id: str
    fx_publication_date: str
    usd_fx_snapshot_id: str
    fx_margin_bp: int
    overseas_settings_revision_no: int | None
    overseas_cost_override_revision_no: int | None
    overseas_shipping_krw: int
    overseas_shipping_source: OverseasShippingSource
    duty_decision: DutyDecision
    duty_rate_bp: int
    duties_krw: int
    declared_value_usd: str

    def columns(self) -> dict[str, object]:
        return {
            name: value.value if isinstance(value, StrEnum) else value
            for name, value in ((f.name, getattr(self, f.name)) for f in fields(self))
        }


LANDED_COST_COLUMNS: Final = tuple(f.name for f in fields(LandedCostPin))


def pin_columns(
    pin: LandedCostPin | None, *, purchase_cost_krw: int, supplier_shipping_krw: int
) -> dict[str, object]:
    """The landed-cost columns of one snapshot row — the store-level rule both pricing stores (the
    only writers) apply before an insert. A KRW price states none; a foreign price states every
    one, and they must describe the amounts the row records: the overseas shipping is its
    supplier shipping, the duties are inside its purchase cost, a price without included duties
    states none, and a product's own shipping names the product's revision it came from."""
    if pin is None:
        return dict.fromkeys(LANDED_COST_COLUMNS)
    if supplier_shipping_krw != pin.overseas_shipping_krw:
        raise LandedCostError("a foreign price's supplier shipping is its overseas shipping")
    if not 0 <= pin.duties_krw <= purchase_cost_krw:
        raise LandedCostError("a foreign price's duties are part of its purchase cost")
    if pin.duty_decision is not DutyDecision.INCLUDED and pin.duties_krw != 0:
        raise LandedCostError("only included duties are a cost")
    if (
        pin.overseas_shipping_source is OverseasShippingSource.PRODUCT
        and pin.overseas_cost_override_revision_no is None
    ):
        raise LandedCostError("a product's own overseas shipping names its revision")
    return pin.columns()


def pin_of(row: object) -> LandedCostPin | None:
    """Read a snapshot row's landed-cost columns back; ``None`` for a KRW price."""
    values = {name: getattr(row, name) for name in LANDED_COST_COLUMNS}
    if values["source_currency"] is None:
        return None
    values["overseas_shipping_source"] = OverseasShippingSource(values["overseas_shipping_source"])
    values["duty_decision"] = DutyDecision(values["duty_decision"])
    return LandedCostPin(**values)


@dataclass(frozen=True)
class LandedCost:
    """One exact landed cost and everything it was computed from."""

    inputs: ForeignSourceInputs
    rate: FxSnapshot
    usd: FxSnapshot
    terms: OverseasCostTerms
    overseas_shipping_krw: int
    overseas_shipping_source: OverseasShippingSource
    supplier_cost_krw: int
    declared_value_usd: Fraction
    duty_decision: DutyDecision
    duties_krw: int
    minimum_sale_price_krw: int | None
    stated_minimum_sale_price_krw: int | None

    @property
    def purchase_cost_krw(self) -> int:
        """The cost of the goods the calculation reads: the supplier cost and included duties."""
        return self.supplier_cost_krw + self.duties_krw

    def pin(self) -> LandedCostPin:
        return LandedCostPin(
            source_currency=self.inputs.currency,
            source_price=self.inputs.source_price,
            fx_snapshot_id=self.rate.snapshot_id,
            fx_publication_date=self.rate.publication_date.isoformat(),
            usd_fx_snapshot_id=self.usd.snapshot_id,
            fx_margin_bp=self.terms.fx_margin_bp,
            overseas_settings_revision_no=self.terms.settings_revision_no,
            overseas_cost_override_revision_no=self.terms.override_revision_no,
            overseas_shipping_krw=self.overseas_shipping_krw,
            overseas_shipping_source=self.overseas_shipping_source,
            duty_decision=self.duty_decision,
            duty_rate_bp=self.terms.duty_rate_bp,
            duties_krw=self.duties_krw,
            declared_value_usd=_cents_up(self.declared_value_usd),
        )

    def dependency(self) -> tuple[tuple[str, object], ...]:
        """What a foreign price's dependency fingerprint names beyond a KRW one: every pin, the
        policy version, the settings' content fingerprint, the stated local delivery and the
        minimum the source stated. A change of a rate, a setting, the product's inputs or the
        policy moves it, and the current snapshot is STALE."""
        return tuple(
            sorted(
                {
                    **self.pin().columns(),
                    "policy_version": LANDED_COST_POLICY_VERSION,
                    "overseas_settings_fingerprint": self.terms.settings_fingerprint,
                    "local_delivery": self.inputs.local_delivery,
                    "minimum": self.inputs.minimum,
                    "stated_minimum": self.inputs.stated_minimum,
                    "minimum_unresolved": self.inputs.minimum_unresolved,
                }.items()
            )
        )


def landed_cost(
    inputs: ForeignSourceInputs, rate: FxSnapshot, usd: FxSnapshot, terms: OverseasCostTerms
) -> LandedCost | tuple[Reason, ...]:
    """The landed cost of one unit (module docstring), or the reason it cannot be made."""
    if rate.currency != inputs.currency or usd.currency != USD:
        raise LandedCostError("the rates are the source currency's and USD's")
    if rate.publication_date != usd.publication_date:
        raise LandedCostError("a declared value is read at one day's rates")
    if terms.overseas_shipping_krw is None or terms.overseas_shipping_source is None:
        return (_review(OVERSEAS_SHIPPING_UNSET, "overseas_shipping"),)
    per_unit = _per_unit(rate)
    price = Fraction(inputs.source_price)
    supplier_cost = _ceil(price * per_unit * (1 + Fraction(terms.fx_margin_bp, _BP)))
    declared_won = (price + Fraction(inputs.local_delivery)) * per_unit
    declared_usd = declared_won / _per_unit(usd)
    if declared_usd <= DUTY_FREE_LIMIT_USD:
        decision, duties = DutyDecision.NOT_OVER_150, 0
    elif terms.duties_included_over_usd150:
        customs_value = _ceil(declared_won)
        duty = _ceil(customs_value * Fraction(terms.duty_rate_bp, _BP))
        decision, duties = (
            DutyDecision.INCLUDED,
            duty + _ceil((customs_value + duty) * IMPORT_VAT_RATE),
        )
    else:
        decision, duties = DutyDecision.EXCLUDED, 0
    return LandedCost(
        inputs=inputs,
        rate=rate,
        usd=usd,
        terms=terms,
        overseas_shipping_krw=terms.overseas_shipping_krw,
        overseas_shipping_source=terms.overseas_shipping_source,
        supplier_cost_krw=supplier_cost,
        declared_value_usd=declared_usd,
        duty_decision=decision,
        duties_krw=duties,
        minimum_sale_price_krw=_won(inputs.minimum, per_unit),
        stated_minimum_sale_price_krw=_won(inputs.stated_minimum, per_unit),
    )


def _won(amount: str | None, per_unit: Fraction) -> int | None:
    """A stated minimum in won: the same TTS, no margin, up to the next won."""
    return None if amount is None else _ceil(Fraction(amount) * per_unit)


__all__ = [
    "DUTY_FREE_LIMIT_USD",
    "FX_STALE",
    "FX_UNAVAILABLE",
    "IMPORT_VAT_RATE",
    "LANDED_COST_COLUMNS",
    "LANDED_COST_POLICY_VERSION",
    "OVERSEAS_SHIPPING_UNSET",
    "DayRates",
    "DutyDecision",
    "ForeignSourceInputs",
    "LandedCost",
    "LandedCostError",
    "LandedCostPin",
    "OverseasCostTerms",
    "OverseasShippingSource",
    "ProductCostInputs",
    "cost_terms",
    "fx_subject",
    "landed_cost",
    "pin_columns",
    "pin_of",
    "same_day_rates",
]
