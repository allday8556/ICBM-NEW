"""What a sales-policy warning quotes (ADR-0039 §2, §3, SP-04).

A warning that a supplier's sales policy is not applied says the policy: it names the supplier and
quotes the supplier's own words. The warning reasons themselves stay codes — a ``Reason`` never
carries page content, and no preparation, snapshot or fingerprint changes for this — so the words
are read here, for the screen to render beside the warning:

- **the supplier**: the key of the Item's binding member's source product and its display name
  (the supplier catalog's names, the one place a supplier is named);
- **the sales channels**: a CONFIRMED statement's ``policy_text`` (the page's own words, at most
  200 characters, ADR-0031 §2); for a statement no rule could read, the first evidence text the
  collector observed for it, if any;
- **the minimum resale price**: a CONFIRMED amount with its source label and its currency — KRW,
  or the source currency of an overseas page (ADR-0038 §1), never converted; for an unread one,
  the first observed evidence text.

It reads the Item's **current** source revision through the same procurement every enforcing owner
reads (``current_procurement``), and the supplier's policy as it holds now. It reads; it writes
nothing and decides nothing: whether a warning exists is the source gates' and pricing's verdict.
"""

from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.stages.collect.facts import (
    FieldStatus,
    ForeignMoneyValue,
    MoneyValue,
    SalesChannelScope,
    SalesChannelsValue,
)
from app.stages.collect.models import ProductFactsEvidence
from app.stages.products.models import SourceProduct
from app.stages.products.pricing_service import current_procurement
from app.stages.products.sales_policy import SalesPolicy, sales_policy_of
from app.stages.products.store import FieldReading, ProductFoundationStore

SALES_CHANNELS_FIELD: Final = "sales_channels"
MINIMUM_FIELD: Final = "minimum_sale_price"
# A quote is the supplier's words, bounded as a sales-channel statement's own text is (ADR-0031 §2).
QUOTE_MAX_CHARS: Final = 200
KRW: Final = "KRW"


class ChannelStatementView(BaseModel):
    """What the source says about where the product may be resold. ``quote`` is the supplier's
    words: the CONFIRMED statement's ``policy_text``, or for an unread statement the first text
    the collector observed for it (``None`` when it observed none)."""

    status: FieldStatus
    scope: SalesChannelScope | None = None
    forbidden: tuple[str, ...] = ()
    quote: str | None = None


class MinimumStatementView(BaseModel):
    """The minimum resale price the source states. A CONFIRMED one has its source ``label``, its
    ``currency`` (``KRW`` or the page's source currency) and its ``amount`` as exact decimal text,
    never converted; an unread one has the first observed text as ``quote``."""

    status: FieldStatus
    label: str | None = None
    currency: str | None = None
    amount: str | None = None
    quote: str | None = None


class SalesPolicyNoteView(BaseModel):
    """What a sales-policy warning on this Item quotes: the supplier, its policy now, and its own
    words. A statement the current revision does not hold is ``None``."""

    item_id: str
    supplier_key: str | None
    supplier_name: str | None
    source_revision_id: str | None
    apply_channel_restrictions: bool
    apply_minimum_price: bool
    sales_channels: ChannelStatementView | None
    minimum_sale_price: MinimumStatementView | None


def clip(text: str) -> str:
    """The supplier's words, whitespace-trimmed and bounded; a cut is marked, never hidden."""
    words = " ".join(text.split())
    return words if len(words) <= QUOTE_MAX_CHARS else words[: QUOTE_MAX_CHARS - 1] + "…"


def _observed(session: Session, revision_id: str, field_key: str) -> str | None:
    """The first non-empty text the collector observed for one field, in evidence order."""
    for observed in session.scalars(
        select(ProductFactsEvidence.observed)
        .where(
            ProductFactsEvidence.revision_id == revision_id,
            ProductFactsEvidence.field_key == field_key,
        )
        .order_by(ProductFactsEvidence.ordinal)
    ):
        if observed is not None and observed.strip():
            return clip(observed)
    return None


def channel_statement(
    session: Session, revision_id: str, reading: FieldReading
) -> ChannelStatementView:
    value = reading.value
    if reading.status is FieldStatus.CONFIRMED and isinstance(value, SalesChannelsValue):
        return ChannelStatementView(
            status=reading.status,
            scope=value.scope,
            forbidden=value.forbidden,
            quote=clip(value.policy_text),
        )
    return ChannelStatementView(
        status=reading.status, quote=_observed(session, revision_id, SALES_CHANNELS_FIELD)
    )


def minimum_statement(
    session: Session, revision_id: str, reading: FieldReading
) -> MinimumStatementView:
    value = reading.value
    if reading.status is FieldStatus.CONFIRMED:
        if isinstance(value, MoneyValue):
            return MinimumStatementView(
                status=reading.status,
                label=clip(value.label),
                currency=KRW,
                amount=str(value.amount_krw),
            )
        if isinstance(value, ForeignMoneyValue):
            return MinimumStatementView(
                status=reading.status,
                label=clip(value.label),
                currency=value.currency,
                amount=value.amount,
            )
    return MinimumStatementView(
        status=reading.status, quote=_observed(session, revision_id, MINIMUM_FIELD)
    )


class SalesPolicyNotes:
    """The supplier and its words, for the warnings of one Item (ADR-0039 §2, §3)."""

    def __init__(self, store: ProductFoundationStore, names: Mapping[str, str]) -> None:
        self._store = store
        # The supplier catalog's display names, by supplier key (live: rebound with the catalog).
        self._names = names

    def note(self, item_id: str) -> SalesPolicyNoteView:
        """One Item's note, from one read. An unknown Item is ``PRODUCTS_ITEM_UNKNOWN``."""
        with self._store.reading() as unit:
            procurement = current_procurement(unit, item_id)
            member = procurement.member
            supplier_key = (
                None
                if member is None
                else unit.session.execute(
                    select(SourceProduct.supplier_key).where(
                        SourceProduct.source_product_uid == member.source_product_uid
                    )
                ).scalar_one_or_none()
            )
            policy = (
                SalesPolicy("")
                if supplier_key is None
                else sales_policy_of(unit.session, supplier_key)
            )
            revision = None if member is None else procurement.current_revision_id
            readings = (
                {}
                if revision is None
                else unit.source_fields(revision, (SALES_CHANNELS_FIELD, MINIMUM_FIELD))
            )
            channels = readings.get(SALES_CHANNELS_FIELD)
            minimum = readings.get(MINIMUM_FIELD)
            return SalesPolicyNoteView(
                item_id=item_id,
                supplier_key=supplier_key,
                supplier_name=(
                    None if supplier_key is None else self._names.get(supplier_key, supplier_key)
                ),
                source_revision_id=revision,
                apply_channel_restrictions=policy.apply_channel_restrictions,
                apply_minimum_price=policy.apply_minimum_price,
                sales_channels=(
                    None
                    if channels is None or revision is None
                    else channel_statement(unit.session, revision, channels)
                ),
                minimum_sale_price=(
                    None
                    if minimum is None or revision is None
                    else minimum_statement(unit.session, revision, minimum)
                ),
            )
