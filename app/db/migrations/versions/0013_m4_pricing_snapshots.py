"""M4 PR-D: immutable pricing snapshots and their current-snapshot pointer history.

Revision ID: 0013_m4_pricing_snapshots
Revises: 0012_m4_product_foundation
Create Date: 2026-09-19

Issue #80 PR-D (kickoff 5737440897), under ADR-0013 §7. It is additive: no M3 or earlier M4 table,
row or trigger is touched, and **nothing is backfilled**. A price is a calculation under an
explicit pricing context, and no migration can know a context, so none is guessed.

**What the CHECK literals make structural** (frozen with this revision; the integration tests
compare them with the ORM models):
- the canonical rule: ``final_sale_price = COALESCE(minimum_sale_price, target_margin_price)``,
  with ``price_basis`` agreeing, so no maximum of the two can ever be stored;
- the platform fee and the other policy costs are the context's exact ceilings of their rates, and
  the expected net profit is exactly the price minus every cost;
- the guard and every guard reason follow from exact integer comparisons: a loss is
  ``purchase + shipping + fee >= final``, and below the minimum margin is
  ``profit × denominator < final × numerator``;
- policy v1 is 35% target and 10% minimum margin, and the only rounding is ``CEIL_KRW_1``.

**What the triggers enforce across rows.** A snapshot prices exactly the current state it names:
- the Item key it freezes is the Item's own;
- its binding is the Item's open ``BASE_PRODUCT`` binding;
- the binding's member is CONFIRMED in the Item's group;
- its source revision is that binding's provenance and that member's current source revision;
- its membership revision is the group's newest, and the group is ACTIVE.

A pointer move names a snapshot of its own Item and exact context fingerprint, extends its history
in order, starts from the current snapshot, and never returns to a snapshot already in the chain.

Both tables reject every UPDATE and every DELETE.

**Downgrade fails closed.** While any pricing snapshot or move exists it refuses: pricing history is
never silently destroyed.
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_m4_pricing_snapshots"
down_revision: str | None = "0012_m4_product_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SNAPSHOTS = "pricing_snapshots"
MOVES = "current_pricing_snapshot_moves"
TABLES = (SNAPSHOTS, MOVES)  # creation order; dropped in reverse
ITEMS = "product_items"
GROUPS = "product_groups"
MEMBERS = "group_members"
MEMBERSHIP = "group_membership_revisions"
BINDINGS = "source_bindings"
SOURCE_MOVES = "current_source_revision_moves"
REVISIONS = "product_facts_revisions"

# Vocabularies frozen with this revision (app.products.pricing).
_RULE_VERSION = "pricing-rule/v1"
_ROUNDINGS = ("CEIL_KRW_1",)
_BASES = ("MINIMUM_SALE_PRICE", "TARGET_MARGIN")
_GUARDS = ("OK", "LOSS", "BELOW_MIN_MARGIN")
_MOVE_REASONS = ("INITIAL", "REPRICED")
_LOSS = "purchase_cost_krw + supplier_shipping_krw + platform_fee_krw >= final_sale_price_krw"
_BELOW = (
    "expected_net_profit_krw * minimum_margin_denominator"
    " < final_sale_price_krw * minimum_margin_numerator"
)


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


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


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _fk(table: str, column: str, target: str, target_column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.{target_column}"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def _trigger(name: str, event: str, table: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    op.create_table(
        SNAPSHOTS,
        sa.Column("pricing_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("composition_signature", sa.String(length=64), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("account_id", sa.String(length=64), nullable=True),
        sa.Column("fee_table_version", sa.String(length=64), nullable=False),
        sa.Column("pricing_policy_version", sa.String(length=64), nullable=False),
        sa.Column("pricing_context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("pricing_context_json", sa.Text(), nullable=False),
        sa.Column("membership_revision_id", sa.String(length=36), nullable=False),
        sa.Column("source_binding_id", sa.String(length=36), nullable=False),
        sa.Column("source_product_facts_revision_id", sa.String(length=36), nullable=False),
        sa.Column("dependency_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("pricing_rule_version", sa.String(length=40), nullable=False),
        sa.Column("purchase_cost_krw", sa.Integer(), nullable=False),
        sa.Column("supplier_shipping_krw", sa.Integer(), nullable=False),
        sa.Column("minimum_sale_price_krw", sa.Integer(), nullable=True),
        sa.Column("fee_rate_numerator", sa.Integer(), nullable=False),
        sa.Column("fee_rate_denominator", sa.Integer(), nullable=False),
        sa.Column("fee_fixed_krw", sa.Integer(), nullable=False),
        sa.Column("other_cost_rate_numerator", sa.Integer(), nullable=False),
        sa.Column("other_cost_rate_denominator", sa.Integer(), nullable=False),
        sa.Column("other_cost_fixed_krw", sa.Integer(), nullable=False),
        sa.Column("cost_rounding", sa.String(length=20), nullable=False),
        sa.Column("price_rounding", sa.String(length=20), nullable=False),
        sa.Column("target_margin_numerator", sa.Integer(), nullable=False),
        sa.Column("target_margin_denominator", sa.Integer(), nullable=False),
        sa.Column("minimum_margin_numerator", sa.Integer(), nullable=False),
        sa.Column("minimum_margin_denominator", sa.Integer(), nullable=False),
        sa.Column("platform_fee_krw", sa.Integer(), nullable=False),
        sa.Column("other_policy_cost_krw", sa.Integer(), nullable=False),
        sa.Column("target_margin_price_krw", sa.Integer(), nullable=False),
        sa.Column("final_sale_price_krw", sa.Integer(), nullable=False),
        sa.Column("price_basis", sa.String(length=20), nullable=False),
        sa.Column("expected_net_profit_krw", sa.Integer(), nullable=False),
        sa.Column("expected_net_margin_bp", sa.Integer(), nullable=False),
        sa.Column("price_guard", sa.String(length=20), nullable=False),
        sa.Column("guard_reasons_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(SNAPSHOTS, "account_id IS NULL OR account_id <> ''", "account_id_present"),
        _check(
            SNAPSHOTS,
            "final_sale_price_krw = COALESCE(minimum_sale_price_krw, target_margin_price_krw)"
            " AND (minimum_sale_price_krw IS NULL) = (price_basis = 'TARGET_MARGIN')"
            f" AND {_in('price_basis', _BASES)}",
            "canonical_rule",
        ),
        _check(SNAPSHOTS, _hex64("pricing_context_fingerprint"), "context_fingerprint_hex"),
        _check(
            SNAPSHOTS,
            "json_valid(pricing_context_json) AND json_type(pricing_context_json) = 'object'",
            "context_is_object",
        ),
        _check(SNAPSHOTS, _hex64("dependency_fingerprint"), "dependency_fingerprint_hex"),
        _check(SNAPSHOTS, _rate("fee"), "fee_rate_valid"),
        _check(SNAPSHOTS, "fee_table_version <> ''", "fee_table_version_present"),
        _check(
            SNAPSHOTS,
            f"{_in('price_guard', _GUARDS)} AND (price_guard = 'LOSS') = ({_LOSS})"
            f" AND (price_guard = 'BELOW_MIN_MARGIN') = (NOT ({_LOSS}) AND {_BELOW})",
            "guard_exact",
        ),
        _check(
            SNAPSHOTS,
            "json_valid(guard_reasons_json) AND json_type(guard_reasons_json) = 'array'"
            f" AND (instr(guard_reasons_json, '\"PRICE_LOSS\"') > 0) = ({_LOSS})"
            " AND (instr(guard_reasons_json, '\"PRICE_BELOW_MIN_MARGIN\"') > 0)"
            f" = ({_BELOW})"
            f" AND json_array_length(guard_reasons_json) = ({_LOSS}) + ({_BELOW})",
            "guard_reasons_exact",
        ),
        _check(
            SNAPSHOTS,
            "expected_net_margin_bp * final_sale_price_krw <= expected_net_profit_krw * 10000"
            " AND expected_net_profit_krw * 10000"
            " < (expected_net_margin_bp + 1) * final_sale_price_krw",
            "margin_bp_floor",
        ),
        _check(SNAPSHOTS, "marketplace_key <> ''", "marketplace_key_present"),
        _check(SNAPSHOTS, _rate("other_cost"), "other_cost_rate_valid"),
        _check(
            SNAPSHOTS,
            _ceiling_cost("other_policy_cost_krw", "other_cost"),
            "other_policy_cost_exact",
        ),
        _check(SNAPSHOTS, _ceiling_cost("platform_fee_krw", "fee"), "platform_fee_exact"),
        _check(
            SNAPSHOTS,
            "target_margin_denominator >= 1 AND minimum_margin_denominator >= 1"
            f" AND (pricing_rule_version <> '{_RULE_VERSION}'"
            " OR (target_margin_numerator * 100 = 35 * target_margin_denominator"
            " AND minimum_margin_numerator * 100 = 10 * minimum_margin_denominator))",
            "policy_margins",
        ),
        _check(
            SNAPSHOTS,
            "target_margin_price_krw >= 1 AND final_sale_price_krw >= 1",
            "prices_positive",
        ),
        _check(SNAPSHOTS, "pricing_policy_version <> ''", "pricing_policy_version_present"),
        _check(
            SNAPSHOTS,
            "expected_net_profit_krw = final_sale_price_krw - purchase_cost_krw"
            " - supplier_shipping_krw - platform_fee_krw - other_policy_cost_krw",
            "profit_exact",
        ),
        _check(
            SNAPSHOTS,
            f"{_in('cost_rounding', _ROUNDINGS)} AND {_in('price_rounding', _ROUNDINGS)}",
            "rounding_declared",
        ),
        _check(SNAPSHOTS, _in("pricing_rule_version", (_RULE_VERSION,)), "rule_known"),
        _check(SNAPSHOTS, _hex64("composition_signature"), "signature_hex"),
        _check(
            SNAPSHOTS,
            "purchase_cost_krw >= 0 AND supplier_shipping_krw >= 0"
            " AND (minimum_sale_price_krw IS NULL OR minimum_sale_price_krw > 0)",
            "source_amounts_valid",
        ),
        _fk(SNAPSHOTS, "item_id", ITEMS, "item_id"),
        _fk(SNAPSHOTS, "product_group_id", GROUPS, "product_group_id"),
        _fk(SNAPSHOTS, "membership_revision_id", MEMBERSHIP, "membership_revision_id"),
        _fk(SNAPSHOTS, "source_binding_id", BINDINGS, "binding_id"),
        _fk(SNAPSHOTS, "source_product_facts_revision_id", REVISIONS, "revision_id"),
        sa.PrimaryKeyConstraint("pricing_snapshot_id", name=op.f(f"pk_{SNAPSHOTS}")),
    )
    op.create_index(
        "ix_pricing_snapshots_item_context",
        SNAPSHOTS,
        ["item_id", "pricing_context_fingerprint"],
    )
    op.create_table(
        MOVES,
        sa.Column("move_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("pricing_context_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("pricing_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("previous_pricing_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("rule_version", sa.String(length=40), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(MOVES, _hex64("pricing_context_fingerprint"), "context_fingerprint_hex"),
        _check(MOVES, "correlation_id <> ''", "correlation_present"),
        _check(MOVES, "decided_by <> ''", "decided_by_present"),
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
        _check(MOVES, _in("reason", _MOVE_REASONS), "reason_valid"),
        _check(MOVES, "rule_version <> ''", "rule_version_present"),
        _check(MOVES, "sequence >= 1", "sequence_positive"),
        _fk(MOVES, "item_id", ITEMS, "item_id"),
        _fk(MOVES, "pricing_snapshot_id", SNAPSHOTS, "pricing_snapshot_id"),
        _fk(MOVES, "previous_pricing_snapshot_id", SNAPSHOTS, "pricing_snapshot_id"),
        sa.PrimaryKeyConstraint("move_id", name=op.f(f"pk_{MOVES}")),
        sa.UniqueConstraint(
            "item_id",
            "pricing_context_fingerprint",
            "sequence",
            name=op.f(f"uq_{MOVES}_item_id_pricing_context_fingerprint_sequence"),
        ),
    )
    _install_triggers()


def _install_triggers() -> None:
    for table in TABLES:
        _trigger(f"trg_{table}_no_update", "UPDATE", table, _raise(f"{table} is append-only", "1"))
        _trigger(f"trg_{table}_no_delete", "DELETE", table, _raise(f"{table} is append-only", "1"))
    binding = f"(SELECT b.{{column}} FROM {BINDINGS} b WHERE b.binding_id = NEW.source_binding_id)"
    member_uid = (
        f"(SELECT m.source_product_uid FROM {BINDINGS} b JOIN {MEMBERS} m"
        " ON m.member_id = b.group_member_id WHERE b.binding_id = NEW.source_binding_id)"
    )
    _trigger(
        f"trg_{SNAPSHOTS}_prices_current_state",
        "INSERT",
        SNAPSHOTS,
        _raise(
            f"{SNAPSHOTS}: the frozen Item key is that of the Item",
            f"NOT EXISTS (SELECT 1 FROM {ITEMS} i WHERE i.item_id = NEW.item_id"
            " AND i.product_group_id = NEW.product_group_id"
            " AND i.composition_signature = NEW.composition_signature)",
        )
        + _raise(
            f"{SNAPSHOTS}: the group is ACTIVE",
            f"(SELECT status FROM {GROUPS} WHERE product_group_id = NEW.product_group_id)"
            " IS NOT 'ACTIVE'",
        )
        + _raise(
            f"{SNAPSHOTS}: the binding is the open BASE_PRODUCT binding of the Item",
            f"NOT EXISTS (SELECT 1 FROM {BINDINGS} b WHERE b.binding_id = NEW.source_binding_id"
            " AND b.item_id = NEW.item_id AND b.binding_kind = 'BASE_PRODUCT'"
            " AND b.valid_to IS NULL)",
        )
        + _raise(
            f"{SNAPSHOTS}: the binding member is CONFIRMED in the group of the Item",
            f"NOT EXISTS (SELECT 1 FROM {BINDINGS} b JOIN {MEMBERS} m"
            " ON m.member_id = b.group_member_id WHERE b.binding_id = NEW.source_binding_id"
            " AND m.product_group_id = NEW.product_group_id AND m.status = 'CONFIRMED')",
        )
        + _raise(
            f"{SNAPSHOTS}: the source revision is the provenance of the binding",
            f"NEW.source_product_facts_revision_id IS NOT"
            f" {binding.format(column='provenance_revision_id')}",
        )
        + _raise(
            f"{SNAPSHOTS}: the source revision is the current source revision of the member",
            f"NEW.source_product_facts_revision_id IS NOT (SELECT revision_id FROM {SOURCE_MOVES}"
            f" WHERE source_product_uid = {member_uid} ORDER BY sequence DESC LIMIT 1)",
        )
        + _raise(
            f"{SNAPSHOTS}: the membership revision is the current one of the group",
            f"NEW.membership_revision_id IS NOT (SELECT membership_revision_id FROM {MEMBERSHIP}"
            " WHERE product_group_id = NEW.product_group_id ORDER BY revision_no DESC LIMIT 1)",
        ),
    )
    chain = (
        f"FROM {MOVES} WHERE item_id = NEW.item_id"
        " AND pricing_context_fingerprint = NEW.pricing_context_fingerprint"
    )
    _trigger(
        f"trg_{MOVES}_chain",
        "INSERT",
        MOVES,
        _raise(
            f"{MOVES}: the snapshot prices this Item under this exact context",
            f"NOT EXISTS (SELECT 1 FROM {SNAPSHOTS} s"
            " WHERE s.pricing_snapshot_id = NEW.pricing_snapshot_id AND s.item_id = NEW.item_id"
            " AND s.pricing_context_fingerprint = NEW.pricing_context_fingerprint)",
        )
        + _raise(
            f"{MOVES}: moves are appended in order",
            f"NEW.sequence <> (SELECT COALESCE(MAX(sequence), 0) + 1 {chain})",
        )
        + _raise(
            f"{MOVES}: a move starts from the current snapshot",
            "NEW.previous_pricing_snapshot_id IS NOT"
            f" (SELECT pricing_snapshot_id {chain} ORDER BY sequence DESC LIMIT 1)",
        )
        + _raise(
            f"{MOVES}: a move never returns to an earlier snapshot",
            f"EXISTS (SELECT 1 {chain} AND pricing_snapshot_id = NEW.pricing_snapshot_id)",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if held:
            raise RuntimeError(
                f"cannot drop {table}: {held} row(s) of pricing history are held; pricing history"
                " is never silently destroyed"
            )
    for table in reversed(TABLES):
        op.drop_table(table)
