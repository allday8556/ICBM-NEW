"""Persistence of the M4 canonical product foundation (Issue #80 PR-B, ADR-0013).

Nine tables, in the order the migration creates them:

- ``source_products``: the supplier's product identity ``(supplier_key, source_product_id)``,
  exactly one row per pair. It holds no fact: facts stay in the immutable
  ``product_facts_revisions`` of ADR-0010.
- ``current_source_revision_moves``: the append-only history of each source product's current
  source revision pointer. It is never an "accepted" or confirmed revision (ADR-0013 §3).
- ``product_groups``: the canonical Product. It **is** the Canonical v3.1 ``ProductGroup``, so
  there is one entity and one identifier (ADR-0013 §1).
- ``group_members``: a group's link to a source product, never to one revision.
- ``group_membership_revisions``: immutable snapshots of a group's CONFIRMED member set.
- ``group_change_events``: MERGE/SPLIT lineage. PR-B defines it and performs neither.
- ``listing_compositions``: immutable seller multiplicity, identified by its canonical signature.
- ``product_items``: the product-side Item, ``product_group_id + composition_signature``.
- ``source_bindings``: current procurement. Only ``BASE_PRODUCT`` can be stored; ``SOURCE_OFFER``
  needs referential source SKU and offer entities that do not exist yet.

CHECK constraints repeat the single-row invariants. The cross-row invariants — same source
identity, pointer chain, complete membership snapshots, and a confirmed member bound to its own
group's Item — are enforced by the triggers of migration 0012, so no write path can bypass them.
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime
from app.products.model import (
    SIGNATURE_VERSION,
    BindingKind,
    ChangeEventType,
    GroupStatus,
    MemberStatus,
    MoveReason,
)
from app.products.pricing import (
    PRICING_RULE_VERSION,
    GuardReason,
    PriceBasis,
    PriceGuard,
    PricingMoveReason,
    Rounding,
)


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _json_array(column: str, *, minimum: int) -> str:
    return (
        f"json_valid({column}) AND json_type({column}) = 'array'"
        f" AND json_array_length({column}) >= {minimum}"
    )


def _decimal(column: str) -> str:
    return f"{column} IS NULL OR ({column} <> '' AND {column} NOT GLOB '*[^0-9.]*')"


class SourceProduct(Base):
    """One supplier product identity. Its facts are its ProductFactsRevision history."""

    __tablename__ = "source_products"
    __table_args__ = (
        UniqueConstraint("supplier_key", "source_product_id"),
        CheckConstraint("supplier_key <> ''", name="supplier_key_present"),
        CheckConstraint("source_product_id <> ''", name="source_identity_present"),
    )

    source_product_uid: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(String(40))
    source_product_id: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CurrentSourceRevisionMove(Base):
    """One move of a source product's current source revision pointer. The newest move names
    the current source revision; the pointer is never inferred from the revision sequence."""

    __tablename__ = "current_source_revision_moves"
    __table_args__ = (
        UniqueConstraint("source_product_uid", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(_in("reason", MoveReason), name="reason_valid"),
        CheckConstraint(
            "(sequence = 1) = (previous_revision_id IS NULL)", name="first_move_has_no_previous"
        ),
        CheckConstraint("(reason = 'INITIAL') = (sequence = 1)", name="initial_opens_history"),
        CheckConstraint(
            "previous_revision_id IS NULL OR previous_revision_id <> revision_id",
            name="move_changes_revision",
        ),
        CheckConstraint("rule_version IS NULL OR rule_version <> ''", name="rule_version_present"),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    move_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_product_uid: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_products.source_product_uid")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    previous_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    reason: Mapped[str] = mapped_column(String(30))
    rule_version: Mapped[str | None] = mapped_column(String(64))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ProductGroup(Base):
    """The canonical Product (ADR-0013 §1). It carries no source, no marketplace policy and no
    duplicate allowance: those belong to members, bindings and M5 overrides."""

    __tablename__ = "product_groups"
    __table_args__ = (
        CheckConstraint(_in("status", GroupStatus), name="status_valid"),
        CheckConstraint(
            "(status = 'RETIRED') = (retired_at IS NOT NULL)", name="retired_when_retired_at"
        ),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
    )

    product_group_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20))
    decided_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class GroupMember(Base):
    """A group's link to a source product. Only a CONFIRMED member is canonical, and a source
    product is CONFIRMED in at most one group at a time."""

    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("product_group_id", "source_product_uid"),
        Index(
            "ux_group_members_confirmed_source",
            "source_product_uid",
            unique=True,
            sqlite_where=text("status = 'CONFIRMED'"),
        ),
        CheckConstraint(_in("status", MemberStatus), name="status_valid"),
        CheckConstraint("match_method IS NULL OR match_method <> ''", name="match_method_present"),
        CheckConstraint(
            "match_confidence IS NULL OR (match_confidence >= 0 AND match_confidence <= 1)",
            name="match_confidence_range",
        ),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
    )

    member_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    source_product_uid: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_products.source_product_uid")
    )
    status: Mapped[str] = mapped_column(String(20))
    match_method: Mapped[str | None] = mapped_column(String(40))
    match_confidence: Mapped[float | None] = mapped_column(Float)
    match_strategy_version: Mapped[str | None] = mapped_column(String(64))
    decided_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime)


class GroupMembershipRevision(Base):
    """An immutable, numbered snapshot of a group's complete CONFIRMED member set."""

    __tablename__ = "group_membership_revisions"
    __table_args__ = (
        UniqueConstraint("product_group_id", "revision_no"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint(_json_array("members_json", minimum=0), name="members_is_array"),
        CheckConstraint(
            "member_count >= 0 AND json_array_length(members_json) = member_count",
            name="member_count_matches",
        ),
        CheckConstraint("reason <> ''", name="reason_present"),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    membership_revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    members_json: Mapped[str] = mapped_column(Text)
    member_count: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(40))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class GroupChangeEvent(Base):
    """MERGE/SPLIT lineage (Canonical v3.1 §6.8). Defined here; no merge or split runs in PR-B."""

    __tablename__ = "group_change_events"
    __table_args__ = (
        CheckConstraint(_in("event_type", ChangeEventType), name="event_type_valid"),
        CheckConstraint(
            _json_array("predecessor_group_ids", minimum=1), name="predecessors_is_array"
        ),
        CheckConstraint(_json_array("successor_group_ids", minimum=1), name="successors_is_array"),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(10))
    predecessor_group_ids: Mapped[str] = mapped_column(Text)
    successor_group_ids: Mapped[str] = mapped_column(Text)
    membership_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("group_membership_revisions.membership_revision_id")
    )
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ListingComposition(Base):
    """An immutable seller multiplicity. Its identity is the canonical structural signature; an
    unknown unit field stays NULL and is part of that signature."""

    __tablename__ = "listing_compositions"
    __table_args__ = (
        UniqueConstraint("composition_signature"),
        CheckConstraint("quantity >= 1", name="quantity_positive"),
        CheckConstraint(_decimal("unit_amount"), name="unit_amount_decimal"),
        CheckConstraint(
            "unit_code IS NULL OR (unit_code <> '' AND unit_code NOT GLOB '*[^a-z0-9]*')",
            name="unit_code_token",
        ),
        CheckConstraint("(unit_amount IS NULL) = (unit_code IS NULL)", name="unit_stated_together"),
        CheckConstraint("pack_count IS NULL OR pack_count >= 1", name="pack_count_positive"),
        CheckConstraint(
            "units_per_pack IS NULL OR units_per_pack >= 1", name="units_per_pack_positive"
        ),
        CheckConstraint(_decimal("total_amount"), name="total_amount_decimal"),
        CheckConstraint("total_amount IS NULL OR unit_code IS NOT NULL", name="total_has_unit"),
        CheckConstraint(_hex64("composition_signature"), name="signature_hex"),
        CheckConstraint(f"signature_version = '{SIGNATURE_VERSION}'", name="signature_version"),
    )

    composition_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_amount: Mapped[str | None] = mapped_column(String(40))
    unit_code: Mapped[str | None] = mapped_column(String(16))
    pack_count: Mapped[int | None] = mapped_column(Integer)
    units_per_pack: Mapped[int | None] = mapped_column(Integer)
    total_amount: Mapped[str | None] = mapped_column(String(40))
    composition_signature: Mapped[str] = mapped_column(String(64))
    signature_version: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ProductItem(Base):
    """The product-side sellable Item: its identity is ``product_group_id + composition_signature``.
    It holds no marketplace or listing identity; that is M5."""

    __tablename__ = "product_items"
    __table_args__ = (
        UniqueConstraint("product_group_id", "composition_signature"),
        CheckConstraint(_hex64("composition_signature"), name="signature_hex"),
    )

    item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    composition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listing_compositions.composition_id")
    )
    composition_signature: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SourceBinding(Base):
    """Where an Item is currently procured (ADR-0013 §6). A binding is closed, never edited, and
    at most one is open per Item. There is no source SKU or offer column: a ``BASE_PRODUCT``
    binding has none, and ``SOURCE_OFFER`` cannot be stored until referential entities exist."""

    __tablename__ = "source_bindings"
    __table_args__ = (
        Index(
            "ux_source_bindings_one_open_per_item",
            "item_id",
            unique=True,
            sqlite_where=text("valid_to IS NULL"),
        ),
        CheckConstraint(_in("binding_kind", BindingKind), name="binding_kind_valid"),
        CheckConstraint("binding_kind <> 'SOURCE_OFFER'", name="source_offer_unavailable"),
        CheckConstraint("fulfillment_quantity >= 1", name="fulfillment_quantity_positive"),
        CheckConstraint(
            "binding_kind <> 'BASE_PRODUCT' OR fulfillment_quantity = 1",
            name="base_product_single_unit",
        ),
        CheckConstraint(_json_array("provenance_fields", minimum=1), name="provenance_is_array"),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="validity_ordered"),
    )

    binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_items.item_id"))
    group_member_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_members.member_id"))
    binding_kind: Mapped[str] = mapped_column(String(20))
    fulfillment_quantity: Mapped[int] = mapped_column(Integer)
    provenance_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    provenance_fields: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime)


# ---------------------------------------------------------------- pricing (migration 0013)
#
# One immutable, exact pricing result per Item and explicit pricing context, and the append-only
# history of which one is current. The CHECK constraints make the canonical rule and the guards
# structural: the final price is the minimum sale price when one exists and the target-margin price
# otherwise, never a maximum of the two; the fee and the other policy costs are the context's exact
# ceilings; the guard and every guard reason follow from exact integer comparisons. Migration 0013's
# triggers make a snapshot price exactly the current procurement it names.


def _rate(prefix: str) -> str:
    return (
        f"{prefix}_rate_denominator >= 1 AND {prefix}_rate_numerator >= 0"
        f" AND {prefix}_rate_numerator < {prefix}_rate_denominator AND {prefix}_fixed_krw >= 0"
    )


def _ceiling_cost(column: str, prefix: str) -> str:
    return (
        f"{column} = ({prefix}_rate_numerator * final_sale_price_krw + {prefix}_rate_denominator"
        f" - 1) / {prefix}_rate_denominator + {prefix}_fixed_krw"
    )


_LOSS = "purchase_cost_krw + supplier_shipping_krw + platform_fee_krw >= final_sale_price_krw"
_BELOW = (
    "expected_net_profit_krw * minimum_margin_denominator"
    " < final_sale_price_krw * minimum_margin_numerator"
)


class PricingSnapshot(Base):
    __tablename__ = "pricing_snapshots"
    __table_args__ = (
        Index("ix_pricing_snapshots_item_context", "item_id", "pricing_context_fingerprint"),
        CheckConstraint("marketplace_key <> ''", name="marketplace_key_present"),
        CheckConstraint("account_id IS NULL OR account_id <> ''", name="account_id_present"),
        CheckConstraint("fee_table_version <> ''", name="fee_table_version_present"),
        CheckConstraint("pricing_policy_version <> ''", name="pricing_policy_version_present"),
        CheckConstraint(_hex64("pricing_context_fingerprint"), name="context_fingerprint_hex"),
        CheckConstraint(
            "json_valid(pricing_context_json) AND json_type(pricing_context_json) = 'object'",
            name="context_is_object",
        ),
        CheckConstraint(_hex64("dependency_fingerprint"), name="dependency_fingerprint_hex"),
        CheckConstraint(_hex64("composition_signature"), name="signature_hex"),
        CheckConstraint(_in("pricing_rule_version", (PRICING_RULE_VERSION,)), name="rule_known"),
        CheckConstraint(
            "purchase_cost_krw >= 0 AND supplier_shipping_krw >= 0"
            " AND (minimum_sale_price_krw IS NULL OR minimum_sale_price_krw > 0)",
            name="source_amounts_valid",
        ),
        CheckConstraint(_rate("fee"), name="fee_rate_valid"),
        CheckConstraint(_rate("other_cost"), name="other_cost_rate_valid"),
        CheckConstraint(
            f"{_in('cost_rounding', Rounding)} AND {_in('price_rounding', Rounding)}",
            name="rounding_declared",
        ),
        CheckConstraint(
            "target_margin_denominator >= 1 AND minimum_margin_denominator >= 1"
            f" AND (pricing_rule_version <> '{PRICING_RULE_VERSION}'"
            " OR (target_margin_numerator * 100 = 35 * target_margin_denominator"
            " AND minimum_margin_numerator * 100 = 10 * minimum_margin_denominator))",
            name="policy_margins",
        ),
        CheckConstraint(
            "target_margin_price_krw >= 1 AND final_sale_price_krw >= 1", name="prices_positive"
        ),
        # CLAUDE.md §6.1: the minimum sale price when it exists, else the target-margin price.
        CheckConstraint(
            "final_sale_price_krw = COALESCE(minimum_sale_price_krw, target_margin_price_krw)"
            " AND (minimum_sale_price_krw IS NULL) = (price_basis = 'TARGET_MARGIN')"
            f" AND {_in('price_basis', PriceBasis)}",
            name="canonical_rule",
        ),
        CheckConstraint(_ceiling_cost("platform_fee_krw", "fee"), name="platform_fee_exact"),
        CheckConstraint(
            _ceiling_cost("other_policy_cost_krw", "other_cost"), name="other_policy_cost_exact"
        ),
        CheckConstraint(
            "expected_net_profit_krw = final_sale_price_krw - purchase_cost_krw"
            " - supplier_shipping_krw - platform_fee_krw - other_policy_cost_krw",
            name="profit_exact",
        ),
        CheckConstraint(
            "expected_net_margin_bp * final_sale_price_krw <= expected_net_profit_krw * 10000"
            " AND expected_net_profit_krw * 10000"
            " < (expected_net_margin_bp + 1) * final_sale_price_krw",
            name="margin_bp_floor",
        ),
        CheckConstraint(
            f"{_in('price_guard', PriceGuard)} AND (price_guard = 'LOSS') = ({_LOSS})"
            f" AND (price_guard = 'BELOW_MIN_MARGIN') = (NOT ({_LOSS}) AND {_BELOW})",
            name="guard_exact",
        ),
        CheckConstraint(
            "json_valid(guard_reasons_json) AND json_type(guard_reasons_json) = 'array'"
            f" AND (instr(guard_reasons_json, '\"{GuardReason.PRICE_LOSS}\"') > 0) = ({_LOSS})"
            f" AND (instr(guard_reasons_json, '\"{GuardReason.PRICE_BELOW_MIN_MARGIN}\"') > 0)"
            f" = ({_BELOW})"
            f" AND json_array_length(guard_reasons_json) = ({_LOSS}) + ({_BELOW})",
            name="guard_reasons_exact",
        ),
    )

    pricing_snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_items.item_id"))
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    composition_signature: Mapped[str] = mapped_column(String(64))
    marketplace_key: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str | None] = mapped_column(String(64))
    fee_table_version: Mapped[str] = mapped_column(String(64))
    pricing_policy_version: Mapped[str] = mapped_column(String(64))
    pricing_context_fingerprint: Mapped[str] = mapped_column(String(64))
    pricing_context_json: Mapped[str] = mapped_column(Text)
    membership_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("group_membership_revisions.membership_revision_id")
    )
    source_binding_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_bindings.binding_id")
    )
    source_product_facts_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    dependency_fingerprint: Mapped[str] = mapped_column(String(64))
    pricing_rule_version: Mapped[str] = mapped_column(String(40))
    purchase_cost_krw: Mapped[int] = mapped_column(Integer)
    supplier_shipping_krw: Mapped[int] = mapped_column(Integer)
    minimum_sale_price_krw: Mapped[int | None] = mapped_column(Integer)
    fee_rate_numerator: Mapped[int] = mapped_column(Integer)
    fee_rate_denominator: Mapped[int] = mapped_column(Integer)
    fee_fixed_krw: Mapped[int] = mapped_column(Integer)
    other_cost_rate_numerator: Mapped[int] = mapped_column(Integer)
    other_cost_rate_denominator: Mapped[int] = mapped_column(Integer)
    other_cost_fixed_krw: Mapped[int] = mapped_column(Integer)
    cost_rounding: Mapped[str] = mapped_column(String(20))
    price_rounding: Mapped[str] = mapped_column(String(20))
    target_margin_numerator: Mapped[int] = mapped_column(Integer)
    target_margin_denominator: Mapped[int] = mapped_column(Integer)
    minimum_margin_numerator: Mapped[int] = mapped_column(Integer)
    minimum_margin_denominator: Mapped[int] = mapped_column(Integer)
    platform_fee_krw: Mapped[int] = mapped_column(Integer)
    other_policy_cost_krw: Mapped[int] = mapped_column(Integer)
    target_margin_price_krw: Mapped[int] = mapped_column(Integer)
    final_sale_price_krw: Mapped[int] = mapped_column(Integer)
    price_basis: Mapped[str] = mapped_column(String(20))
    expected_net_profit_krw: Mapped[int] = mapped_column(Integer)
    expected_net_margin_bp: Mapped[int] = mapped_column(Integer)
    price_guard: Mapped[str] = mapped_column(String(20))
    guard_reasons_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CurrentPricingSnapshotMove(Base):
    __tablename__ = "current_pricing_snapshot_moves"
    __table_args__ = (
        UniqueConstraint("item_id", "pricing_context_fingerprint", "sequence"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(_in("reason", PricingMoveReason), name="reason_valid"),
        CheckConstraint(
            "(sequence = 1) = (previous_pricing_snapshot_id IS NULL)",
            name="first_move_has_no_previous",
        ),
        CheckConstraint("(reason = 'INITIAL') = (sequence = 1)", name="initial_opens_history"),
        CheckConstraint(
            "previous_pricing_snapshot_id IS NULL"
            " OR previous_pricing_snapshot_id <> pricing_snapshot_id",
            name="move_changes_snapshot",
        ),
        CheckConstraint(_hex64("pricing_context_fingerprint"), name="context_fingerprint_hex"),
        CheckConstraint("rule_version <> ''", name="rule_version_present"),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    move_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_items.item_id"))
    pricing_context_fingerprint: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer)
    pricing_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pricing_snapshots.pricing_snapshot_id")
    )
    previous_pricing_snapshot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pricing_snapshots.pricing_snapshot_id")
    )
    reason: Mapped[str] = mapped_column(String(20))
    rule_version: Mapped[str] = mapped_column(String(40))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)
