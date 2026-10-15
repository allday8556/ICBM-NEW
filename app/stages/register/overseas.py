"""ADR-0038 §6 (O3): the cross-border inputs of one 구매대행 provider-listing unit.

A unit is **foreign** when an Item's pinned PricingSnapshot is a landed-cost price: its source
revision states a source currency (FX-01) and the price pins it (FX-02). Only then does anything
here apply. A KRW unit reads nothing, states no reason, adds nothing to the preparation fingerprint
and nothing to its payload, so its Snapshot and its wire bytes are exactly as before.

What a foreign unit carries, and from whom — read, never decided or recomputed here:

- **the duty decision** of each Item (``NOT_OVER_150``, ``INCLUDED``, ``EXCLUDED``): its pinned
  PricingSnapshot's (FX-04). The listing states what the cost assumed: the duties are included in
  the price unless the user excluded them, and the marketplace adapter projects exactly that;
- **the source currency**: the pin's;
- **the source country**: the country the Item's supplier *declares* (``SupplierProfile.country``,
  ADR-0038 §2: a declared property of the supplier), never inferred from the currency or a page;
- **the overseas 출고지** (the SmartStore 주소록 number) and **the importer label**: the overseas
  settings (ADR-0038 §5) as they hold now, with the revision they come from.

**Fail closed.** Each missing or contradictory input is ``REVIEW_REQUIRED`` and nothing is
projected from a guess: no overseas 출고지 (``REGISTER_OVERSEAS_SHIPPING_ADDRESS_UNSET``) or one
that is no address-book number (``..._INVALID``); no importer (``..._IMPORTER_UNSET``); no account
delivery policy to carry the 출고지 (``..._DELIVERY_POLICY_MISSING``); Items of one listing that do
not agree on the currency, the country or whether the buyer pays the duties, or that mix a KRW
price with a foreign one (``..._ITEMS_INCONSISTENT``); a foreign Item on a marketplace without a
cross-border projection (``..._MARKETPLACE_UNSUPPORTED``); and no settings reader at all
(``..._SETTINGS_UNAVAILABLE``).

**What ICBM does not decide** (FX-05, ADR-0018 §5): whether a category may be sold by 구매대행 is
the seller's judgement. Nothing here judges a category.

Pure but for :class:`OverseasListingReader`, which only composes the reads it is handed.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from app.capabilities.overseas.settings import OverseasSettings
from app.stages.products.landed_cost import DutyDecision
from app.stages.products.model import ReadinessStatus, Reason

OVERSEAS_LISTING_VERSION: Final = "overseas-listing/v1"
# The marketplaces with an adopted cross-border projection (ADR-0038 §6 is SmartStore's).
OVERSEAS_MARKETPLACES: Final = frozenset({"smartstore"})
# A SmartStore 주소록 number is a positive int64 on the wire.
MAX_ADDRESS_ID: Final = 2**63 - 1

OVERSEAS_SETTINGS_UNAVAILABLE: Final = "REGISTER_OVERSEAS_SETTINGS_UNAVAILABLE"
OVERSEAS_SHIPPING_ADDRESS_UNSET: Final = "REGISTER_OVERSEAS_SHIPPING_ADDRESS_UNSET"
OVERSEAS_SHIPPING_ADDRESS_INVALID: Final = "REGISTER_OVERSEAS_SHIPPING_ADDRESS_INVALID"
OVERSEAS_IMPORTER_UNSET: Final = "REGISTER_OVERSEAS_IMPORTER_UNSET"
OVERSEAS_DELIVERY_POLICY_MISSING: Final = "REGISTER_OVERSEAS_DELIVERY_POLICY_MISSING"
OVERSEAS_ITEMS_INCONSISTENT: Final = "REGISTER_OVERSEAS_ITEMS_INCONSISTENT"
OVERSEAS_MARKETPLACE_UNSUPPORTED: Final = "REGISTER_OVERSEAS_MARKETPLACE_UNSUPPORTED"
OVERSEAS_REASON_CODES: Final = frozenset(
    {
        OVERSEAS_SETTINGS_UNAVAILABLE,
        OVERSEAS_SHIPPING_ADDRESS_UNSET,
        OVERSEAS_SHIPPING_ADDRESS_INVALID,
        OVERSEAS_IMPORTER_UNSET,
        OVERSEAS_DELIVERY_POLICY_MISSING,
        OVERSEAS_ITEMS_INCONSISTENT,
        OVERSEAS_MARKETPLACE_UNSUPPORTED,
    }
)

_R: Final = ReadinessStatus.REVIEW_REQUIRED


@dataclass(frozen=True)
class OverseasTerms:
    """The overseas settings a 구매대행 listing projects (ADR-0038 §5), as their owner holds them
    now, with the revision they come from (``None`` while the defaults apply)."""

    shipping_address_id: str | None
    importer_label: str | None
    delivery_days_min: int
    delivery_days_max: int
    settings_revision_no: int | None = None
    settings_fingerprint: str | None = None

    @classmethod
    def of(cls, settings: OverseasSettings) -> "OverseasTerms":
        return cls(
            shipping_address_id=settings.smartstore_overseas_shipping_address_id,
            importer_label=settings.importer_label,
            delivery_days_min=settings.overseas_delivery_days_min,
            delivery_days_max=settings.overseas_delivery_days_max,
            settings_revision_no=settings.revision_no,
            settings_fingerprint=settings.fingerprint,
        )

    def canonical(self) -> dict[str, object]:
        return {
            "shipping_address_id": self.shipping_address_id,
            "importer_label": self.importer_label,
            "delivery_days_min": self.delivery_days_min,
            "delivery_days_max": self.delivery_days_max,
            "settings_revision_no": self.settings_revision_no,
            "settings_fingerprint": self.settings_fingerprint,
        }


@dataclass(frozen=True)
class OverseasSource:
    """One Item's supplier and the country that supplier declares (``None``: it declares none,
    or the Item has no current source)."""

    item_id: str
    supplier_key: str | None
    source_country: str | None


@dataclass(frozen=True)
class OverseasReading:
    """Everything current a foreign unit's cross-border projection reads: the settings and each
    Item's declared source country. It enters the preparation fingerprint of a foreign unit only."""

    terms: OverseasTerms
    sources: tuple[OverseasSource, ...] = ()

    def country_of(self, item_id: str) -> str | None:
        return next((s.source_country for s in self.sources if s.item_id == item_id), None)

    def canonical(self) -> dict[str, object]:
        return {
            "version": OVERSEAS_LISTING_VERSION,
            "terms": self.terms.canonical(),
            "sources": sorted([s.item_id, s.supplier_key, s.source_country] for s in self.sources),
        }


class OverseasReader(Protocol):
    """The read a preflight makes for a foreign unit's Items."""

    def __call__(self, item_ids: Sequence[str]) -> OverseasReading: ...


class OverseasListingReader:
    """Composes the three owners' reads: the overseas settings (``OverseasSettingsStore.current``),
    each Item's supplier (``SourceGates.suppliers``) and the supplier definition's declared country
    (``integrations.suppliers.registry.source_country``), injected so REGISTER reaches no supplier
    package."""

    def __init__(
        self,
        *,
        settings: Callable[[], OverseasSettings],
        suppliers: Callable[[Sequence[str]], Sequence[tuple[str, str | None]]],
        country_of: Callable[[str], str | None],
    ) -> None:
        self._settings = settings
        self._suppliers = suppliers
        self._country_of = country_of

    def __call__(self, item_ids: Sequence[str]) -> OverseasReading:
        return OverseasReading(
            terms=OverseasTerms.of(self._settings()),
            sources=tuple(
                OverseasSource(item_id, key, None if key is None else self._country_of(key))
                for item_id, key in self._suppliers(item_ids)
            ),
        )


@dataclass(frozen=True)
class ForeignPin:
    """What one Item's pinned PricingSnapshot states about its currency and duties: both ``None``
    for a KRW price."""

    item_id: str
    source_currency: str | None
    duty_decision: str | None


def is_foreign(pins: Sequence[ForeignPin]) -> bool:
    return any(pin.source_currency is not None for pin in pins)


def address_number(text: str | None) -> int | None:
    """The 주소록 number the settings state, as the positive int64 the wire carries; ``None`` for
    none or for text that is not one."""
    if text is None or not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if 0 < number <= MAX_ADDRESS_ID else None


@dataclass(frozen=True)
class OverseasListing:
    """The cross-border section a foreign unit's Snapshot freezes (``payload["overseas"]``): the
    marketplace adapter projects the wire from it, and from nothing else."""

    source_currency: str
    source_country: str | None
    duty_decisions: tuple[str, ...]
    shipping_address_id: int
    importer: str
    settings_revision_no: int | None
    settings_fingerprint: str | None

    @property
    def duties_included(self) -> bool:
        """FX-04: the duties are in the price unless the user excluded them."""
        return DutyDecision.EXCLUDED.value not in self.duty_decisions

    def canonical(self) -> dict[str, object]:
        return {
            "version": OVERSEAS_LISTING_VERSION,
            "source_currency": self.source_currency,
            "source_country": self.source_country,
            "duty_decisions": list(self.duty_decisions),
            "shipping_address_id": self.shipping_address_id,
            "importer": self.importer,
            "settings_revision_no": self.settings_revision_no,
            "settings_fingerprint": self.settings_fingerprint,
        }


@dataclass(frozen=True)
class OverseasSummary:
    """What the register editor shows of a foreign unit's projection inputs, as read — also when
    a reason keeps it from being projected."""

    source_currencies: tuple[str, ...]
    source_countries: tuple[str, ...]
    duty_decisions: tuple[str, ...]
    shipping_address_id: str | None
    importer: str | None
    delivery_days_min: int | None
    delivery_days_max: int | None
    settings_revision_no: int | None

    @property
    def duties_included(self) -> bool | None:
        """FX-04: the duties are in the price unless the user excluded them; ``None`` when the
        Items disagree or none is pinned."""
        terms = {d == DutyDecision.EXCLUDED.value for d in self.duty_decisions}
        return None if len(terms) != 1 else not terms.pop()


@dataclass(frozen=True)
class OverseasAssessment:
    """The reasons a foreign unit is not projectable, its frozen section when it is, and what it
    read. A KRW unit's assessment is :data:`NOT_FOREIGN`: nothing at all."""

    reasons: tuple[Reason, ...] = ()
    listing: OverseasListing | None = None
    summary: OverseasSummary | None = None


NOT_FOREIGN: Final = OverseasAssessment()


def assess(
    marketplace_key: str,
    pins: Sequence[ForeignPin],
    reading: OverseasReading | None,
    *,
    delivery_policy: bool,
) -> OverseasAssessment:
    """The cross-border assessment of one unit from its Items' pins (an Item without a pin is not
    one of them: its own reason already holds the unit back) and the current reading."""
    if not is_foreign(pins):
        return NOT_FOREIGN
    currencies = tuple(sorted({p.source_currency for p in pins if p.source_currency is not None}))
    decisions = tuple(sorted({p.duty_decision for p in pins if p.duty_decision is not None}))
    if marketplace_key not in OVERSEAS_MARKETPLACES:
        return OverseasAssessment(
            (Reason(OVERSEAS_MARKETPLACE_UNSUPPORTED, _R, "overseas"),),
            summary=OverseasSummary(currencies, (), decisions, None, None, None, None, None),
        )
    if reading is None:
        return OverseasAssessment(
            (Reason(OVERSEAS_SETTINGS_UNAVAILABLE, _R, "overseas:settings"),),
            summary=OverseasSummary(currencies, (), decisions, None, None, None, None, None),
        )
    terms = reading.terms
    countries = {reading.country_of(p.item_id) for p in pins}
    importer = (terms.importer_label or "").strip() or None
    summary = OverseasSummary(
        source_currencies=currencies,
        source_countries=tuple(sorted(c for c in countries if c is not None)),
        duty_decisions=decisions,
        shipping_address_id=terms.shipping_address_id,
        importer=importer,
        delivery_days_min=terms.delivery_days_min,
        delivery_days_max=terms.delivery_days_max,
        settings_revision_no=terms.settings_revision_no,
    )
    reasons: list[Reason] = []
    if (
        any(p.source_currency is None or p.duty_decision is None for p in pins)
        or len(currencies) != 1
        or len(countries) != 1
        or (DutyDecision.EXCLUDED.value in decisions and len(decisions) > 1)
    ):
        # One listing states one origin, one currency's price and one customs term.
        reasons.append(Reason(OVERSEAS_ITEMS_INCONSISTENT, _R, "overseas"))
    if not delivery_policy:
        # The 출고지 travels in the delivery policy's claimDeliveryInfo; there is none to carry it.
        reasons.append(Reason(OVERSEAS_DELIVERY_POLICY_MISSING, _R, "overseas:delivery_policy"))
    number = address_number(terms.shipping_address_id)
    if terms.shipping_address_id is None:
        reasons.append(Reason(OVERSEAS_SHIPPING_ADDRESS_UNSET, _R, "overseas:shipping_address"))
    elif number is None:
        reasons.append(Reason(OVERSEAS_SHIPPING_ADDRESS_INVALID, _R, "overseas:shipping_address"))
    if importer is None:
        reasons.append(Reason(OVERSEAS_IMPORTER_UNSET, _R, "overseas:importer"))
    if reasons or number is None or importer is None:
        return OverseasAssessment(tuple(reasons), summary=summary)
    (currency,) = currencies
    (country,) = countries
    return OverseasAssessment(
        (),
        OverseasListing(
            source_currency=currency,
            source_country=country,
            duty_decisions=decisions,
            shipping_address_id=number,
            importer=importer,
            settings_revision_no=terms.settings_revision_no,
            settings_fingerprint=terms.settings_fingerprint,
        ),
        summary,
    )
