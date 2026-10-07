"""AtomicSKU-qualified M4 economics persistence.

The legacy no-option tables remain unchanged.  These additive v2 rows carry the exact
``ProductGroup + ListingComposition + AtomicSKU`` identity while the existing M4 pricing owner
continues to perform every calculation.
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime
from app.stages.products.pricing import (
    PRICING_RULE_VERSION,
    GuardReason,
    PriceBasis,
    PriceGuard,
    PricingMoveReason,
    Rounding,
)

ATOMIC_ACQUISITION_BASIS = "BASE_PRICE_PLUS_OPTION_DELTA"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _in(column: str, values: Iterable[object]) -> str:
    return f"{column} IN ({', '.join(repr(v.value if hasattr(v, 'value') else v) for v in values)})"


class AtomicSKUSourceBinding(Base):
    """The current exact source configuration and acquisition basis of one v2 Item."""

    __tablename__ = "atomic_sku_source_bindings"
    __table_args__ = (
        Index(
            "ux_atomic_sku_source_bindings_one_open_per_item",
            "atomic_sku_item_id",
            unique=True,
            sqlite_where=text("valid_to IS NULL"),
        ),
        CheckConstraint(
            f"acquisition_basis = '{ATOMIC_ACQUISITION_BASIS}'", name="acquisition_basis_known"
        ),
        CheckConstraint("base_purchase_cost_krw >= 0", name="base_cost_non_negative"),
        CheckConstraint(
            "purchase_cost_krw = base_purchase_cost_krw + option_additional_price_krw"
            " AND purchase_cost_krw >= 0",
            name="purchase_cost_exact",
        ),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="validity_ordered"),
    )

    binding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    atomic_sku_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_product_items.atomic_sku_item_id")
    )
    atomic_sku_revision_member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_revision_members.revision_member_id")
    )
    group_member_id: Mapped[str] = mapped_column(String(36), ForeignKey("group_members.member_id"))
    provenance_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    acquisition_basis: Mapped[str] = mapped_column(String(40))
    base_purchase_cost_krw: Mapped[int] = mapped_column(Integer)
    option_additional_price_krw: Mapped[int] = mapped_column(Integer)
    purchase_cost_krw: Mapped[int] = mapped_column(Integer)
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    valid_from: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime)


_LOSS = "purchase_cost_krw + supplier_shipping_krw + platform_fee_krw >= final_sale_price_krw"
_BELOW = (
    "expected_net_profit_krw * minimum_margin_denominator"
    " < final_sale_price_krw * minimum_margin_numerator"
)


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


class AtomicSKUPricingSnapshot(Base):
    """One immutable context-specific price of one AtomicSKU-qualified Item."""

    __tablename__ = "atomic_sku_pricing_snapshots"
    __table_args__ = (
        Index(
            "ix_atomic_sku_pricing_snapshots_item_context",
            "atomic_sku_item_id",
            "pricing_context_fingerprint",
        ),
        CheckConstraint(_hex64("composition_signature"), name="composition_signature_hex"),
        CheckConstraint(_hex64("atomic_sku_selection_signature"), name="selection_signature_hex"),
        CheckConstraint(_hex64("pricing_context_fingerprint"), name="context_fingerprint_hex"),
        CheckConstraint(_hex64("dependency_fingerprint"), name="dependency_fingerprint_hex"),
        CheckConstraint(
            f"pricing_rule_version = '{PRICING_RULE_VERSION}'", name="pricing_rule_known"
        ),
        CheckConstraint("marketplace_key <> ''", name="marketplace_key_present"),
        CheckConstraint("account_id IS NULL OR account_id <> ''", name="account_id_present"),
        CheckConstraint("fee_table_version <> ''", name="fee_table_version_present"),
        CheckConstraint("pricing_policy_version <> ''", name="pricing_policy_version_present"),
        CheckConstraint(
            "json_valid(pricing_context_json) AND json_type(pricing_context_json) = 'object'",
            name="context_is_object",
        ),
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
            "target_margin_price_krw >= 1 AND final_sale_price_krw >= 1",
            name="prices_positive",
        ),
        CheckConstraint(
            "final_sale_price_krw = COALESCE(minimum_sale_price_krw, target_margin_price_krw)"
            " AND (minimum_sale_price_krw IS NULL) = (price_basis = 'TARGET_MARGIN')"
            f" AND {_in('price_basis', PriceBasis)}",
            name="canonical_rule",
        ),
        CheckConstraint(_ceiling_cost("platform_fee_krw", "fee"), name="platform_fee_exact"),
        CheckConstraint(
            _ceiling_cost("other_policy_cost_krw", "other_cost"),
            name="other_policy_cost_exact",
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
    atomic_sku_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_product_items.atomic_sku_item_id")
    )
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    composition_signature: Mapped[str] = mapped_column(String(64))
    atomic_sku_id: Mapped[str] = mapped_column(String(36), ForeignKey("atomic_skus.atomic_sku_id"))
    atomic_sku_selection_signature: Mapped[str] = mapped_column(String(64))
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
        String(36), ForeignKey("atomic_sku_source_bindings.binding_id")
    )
    atomic_sku_revision_member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_revision_members.revision_member_id")
    )
    source_product_facts_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    dependency_fingerprint: Mapped[str] = mapped_column(String(64))
    pricing_rule_version: Mapped[str] = mapped_column(String(40))
    purchase_cost_krw: Mapped[int] = mapped_column(Integer)
    supplier_shipping_krw: Mapped[int] = mapped_column(Integer)
    minimum_sale_price_krw: Mapped[int | None] = mapped_column(Integer)
    platform_fee_krw: Mapped[int] = mapped_column(Integer)
    other_policy_cost_krw: Mapped[int] = mapped_column(Integer)
    target_margin_price_krw: Mapped[int] = mapped_column(Integer)
    final_sale_price_krw: Mapped[int] = mapped_column(Integer)
    price_basis: Mapped[str] = mapped_column(String(20))
    expected_net_profit_krw: Mapped[int] = mapped_column(Integer)
    expected_net_margin_bp: Mapped[int] = mapped_column(Integer)
    price_guard: Mapped[str] = mapped_column(String(20))
    guard_reasons_json: Mapped[str] = mapped_column(Text)
    # Exact context inputs retained like the legacy snapshots, so another owner never re-decides.
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CurrentAtomicSKUPricingSnapshotMove(Base):
    __tablename__ = "current_atomic_sku_pricing_snapshot_moves"
    __table_args__ = (
        UniqueConstraint("atomic_sku_item_id", "pricing_context_fingerprint", "sequence"),
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
    atomic_sku_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_product_items.atomic_sku_item_id")
    )
    pricing_context_fingerprint: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer)
    pricing_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_pricing_snapshots.pricing_snapshot_id")
    )
    previous_pricing_snapshot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("atomic_sku_pricing_snapshots.pricing_snapshot_id")
    )
    reason: Mapped[str] = mapped_column(String(20))
    rule_version: Mapped[str] = mapped_column(String(64))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)


__all__ = [
    "ATOMIC_ACQUISITION_BASIS",
    "AtomicSKUPricingSnapshot",
    "AtomicSKUSourceBinding",
    "CurrentAtomicSKUPricingSnapshotMove",
]
