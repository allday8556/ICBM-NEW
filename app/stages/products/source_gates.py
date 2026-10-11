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

**The supplier's sales policy** (ADR-0039 §2). Whether the channel statement is enforced is the
user's choice, per supplier (``app.stages.products.sales_policy``). With
``apply_channel_restrictions`` false a forbidding statement is
``SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED`` and an unread one
``SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED``: non-blocking warnings (:data:`SOURCE_GATE_WARNINGS`)
that still show the restriction (SP-04). The fact itself is read exactly as before (SP-01), and
every marketplace's REGISTER reads the policy through this one seam.
:meth:`SourceGates.policy_pins` names the policy revision each Item's supplier has, for the
preparation to pin (SP-03).
"""

from collections.abc import Callable, Sequence
from typing import Final

from sqlalchemy import select

from app.stages.collect.facts import FieldStatus, SalesChannelScope, SalesChannelsValue
from app.stages.collect.models import ProductFactsRevision
from app.stages.products.models import SourceProduct
from app.stages.products.pricing_service import current_procurement
from app.stages.products.sales_policy import sales_policy_of
from app.stages.products.store import ProductFoundationStore

SOURCE_CHANNEL_FORBIDDEN: Final = "SOURCE_CHANNEL_FORBIDDEN"
SOURCE_CHANNEL_UNRESOLVED: Final = "SOURCE_CHANNEL_UNRESOLVED"
SUPPLIER_NOT_ACTIVE: Final = "SUPPLIER_NOT_ACTIVE"
# ADR-0039 §2: the same two readings, for a supplier whose channel policy the user chose not to
# apply. Each is a non-blocking warning.
SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED: Final = "SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED"
SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED: Final = "SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED"
SALES_CHANNELS_FIELD: Final = "sales_channels"
SOURCE_GATE_WARNINGS: Final = frozenset(
    {SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED, SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED}
)
SOURCE_GATE_CODES: Final = frozenset(
    {SOURCE_CHANNEL_FORBIDDEN, SOURCE_CHANNEL_UNRESOLVED, SUPPLIER_NOT_ACTIVE}
    | SOURCE_GATE_WARNINGS
)
# What a forbidding or unread statement becomes when the supplier's channel policy is not applied.
_NOT_APPLIED: Final = {
    SOURCE_CHANNEL_FORBIDDEN: SOURCE_CHANNEL_RESTRICTION_NOT_APPLIED,
    SOURCE_CHANNEL_UNRESOLVED: SOURCE_CHANNEL_UNRESOLVED_NOT_APPLIED,
}
# A platform template's extraction revision joins the template and the site (ADR-0030 §4).
TEMPLATE_REVISION_JOIN: Final = "+"


def channel_verdict(
    status: FieldStatus,
    value: object,
    marketplace_key: str,
    *,
    apply_restrictions: bool = True,
) -> str | None:
    """ADR-0031 §4.1's table, amended by ADR-0039 §2: the gate a sales-channel reading puts on
    one marketplace. With ``apply_restrictions`` false the same reading is its warning."""
    verdict = _channel_verdict(status, value, marketplace_key)
    if verdict is None or apply_restrictions:
        return verdict
    return _NOT_APPLIED[verdict]


def _channel_verdict(status: FieldStatus, value: object, marketplace_key: str) -> str | None:
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
                apply = (
                    True
                    if supplier_key is None
                    else sales_policy_of(unit.session, supplier_key).apply_channel_restrictions
                )
                verdict = channel_verdict(
                    reading.status, reading.value, marketplace_key, apply_restrictions=apply
                )
                if verdict is not None:
                    gates.append((item_id, verdict))
        return tuple(gates)

    def policy_pins(self, item_ids: Sequence[str]) -> tuple[tuple[str, str, int, str], ...]:
        """``(item_id, supplier_key, revision_no, fingerprint)`` of the sales-policy revision each
        Item's supplier has now (ADR-0039 §1, SP-03). A supplier with no revision — the defaults —
        names nothing, so an Item never configured keeps its earlier preparation fingerprint."""
        pins: list[tuple[str, str, int, str]] = []
        with self._store.reading() as unit:
            for item_id in item_ids:
                member = current_procurement(unit, item_id).member
                if member is None:
                    continue
                supplier_key = unit.session.execute(
                    select(SourceProduct.supplier_key).where(
                        SourceProduct.source_product_uid == member.source_product_uid
                    )
                ).scalar_one_or_none()
                if supplier_key is None:
                    continue
                pin = sales_policy_of(unit.session, supplier_key).pin()
                if pin is not None:
                    pins.append((item_id, *pin))
        return tuple(pins)
