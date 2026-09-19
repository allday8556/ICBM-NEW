"""Product-level quantity offers (Issue #80 PR-Q; ADR-0013 §2, §5–§6; ruling 5738760913).

Pure: no database, no clock, no I/O.

A source revision that states ``options = ABSENT`` and ``quantity_tiers = CONFIRMED`` states
product-level offers. Each confirmed tier is one exact purchase condition of the source product
itself, ``(quantity, total_price)``, and becomes one immutable, revision-scoped ``QuantityOffer``.
``options = ABSENT`` proves that the source stated no SKU or configuration, so such an offer names
no SourceSKU, and none is ever fabricated for it.

Nothing else is read as an offer:
- ``quantity_tiers`` ABSENT states none (the BASE_PRODUCT rule may apply instead);
- ``quantity_tiers`` REVIEW_REQUIRED: the source's totals are not settled, and M4 never chooses one;
- ``options`` CONFIRMED or REVIEW_REQUIRED: a tier may belong to a configuration, and mapping a tier
  to a source SKU is outside this scope;
- a revision in another currency, or a tier value that does not read back.

Every total is kept exactly as the source states it. It is never divided into a unit price, never
rebuilt from one price and a quantity, and never replaced by a generic ``prices`` entry.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from app.collect.facts import CURRENCY, FieldStatus, QuantityTiersValue

# The rule the materializer applies to read offers from a current source revision.
QUANTITY_OFFER_RULE_VERSION: Final = "quantity-offer-rule/v1"


class QuantityOfferEvidence(StrEnum):
    """What the current revision states about product-level quantity offers.

    ``PRODUCT_LEVEL``  ``options`` ABSENT and ``quantity_tiers`` CONFIRMED, in KRW: an offer a tier.
    ``NONE_STATED``    ``quantity_tiers`` ABSENT: no offer exists to read.
    ``NOT_PROVEN``     anything else. No offer is guessed from it.
    """

    PRODUCT_LEVEL = "PRODUCT_LEVEL"
    NONE_STATED = "NONE_STATED"
    NOT_PROVEN = "NOT_PROVEN"


class OfferField(Protocol):
    @property
    def status(self) -> FieldStatus: ...

    @property
    def value(self) -> object: ...


@dataclass(frozen=True)
class OfferTerms:
    """One confirmed tier exactly as its revision states it, at its source position."""

    tier_ordinal: int
    quantity: int
    total_price_krw: int


def product_level_offers(
    fields: Mapping[str, OfferField], currency: str
) -> tuple[QuantityOfferEvidence, tuple[OfferTerms, ...]]:
    """The product-level offers a revision states, in source order, and the evidence for them."""
    tiers = fields.get("quantity_tiers")
    if tiers is not None and tiers.status is FieldStatus.ABSENT:
        return QuantityOfferEvidence.NONE_STATED, ()
    options = fields.get("options")
    if (
        tiers is None
        or tiers.status is not FieldStatus.CONFIRMED
        or not isinstance(tiers.value, QuantityTiersValue)
        or options is None
        or options.status is not FieldStatus.ABSENT
        or currency != CURRENCY
    ):
        return QuantityOfferEvidence.NOT_PROVEN, ()
    return QuantityOfferEvidence.PRODUCT_LEVEL, tuple(
        OfferTerms(ordinal, tier.quantity, tier.total_price_krw)
        for ordinal, tier in enumerate(tiers.value.tiers)
    )
