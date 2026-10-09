"""What an Item's source forbids about registering it (ADR-0031 §4, ADR-0030 §7).

Two source-side gates, read for the registration preflight and nowhere decided twice:

- **sales channels** (ADR-0031 §4.1): the current source revision of the Item's binding member
  states where the product may be resold. A marketplace the page forbids is
  ``SOURCE_CHANNEL_FORBIDDEN``; a statement no rule could read is ``SOURCE_CHANNEL_UNRESOLVED``.
  No statement, or a revision recorded before the field existed, forbids nothing.
- **supplier status** (ADR-0030 §7): an Item whose supplier is a site still in ``RECON`` is
  ``SUPPLIER_NOT_ACTIVE``. So is one whose current revision a platform template produced (its
  extraction revision is ``<template>+<site>``) while no ACTIVE site of that key is configured: a
  site whose file was removed or no longer validates never reaches a marketplace (PT-06, PT-07).

Every marketplace ICBM registers to is an open marketplace, so ``CLOSED_MALL_ONLY`` forbids each
of them (SC-03). It reads; it writes nothing.
"""

from collections.abc import Callable, Sequence
from typing import Final

from sqlalchemy import select

from app.stages.collect.facts import FieldStatus, SalesChannelScope, SalesChannelsValue
from app.stages.collect.models import ProductFactsRevision
from app.stages.products.models import SourceProduct
from app.stages.products.pricing_service import current_procurement
from app.stages.products.store import ProductFoundationStore

SOURCE_CHANNEL_FORBIDDEN: Final = "SOURCE_CHANNEL_FORBIDDEN"
SOURCE_CHANNEL_UNRESOLVED: Final = "SOURCE_CHANNEL_UNRESOLVED"
SUPPLIER_NOT_ACTIVE: Final = "SUPPLIER_NOT_ACTIVE"
SALES_CHANNELS_FIELD: Final = "sales_channels"
SOURCE_GATE_CODES: Final = frozenset(
    {SOURCE_CHANNEL_FORBIDDEN, SOURCE_CHANNEL_UNRESOLVED, SUPPLIER_NOT_ACTIVE}
)
# A platform template's extraction revision joins the template and the site (ADR-0030 §4).
TEMPLATE_REVISION_JOIN: Final = "+"


def channel_verdict(status: FieldStatus, value: object, marketplace_key: str) -> str | None:
    """ADR-0031 §4.1's table: the gate a sales-channel reading puts on one marketplace."""
    if status is FieldStatus.ABSENT:
        return None
    if status is not FieldStatus.CONFIRMED or not isinstance(value, SalesChannelsValue):
        return SOURCE_CHANNEL_UNRESOLVED
    if value.scope is SalesChannelScope.CLOSED_MALL_ONLY:
        return SOURCE_CHANNEL_FORBIDDEN
    if value.scope is SalesChannelScope.LISTED and marketplace_key in value.forbidden:
        return SOURCE_CHANNEL_FORBIDDEN
    return None


class SourceGates:
    """The source-side gates of Items, for one marketplace."""

    def __init__(
        self, store: ProductFoundationStore, site_active: Callable[[str], bool | None]
    ) -> None:
        """``site_active(key)`` is True for an ACTIVE site, False for one in RECON, and None for
        a key that is no configured site at all (the KM package, or a site that is gone)."""
        self._store = store
        self._site_active = site_active

    def __call__(
        self, marketplace_key: str, item_ids: Sequence[str]
    ) -> tuple[tuple[str, str], ...]:
        """``(item_id, code)`` for every gate the Items' sources put on ``marketplace_key``."""
        gates: list[tuple[str, str]] = []
        with self._store.reading() as unit:
            for item_id in item_ids:
                procurement = current_procurement(unit, item_id)
                member = procurement.member
                if member is None:
                    continue  # no source to read: pricing and readiness already say why
                supplier_key = unit.session.execute(
                    select(SourceProduct.supplier_key).where(
                        SourceProduct.source_product_uid == member.source_product_uid
                    )
                ).scalar_one_or_none()
                revision = procurement.current_revision_id
                extractor = (
                    None
                    if revision is None
                    else unit.session.execute(
                        select(ProductFactsRevision.extractor_revision).where(
                            ProductFactsRevision.revision_id == revision
                        )
                    ).scalar_one_or_none()
                )
                active = None if supplier_key is None else self._site_active(supplier_key)
                from_template = extractor is not None and TEMPLATE_REVISION_JOIN in extractor
                if active is False or (from_template and active is not True):
                    gates.append((item_id, SUPPLIER_NOT_ACTIVE))
                if revision is None:
                    continue
                reading = unit.source_fields(revision, (SALES_CHANNELS_FIELD,)).get(
                    SALES_CHANNELS_FIELD
                )
                if reading is None:
                    continue  # recorded before ADR-0031: the field did not exist (§6)
                verdict = channel_verdict(reading.status, reading.value, marketplace_key)
                if verdict is not None:
                    gates.append((item_id, verdict))
        return tuple(gates)
