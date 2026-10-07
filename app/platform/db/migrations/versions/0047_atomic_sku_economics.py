"""Add AtomicSKU-qualified source bindings and pricing snapshots.

Revision ID: 0047_atomic_sku_economics
Revises: 0046_sequential_bulk_registration
Create Date: 2026-10-05

The legacy no-option economics tables are untouched.  The additive v2 tables keep the exact
AtomicSKU Item identity while the existing M4 pricing service remains the only calculator.
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0047_atomic_sku_economics"
down_revision: str | None = "0046_sequential_bulk_registration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BINDINGS = "atomic_sku_source_bindings"
SNAPSHOTS = "atomic_sku_pricing_snapshots"
MOVES = "current_atomic_sku_pricing_snapshot_moves"
ATOMIC_ACQUISITION_BASIS = "BASE_PRICE_PLUS_OPTION_DELTA"
PRICING_RULE_VERSION = "pricing-rule/v1"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _fk(table: str, column: str, target: str, target_column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.{target_column}"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def _hex(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _one_of(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


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


def _immutable(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_{event.lower()} BEFORE {event} ON {table}"
            f" BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


def upgrade() -> None:
    op.create_table(
        BINDINGS,
        sa.Column("binding_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_item_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_revision_member_id", sa.String(36), nullable=False),
        sa.Column("group_member_id", sa.String(36), nullable=False),
        sa.Column("provenance_revision_id", sa.String(36), nullable=False),
        sa.Column("acquisition_basis", sa.String(40), nullable=False),
        sa.Column("base_purchase_cost_krw", sa.Integer(), nullable=False),
        sa.Column("option_additional_price_krw", sa.Integer(), nullable=False),
        sa.Column("purchase_cost_krw", sa.Integer(), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("valid_from", sa.DateTime(), nullable=False),
        sa.Column("valid_to", sa.DateTime(), nullable=True),
        _check(
            BINDINGS, f"acquisition_basis = '{ATOMIC_ACQUISITION_BASIS}'", "acquisition_basis_known"
        ),
        _check(BINDINGS, "base_purchase_cost_krw >= 0", "base_cost_non_negative"),
        _check(
            BINDINGS,
            "purchase_cost_krw = base_purchase_cost_krw + option_additional_price_krw"
            " AND purchase_cost_krw >= 0",
            "purchase_cost_exact",
        ),
        _check(BINDINGS, "decided_by <> ''", "decided_by_present"),
        _check(BINDINGS, "correlation_id <> ''", "correlation_present"),
        _check(BINDINGS, "valid_to IS NULL OR valid_to >= valid_from", "validity_ordered"),
        _fk(BINDINGS, "atomic_sku_item_id", "atomic_sku_product_items", "atomic_sku_item_id"),
        _fk(
            BINDINGS,
            "atomic_sku_revision_member_id",
            "atomic_sku_revision_members",
            "revision_member_id",
        ),
        _fk(BINDINGS, "group_member_id", "group_members", "member_id"),
        _fk(BINDINGS, "provenance_revision_id", "product_facts_revisions", "revision_id"),
        sa.PrimaryKeyConstraint("binding_id", name=op.f(f"pk_{BINDINGS}")),
    )
    op.create_index(
        "ux_atomic_sku_source_bindings_one_open_per_item",
        BINDINGS,
        ["atomic_sku_item_id"],
        unique=True,
        sqlite_where=sa.text("valid_to IS NULL"),
    )

    op.create_table(
        SNAPSHOTS,
        sa.Column("pricing_snapshot_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_item_id", sa.String(36), nullable=False),
        sa.Column("product_group_id", sa.String(36), nullable=False),
        sa.Column("composition_signature", sa.String(64), nullable=False),
        sa.Column("atomic_sku_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_selection_signature", sa.String(64), nullable=False),
        sa.Column("marketplace_key", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(64), nullable=True),
        sa.Column("fee_table_version", sa.String(64), nullable=False),
        sa.Column("pricing_policy_version", sa.String(64), nullable=False),
        sa.Column("pricing_context_fingerprint", sa.String(64), nullable=False),
        sa.Column("pricing_context_json", sa.Text(), nullable=False),
        sa.Column("membership_revision_id", sa.String(36), nullable=False),
        sa.Column("source_binding_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_revision_member_id", sa.String(36), nullable=False),
        sa.Column("source_product_facts_revision_id", sa.String(36), nullable=False),
        sa.Column("dependency_fingerprint", sa.String(64), nullable=False),
        sa.Column("pricing_rule_version", sa.String(40), nullable=False),
        sa.Column("purchase_cost_krw", sa.Integer(), nullable=False),
        sa.Column("supplier_shipping_krw", sa.Integer(), nullable=False),
        sa.Column("minimum_sale_price_krw", sa.Integer(), nullable=True),
        sa.Column("platform_fee_krw", sa.Integer(), nullable=False),
        sa.Column("other_policy_cost_krw", sa.Integer(), nullable=False),
        sa.Column("target_margin_price_krw", sa.Integer(), nullable=False),
        sa.Column("final_sale_price_krw", sa.Integer(), nullable=False),
        sa.Column("price_basis", sa.String(20), nullable=False),
        sa.Column("expected_net_profit_krw", sa.Integer(), nullable=False),
        sa.Column("expected_net_margin_bp", sa.Integer(), nullable=False),
        sa.Column("price_guard", sa.String(20), nullable=False),
        sa.Column("guard_reasons_json", sa.Text(), nullable=False),
        sa.Column("fee_rate_numerator", sa.Integer(), nullable=False),
        sa.Column("fee_rate_denominator", sa.Integer(), nullable=False),
        sa.Column("fee_fixed_krw", sa.Integer(), nullable=False),
        sa.Column("other_cost_rate_numerator", sa.Integer(), nullable=False),
        sa.Column("other_cost_rate_denominator", sa.Integer(), nullable=False),
        sa.Column("other_cost_fixed_krw", sa.Integer(), nullable=False),
        sa.Column("cost_rounding", sa.String(20), nullable=False),
        sa.Column("price_rounding", sa.String(20), nullable=False),
        sa.Column("target_margin_numerator", sa.Integer(), nullable=False),
        sa.Column("target_margin_denominator", sa.Integer(), nullable=False),
        sa.Column("minimum_margin_numerator", sa.Integer(), nullable=False),
        sa.Column("minimum_margin_denominator", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(SNAPSHOTS, _hex("composition_signature"), "composition_signature_hex"),
        _check(SNAPSHOTS, _hex("atomic_sku_selection_signature"), "selection_signature_hex"),
        _check(SNAPSHOTS, _hex("pricing_context_fingerprint"), "context_fingerprint_hex"),
        _check(SNAPSHOTS, _hex("dependency_fingerprint"), "dependency_fingerprint_hex"),
        _check(SNAPSHOTS, f"pricing_rule_version = '{PRICING_RULE_VERSION}'", "pricing_rule_known"),
        _check(SNAPSHOTS, "marketplace_key <> ''", "marketplace_key_present"),
        _check(SNAPSHOTS, "account_id IS NULL OR account_id <> ''", "account_id_present"),
        _check(SNAPSHOTS, "fee_table_version <> ''", "fee_table_version_present"),
        _check(SNAPSHOTS, "pricing_policy_version <> ''", "pricing_policy_version_present"),
        _check(
            SNAPSHOTS,
            "json_valid(pricing_context_json) AND json_type(pricing_context_json) = 'object'",
            "context_is_object",
        ),
        _check(
            SNAPSHOTS,
            "purchase_cost_krw >= 0 AND supplier_shipping_krw >= 0"
            " AND (minimum_sale_price_krw IS NULL OR minimum_sale_price_krw > 0)",
            "source_amounts_valid",
        ),
        _check(SNAPSHOTS, _rate("fee"), "fee_rate_valid"),
        _check(SNAPSHOTS, _rate("other_cost"), "other_cost_rate_valid"),
        _check(
            SNAPSHOTS,
            "cost_rounding = 'CEIL_KRW_1' AND price_rounding = 'CEIL_KRW_1'",
            "rounding_declared",
        ),
        _check(
            SNAPSHOTS,
            "target_margin_denominator >= 1 AND minimum_margin_denominator >= 1"
            " AND (target_margin_numerator * 100 = 35 * target_margin_denominator"
            " AND minimum_margin_numerator * 100 = 10 * minimum_margin_denominator)",
            "policy_margins",
        ),
        _check(
            SNAPSHOTS,
            "target_margin_price_krw >= 1 AND final_sale_price_krw >= 1",
            "prices_positive",
        ),
        _check(
            SNAPSHOTS,
            "final_sale_price_krw = COALESCE(minimum_sale_price_krw, target_margin_price_krw)"
            " AND (minimum_sale_price_krw IS NULL) = (price_basis = 'TARGET_MARGIN')"
            " AND price_basis IN ('MINIMUM_SALE_PRICE', 'TARGET_MARGIN')",
            "canonical_rule",
        ),
        _check(SNAPSHOTS, _ceiling_cost("platform_fee_krw", "fee"), "platform_fee_exact"),
        _check(
            SNAPSHOTS,
            _ceiling_cost("other_policy_cost_krw", "other_cost"),
            "other_policy_cost_exact",
        ),
        _check(
            SNAPSHOTS,
            "expected_net_profit_krw = final_sale_price_krw - purchase_cost_krw"
            " - supplier_shipping_krw - platform_fee_krw - other_policy_cost_krw",
            "profit_exact",
        ),
        _check(
            SNAPSHOTS,
            "expected_net_margin_bp * final_sale_price_krw <= expected_net_profit_krw * 10000"
            " AND expected_net_profit_krw * 10000"
            " < (expected_net_margin_bp + 1) * final_sale_price_krw",
            "margin_bp_floor",
        ),
        _check(
            SNAPSHOTS,
            "price_guard IN ('OK', 'LOSS', 'BELOW_MIN_MARGIN')"
            " AND (price_guard = 'LOSS') = (purchase_cost_krw + supplier_shipping_krw"
            " + platform_fee_krw >= final_sale_price_krw)"
            " AND (price_guard = 'BELOW_MIN_MARGIN') = (NOT (purchase_cost_krw"
            " + supplier_shipping_krw + platform_fee_krw >= final_sale_price_krw)"
            " AND expected_net_profit_krw * minimum_margin_denominator"
            " < final_sale_price_krw * minimum_margin_numerator)",
            "guard_exact",
        ),
        _check(
            SNAPSHOTS,
            "json_valid(guard_reasons_json) AND json_type(guard_reasons_json) = 'array'"
            " AND (instr(guard_reasons_json, '\"PRICE_LOSS\"') > 0)"
            " = (purchase_cost_krw + supplier_shipping_krw + platform_fee_krw"
            " >= final_sale_price_krw)"
            " AND (instr(guard_reasons_json, '\"PRICE_BELOW_MIN_MARGIN\"') > 0)"
            " = (expected_net_profit_krw * minimum_margin_denominator"
            " < final_sale_price_krw * minimum_margin_numerator)"
            " AND json_array_length(guard_reasons_json)"
            " = (purchase_cost_krw + supplier_shipping_krw + platform_fee_krw"
            " >= final_sale_price_krw)"
            " + (expected_net_profit_krw * minimum_margin_denominator"
            " < final_sale_price_krw * minimum_margin_numerator)",
            "guard_reasons_exact",
        ),
        _fk(SNAPSHOTS, "atomic_sku_item_id", "atomic_sku_product_items", "atomic_sku_item_id"),
        _fk(SNAPSHOTS, "product_group_id", "product_groups", "product_group_id"),
        _fk(SNAPSHOTS, "atomic_sku_id", "atomic_skus", "atomic_sku_id"),
        _fk(
            SNAPSHOTS,
            "membership_revision_id",
            "group_membership_revisions",
            "membership_revision_id",
        ),
        _fk(SNAPSHOTS, "source_binding_id", BINDINGS, "binding_id"),
        _fk(
            SNAPSHOTS,
            "atomic_sku_revision_member_id",
            "atomic_sku_revision_members",
            "revision_member_id",
        ),
        _fk(
            SNAPSHOTS,
            "source_product_facts_revision_id",
            "product_facts_revisions",
            "revision_id",
        ),
        sa.PrimaryKeyConstraint("pricing_snapshot_id", name=op.f(f"pk_{SNAPSHOTS}")),
    )
    op.create_index(
        "ix_atomic_sku_pricing_snapshots_item_context",
        SNAPSHOTS,
        ["atomic_sku_item_id", "pricing_context_fingerprint"],
    )

    op.create_table(
        MOVES,
        sa.Column("move_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_item_id", sa.String(36), nullable=False),
        sa.Column("pricing_context_fingerprint", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("pricing_snapshot_id", sa.String(36), nullable=False),
        sa.Column("previous_pricing_snapshot_id", sa.String(36), nullable=True),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(MOVES, "sequence >= 1", "sequence_positive"),
        _check(MOVES, "reason IN ('INITIAL', 'REPRICED')", "reason_valid"),
        _check(
            MOVES,
            "(sequence = 1) = (previous_pricing_snapshot_id IS NULL)",
            "first_move_has_no_previous",
        ),
        _check(MOVES, "(reason = 'INITIAL') = (sequence = 1)", "initial_opens_history"),
        _check(
            MOVES,
            "previous_pricing_snapshot_id IS NULL"
            " OR previous_pricing_snapshot_id <> pricing_snapshot_id",
            "move_changes_snapshot",
        ),
        _check(MOVES, _hex("pricing_context_fingerprint"), "context_fingerprint_hex"),
        _check(MOVES, "rule_version <> ''", "rule_version_present"),
        _check(MOVES, "decided_by <> ''", "decided_by_present"),
        _check(MOVES, "correlation_id <> ''", "correlation_present"),
        _fk(MOVES, "atomic_sku_item_id", "atomic_sku_product_items", "atomic_sku_item_id"),
        _fk(MOVES, "pricing_snapshot_id", SNAPSHOTS, "pricing_snapshot_id"),
        _fk(MOVES, "previous_pricing_snapshot_id", SNAPSHOTS, "pricing_snapshot_id"),
        sa.PrimaryKeyConstraint("move_id", name=op.f(f"pk_{MOVES}")),
        sa.UniqueConstraint(
            "atomic_sku_item_id",
            "pricing_context_fingerprint",
            "sequence",
            name=op.f(f"uq_{MOVES}_atomic_sku_item_id_pricing_context_fingerprint_sequence"),
        ),
    )

    _install_binding_triggers()
    _install_snapshot_triggers()
    _install_move_triggers()


def _install_binding_triggers() -> None:
    op.execute(
        f"CREATE TRIGGER trg_{BINDINGS}_close_only BEFORE UPDATE ON {BINDINGS} BEGIN"
        " SELECT CASE WHEN NOT ("
        " NEW.valid_to IS NOT NULL AND OLD.valid_to IS NULL"
        " AND NEW.binding_id IS OLD.binding_id"
        " AND NEW.atomic_sku_item_id IS OLD.atomic_sku_item_id"
        " AND NEW.atomic_sku_revision_member_id IS OLD.atomic_sku_revision_member_id"
        " AND NEW.group_member_id IS OLD.group_member_id"
        " AND NEW.provenance_revision_id IS OLD.provenance_revision_id"
        " AND NEW.acquisition_basis IS OLD.acquisition_basis"
        " AND NEW.base_purchase_cost_krw IS OLD.base_purchase_cost_krw"
        " AND NEW.option_additional_price_krw IS OLD.option_additional_price_krw"
        " AND NEW.purchase_cost_krw IS OLD.purchase_cost_krw"
        " AND NEW.decided_by IS OLD.decided_by"
        " AND NEW.correlation_id IS OLD.correlation_id"
        " AND NEW.valid_from IS OLD.valid_from)"
        " THEN RAISE(ABORT, 'AtomicSKU source binding may only be closed') END; END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{BINDINGS}_no_delete BEFORE DELETE ON {BINDINGS}"
        f" BEGIN SELECT RAISE(ABORT, '{BINDINGS} is append-only'); END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{BINDINGS}_scope BEFORE INSERT ON {BINDINGS} BEGIN"
        " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM atomic_sku_product_items i"
        " JOIN atomic_skus s ON s.atomic_sku_id = i.atomic_sku_id"
        " JOIN atomic_sku_revision_members r ON r.atomic_sku_id = s.atomic_sku_id"
        " JOIN atomic_sku_set_revisions sr ON sr.sku_set_revision_id = r.sku_set_revision_id"
        " JOIN current_atomic_sku_set_moves cm"
        " ON cm.sku_set_revision_id = sr.sku_set_revision_id"
        " JOIN product_facts_revisions pr ON pr.revision_id = r.source_revision_id"
        " JOIN source_products sp ON sp.supplier_key = pr.supplier_key"
        " AND sp.source_product_id = pr.source_product_id"
        " JOIN group_members gm ON gm.source_product_uid = sp.source_product_uid"
        " JOIN product_groups pg ON pg.product_group_id = i.product_group_id"
        " JOIN current_source_revision_moves sm ON sm.source_product_uid = sp.source_product_uid"
        " WHERE i.atomic_sku_item_id = NEW.atomic_sku_item_id"
        " AND r.revision_member_id = NEW.atomic_sku_revision_member_id"
        " AND r.source_revision_id = NEW.provenance_revision_id"
        " AND gm.member_id = NEW.group_member_id AND gm.status = 'CONFIRMED'"
        " AND gm.product_group_id = i.product_group_id"
        " AND pg.status = 'ACTIVE'"
        " AND cm.product_group_id = i.product_group_id"
        " AND cm.sequence = (SELECT MAX(sequence) FROM current_atomic_sku_set_moves"
        " WHERE product_group_id = i.product_group_id)"
        " AND sm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = sp.source_product_uid)"
        " AND sm.revision_id = r.source_revision_id"
        " AND sr.common_option_revision_id = (SELECT revision_id"
        " FROM current_common_sales_option_revision_moves"
        " WHERE product_group_id = i.product_group_id ORDER BY sequence DESC LIMIT 1)"
        " AND sr.fact_mapping_revision_id = (SELECT mapping_revision_id"
        " FROM current_common_option_fact_mapping_moves"
        " WHERE product_group_id = i.product_group_id ORDER BY sequence DESC LIMIT 1)"
        " AND NOT EXISTS (SELECT 1 FROM atomic_sku_revision_members ar"
        " JOIN product_facts_revisions apr ON apr.revision_id = ar.source_revision_id"
        " JOIN source_products asp ON asp.supplier_key = apr.supplier_key"
        " AND asp.source_product_id = apr.source_product_id"
        " WHERE ar.sku_set_revision_id = sr.sku_set_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM group_members agm"
        " JOIN current_source_revision_moves asm"
        " ON asm.source_product_uid = agm.source_product_uid"
        " WHERE agm.product_group_id = i.product_group_id"
        " AND agm.source_product_uid = asp.source_product_uid"
        " AND agm.status = 'CONFIRMED'"
        " AND asm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = asp.source_product_uid)"
        " AND asm.revision_id = ar.source_revision_id))"
        " AND NOT EXISTS (SELECT 1 FROM common_option_fact_axis_mappings am"
        " JOIN product_facts_revisions apr ON apr.revision_id = am.source_revision_id"
        " JOIN source_products asp ON asp.supplier_key = apr.supplier_key"
        " AND asp.source_product_id = apr.source_product_id"
        " WHERE am.mapping_revision_id = sr.fact_mapping_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM group_members agm"
        " JOIN current_source_revision_moves asm"
        " ON asm.source_product_uid = agm.source_product_uid"
        " WHERE agm.product_group_id = i.product_group_id"
        " AND agm.source_product_uid = asp.source_product_uid"
        " AND agm.status = 'CONFIRMED'"
        " AND asm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = asp.source_product_uid)"
        " AND asm.revision_id = am.source_revision_id))"
        " AND NOT EXISTS (SELECT 1 FROM common_option_fact_value_mappings vm"
        " JOIN product_facts_revisions vpr ON vpr.revision_id = vm.source_revision_id"
        " JOIN source_products vsp ON vsp.supplier_key = vpr.supplier_key"
        " AND vsp.source_product_id = vpr.source_product_id"
        " WHERE vm.mapping_revision_id = sr.fact_mapping_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM group_members vgm"
        " JOIN current_source_revision_moves vsm"
        " ON vsm.source_product_uid = vgm.source_product_uid"
        " WHERE vgm.product_group_id = i.product_group_id"
        " AND vgm.source_product_uid = vsp.source_product_uid"
        " AND vgm.status = 'CONFIRMED'"
        " AND vsm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = vsp.source_product_uid)"
        " AND vsm.revision_id = vm.source_revision_id)))"
        " THEN RAISE(ABORT, 'AtomicSKU source binding proof is not current and exact') END; END"
    )


def _install_snapshot_triggers() -> None:
    _immutable(SNAPSHOTS)
    op.execute(
        f"CREATE TRIGGER trg_{SNAPSHOTS}_prices_current_state BEFORE INSERT ON {SNAPSHOTS} BEGIN"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {BINDINGS} b"
        " JOIN atomic_sku_product_items i ON i.atomic_sku_item_id = b.atomic_sku_item_id"
        " WHERE b.binding_id = NEW.source_binding_id AND b.valid_to IS NULL"
        " AND b.atomic_sku_item_id = NEW.atomic_sku_item_id"
        " AND i.product_group_id = NEW.product_group_id"
        " AND i.composition_signature = NEW.composition_signature"
        " AND i.atomic_sku_id = NEW.atomic_sku_id"
        " AND i.atomic_sku_selection_signature = NEW.atomic_sku_selection_signature"
        " AND b.atomic_sku_revision_member_id = NEW.atomic_sku_revision_member_id"
        " AND b.provenance_revision_id = NEW.source_product_facts_revision_id"
        " AND b.purchase_cost_krw = NEW.purchase_cost_krw)"
        " THEN RAISE(ABORT, 'AtomicSKU snapshot does not price its exact open binding') END;"
        " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM group_membership_revisions mr"
        " WHERE mr.membership_revision_id = NEW.membership_revision_id"
        " AND mr.product_group_id = NEW.product_group_id"
        " AND mr.revision_no = (SELECT MAX(revision_no) FROM group_membership_revisions"
        " WHERE product_group_id = NEW.product_group_id))"
        " THEN RAISE(ABORT, 'AtomicSKU snapshot membership is not current') END; END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{SNAPSHOTS}_binding_current_exact BEFORE INSERT ON {SNAPSHOTS} BEGIN"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {BINDINGS} b"
        " JOIN atomic_sku_product_items i ON i.atomic_sku_item_id = b.atomic_sku_item_id"
        " JOIN product_groups pg ON pg.product_group_id = i.product_group_id"
        " JOIN atomic_sku_revision_members r"
        " ON r.revision_member_id = b.atomic_sku_revision_member_id"
        " JOIN atomic_sku_set_revisions sr ON sr.sku_set_revision_id = r.sku_set_revision_id"
        " JOIN current_atomic_sku_set_moves cm"
        " ON cm.sku_set_revision_id = sr.sku_set_revision_id"
        " JOIN product_facts_revisions pr ON pr.revision_id = b.provenance_revision_id"
        " JOIN source_products sp ON sp.supplier_key = pr.supplier_key"
        " AND sp.source_product_id = pr.source_product_id"
        " JOIN group_members gm ON gm.member_id = b.group_member_id"
        " JOIN current_source_revision_moves sm ON sm.source_product_uid = sp.source_product_uid"
        " WHERE b.binding_id = NEW.source_binding_id AND b.valid_to IS NULL"
        " AND i.atomic_sku_item_id = NEW.atomic_sku_item_id"
        " AND pg.status = 'ACTIVE' AND r.atomic_sku_id = i.atomic_sku_id"
        " AND r.source_revision_id = b.provenance_revision_id"
        " AND gm.product_group_id = i.product_group_id AND gm.status = 'CONFIRMED'"
        " AND gm.source_product_uid = sp.source_product_uid"
        " AND cm.product_group_id = i.product_group_id"
        " AND cm.sequence = (SELECT MAX(sequence) FROM current_atomic_sku_set_moves"
        " WHERE product_group_id = i.product_group_id)"
        " AND sm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = sp.source_product_uid)"
        " AND sm.revision_id = b.provenance_revision_id"
        " AND sr.common_option_revision_id = (SELECT revision_id"
        " FROM current_common_sales_option_revision_moves"
        " WHERE product_group_id = i.product_group_id ORDER BY sequence DESC LIMIT 1)"
        " AND sr.fact_mapping_revision_id = (SELECT mapping_revision_id"
        " FROM current_common_option_fact_mapping_moves"
        " WHERE product_group_id = i.product_group_id ORDER BY sequence DESC LIMIT 1)"
        " AND NOT EXISTS (SELECT 1 FROM atomic_sku_revision_members ar"
        " JOIN product_facts_revisions apr ON apr.revision_id = ar.source_revision_id"
        " JOIN source_products asp ON asp.supplier_key = apr.supplier_key"
        " AND asp.source_product_id = apr.source_product_id"
        " WHERE ar.sku_set_revision_id = sr.sku_set_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM group_members agm"
        " JOIN current_source_revision_moves asm"
        " ON asm.source_product_uid = agm.source_product_uid"
        " WHERE agm.product_group_id = i.product_group_id"
        " AND agm.source_product_uid = asp.source_product_uid"
        " AND agm.status = 'CONFIRMED'"
        " AND asm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = asp.source_product_uid)"
        " AND asm.revision_id = ar.source_revision_id))"
        " AND NOT EXISTS (SELECT 1 FROM common_option_fact_axis_mappings am"
        " JOIN product_facts_revisions apr ON apr.revision_id = am.source_revision_id"
        " JOIN source_products asp ON asp.supplier_key = apr.supplier_key"
        " AND asp.source_product_id = apr.source_product_id"
        " WHERE am.mapping_revision_id = sr.fact_mapping_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM group_members agm"
        " JOIN current_source_revision_moves asm"
        " ON asm.source_product_uid = agm.source_product_uid"
        " WHERE agm.product_group_id = i.product_group_id"
        " AND agm.source_product_uid = asp.source_product_uid"
        " AND agm.status = 'CONFIRMED'"
        " AND asm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = asp.source_product_uid)"
        " AND asm.revision_id = am.source_revision_id))"
        " AND NOT EXISTS (SELECT 1 FROM common_option_fact_value_mappings vm"
        " JOIN product_facts_revisions vpr ON vpr.revision_id = vm.source_revision_id"
        " JOIN source_products vsp ON vsp.supplier_key = vpr.supplier_key"
        " AND vsp.source_product_id = vpr.source_product_id"
        " WHERE vm.mapping_revision_id = sr.fact_mapping_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM group_members vgm"
        " JOIN current_source_revision_moves vsm"
        " ON vsm.source_product_uid = vgm.source_product_uid"
        " WHERE vgm.product_group_id = i.product_group_id"
        " AND vgm.source_product_uid = vsp.source_product_uid"
        " AND vgm.status = 'CONFIRMED'"
        " AND vsm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = vsp.source_product_uid)"
        " AND vsm.revision_id = vm.source_revision_id)))"
        " THEN RAISE(ABORT, 'AtomicSKU snapshot binding proof is not current and exact') END; END"
    )


def _install_move_triggers() -> None:
    _immutable(MOVES)
    op.execute(
        f"CREATE TRIGGER trg_{MOVES}_chain BEFORE INSERT ON {MOVES} BEGIN"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {SNAPSHOTS} s"
        " WHERE s.pricing_snapshot_id = NEW.pricing_snapshot_id"
        " AND s.atomic_sku_item_id = NEW.atomic_sku_item_id"
        " AND s.pricing_context_fingerprint = NEW.pricing_context_fingerprint)"
        " THEN RAISE(ABORT, 'AtomicSKU pricing move snapshot scope mismatch') END;"
        f" SELECT CASE WHEN NEW.sequence <> COALESCE((SELECT MAX(sequence) + 1 FROM {MOVES}"
        " WHERE atomic_sku_item_id = NEW.atomic_sku_item_id"
        " AND pricing_context_fingerprint = NEW.pricing_context_fingerprint), 1)"
        " THEN RAISE(ABORT, 'AtomicSKU pricing move sequence is not next') END;"
        f" SELECT CASE WHEN NEW.previous_pricing_snapshot_id IS NOT (SELECT pricing_snapshot_id"
        f" FROM {MOVES} WHERE atomic_sku_item_id = NEW.atomic_sku_item_id"
        " AND pricing_context_fingerprint = NEW.pricing_context_fingerprint"
        " ORDER BY sequence DESC LIMIT 1)"
        " THEN RAISE(ABORT, 'AtomicSKU pricing move previous snapshot is not current') END; END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    held = {
        table: int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
        for table in (BINDINGS, SNAPSHOTS, MOVES)
    }
    if any(held.values()):
        raise RuntimeError(
            f"cannot downgrade 0047 while AtomicSKU economics history exists: {held}"
        )
    for table in (MOVES, SNAPSHOTS, BINDINGS):
        op.drop_table(table)
