"""Create the reviewed Product Fact to Common Sales Option mapping owner.

Revision ID: 0043_common_option_fact_mapping
Revises: 0042_common_sales_option_owner
Create Date: 2026-10-03

Mappings cite exact leaves in immutable, current Product Facts revisions.  They are complete for
one current Common Sales Option revision and become current only after every declared target is
present.  No value is inferred or fabricated here.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from app.stages.products.common_option_mapping import FACT_MAPPING_SIGNATURE_VERSION

revision: str = "0043_common_option_fact_mapping"
down_revision: str | None = "0042_common_sales_option_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVISIONS = "common_option_fact_mapping_revisions"
AXES = "common_option_fact_axis_mappings"
VALUES = "common_option_fact_value_mappings"
MOVES = "current_common_option_fact_mapping_moves"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _immutable(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_{event.lower()} BEFORE {event} ON {table}"
            f" BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


def _fact_columns() -> list[Any]:
    return [
        sa.Column("source_revision_id", sa.String(length=36), nullable=False),
        sa.Column("source_field_key", sa.String(length=40), nullable=False),
        sa.Column("source_json_path", sa.String(length=300), nullable=False),
        sa.Column("source_value_json", sa.Text(), nullable=False),
        sa.Column("source_field_fingerprint", sa.String(length=64), nullable=False),
    ]


def _fact_constraints(table: str) -> list[Any]:
    return [
        _check(table, "source_field_key <> ''", "source_field_key_present"),
        _check(table, "source_json_path LIKE '$%'", "source_json_path_present"),
        _check(table, "json_valid(source_value_json)", "source_value_is_json"),
        _check(
            table,
            "length(source_field_fingerprint) = 64"
            " AND source_field_fingerprint NOT GLOB '*[^0-9a-f]*'",
            "field_fingerprint_hex",
        ),
        sa.ForeignKeyConstraint(
            ["source_revision_id", "source_field_key"],
            ["product_facts_fields.revision_id", "product_facts_fields.field_key"],
            name=op.f(f"fk_{table}_source_fact_product_facts_fields"),
        ),
    ]


def upgrade() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("mapping_revision_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("common_option_revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("mapping_signature", sa.String(length=64), nullable=False),
        sa.Column("signature_version", sa.String(length=64), nullable=False),
        sa.Column("axis_mapping_count", sa.Integer(), nullable=False),
        sa.Column("value_mapping_count", sa.Integer(), nullable=False),
        sa.Column("evidence_reference", sa.String(length=300), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("reviewed_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "revision_no >= 1", "revision_no_positive"),
        _check(REVISIONS, "axis_mapping_count >= 1", "axis_mapping_count_positive"),
        _check(
            REVISIONS,
            "value_mapping_count >= axis_mapping_count",
            "every_axis_maps_a_value",
        ),
        _check(
            REVISIONS,
            "length(mapping_signature) = 64 AND mapping_signature NOT GLOB '*[^0-9a-f]*'",
            "mapping_signature_hex",
        ),
        _check(
            REVISIONS,
            f"signature_version = '{FACT_MAPPING_SIGNATURE_VERSION}'",
            "signature_version",
        ),
        _check(REVISIONS, "evidence_reference <> ''", "evidence_reference_present"),
        _check(REVISIONS, "reason <> ''", "reason_present"),
        _check(REVISIONS, "reviewed_by <> ''", "reviewed_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["product_group_id"],
            ["product_groups.product_group_id"],
            name=op.f(f"fk_{REVISIONS}_product_group_id_product_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["common_option_revision_id"],
            ["common_sales_option_revisions.revision_id"],
            name=op.f(f"fk_{REVISIONS}_common_option_revision_id_common_sales_option_revisions"),
        ),
        sa.PrimaryKeyConstraint("mapping_revision_id", name=op.f(f"pk_{REVISIONS}")),
        sa.UniqueConstraint(
            "product_group_id",
            "revision_no",
            name=op.f(f"uq_{REVISIONS}_product_group_id_revision_no"),
        ),
    )
    op.create_table(
        AXES,
        sa.Column("axis_mapping_id", sa.String(length=36), nullable=False),
        sa.Column("mapping_revision_id", sa.String(length=36), nullable=False),
        sa.Column("common_option_axis_id", sa.String(length=36), nullable=False),
        *_fact_columns(),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        *_fact_constraints(AXES),
        _check(AXES, "ordinal >= 0", "ordinal_non_negative"),
        sa.ForeignKeyConstraint(
            ["mapping_revision_id"],
            [f"{REVISIONS}.mapping_revision_id"],
            name=op.f(f"fk_{AXES}_mapping_revision_id_{REVISIONS}"),
        ),
        sa.ForeignKeyConstraint(
            ["common_option_axis_id"],
            ["common_sales_option_axes.axis_id"],
            name=op.f(f"fk_{AXES}_common_option_axis_id_common_sales_option_axes"),
        ),
        sa.PrimaryKeyConstraint("axis_mapping_id", name=op.f(f"pk_{AXES}")),
        sa.UniqueConstraint(
            "mapping_revision_id",
            "common_option_axis_id",
            name=op.f(f"uq_{AXES}_mapping_revision_id_common_option_axis_id"),
        ),
        sa.UniqueConstraint(
            "mapping_revision_id", "ordinal", name=op.f(f"uq_{AXES}_mapping_revision_id_ordinal")
        ),
        sa.UniqueConstraint(
            "mapping_revision_id",
            "source_revision_id",
            "source_field_key",
            "source_json_path",
            name=op.f(
                f"uq_{AXES}_mapping_revision_id_source_revision_id_source_field_key_source_json_path"
            ),
        ),
    )
    op.create_table(
        VALUES,
        sa.Column("value_mapping_id", sa.String(length=36), nullable=False),
        sa.Column("mapping_revision_id", sa.String(length=36), nullable=False),
        sa.Column("axis_mapping_id", sa.String(length=36), nullable=False),
        sa.Column("common_option_value_id", sa.String(length=36), nullable=False),
        *_fact_columns(),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        *_fact_constraints(VALUES),
        _check(VALUES, "ordinal >= 0", "ordinal_non_negative"),
        sa.ForeignKeyConstraint(
            ["mapping_revision_id"],
            [f"{REVISIONS}.mapping_revision_id"],
            name=op.f(f"fk_{VALUES}_mapping_revision_id_{REVISIONS}"),
        ),
        sa.ForeignKeyConstraint(
            ["axis_mapping_id"],
            [f"{AXES}.axis_mapping_id"],
            name=op.f(f"fk_{VALUES}_axis_mapping_id_{AXES}"),
        ),
        sa.ForeignKeyConstraint(
            ["common_option_value_id"],
            ["common_sales_option_values.value_id"],
            name=op.f(f"fk_{VALUES}_common_option_value_id_common_sales_option_values"),
        ),
        sa.PrimaryKeyConstraint("value_mapping_id", name=op.f(f"pk_{VALUES}")),
        sa.UniqueConstraint(
            "mapping_revision_id",
            "common_option_value_id",
            name=op.f(f"uq_{VALUES}_mapping_revision_id_common_option_value_id"),
        ),
        sa.UniqueConstraint(
            "axis_mapping_id", "ordinal", name=op.f(f"uq_{VALUES}_axis_mapping_id_ordinal")
        ),
        sa.UniqueConstraint(
            "mapping_revision_id",
            "source_revision_id",
            "source_field_key",
            "source_json_path",
            name=op.f(
                f"uq_{VALUES}_mapping_revision_id_source_revision_id_source_field_key_source_json_path"
            ),
        ),
    )
    op.create_table(
        MOVES,
        sa.Column("move_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("mapping_revision_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_mapping_revision_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("reviewed_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(MOVES, "sequence >= 1", "sequence_positive"),
        _check(
            MOVES,
            "(sequence = 1) = (previous_mapping_revision_id IS NULL)",
            "first_move_has_no_previous",
        ),
        _check(MOVES, "reason <> ''", "reason_present"),
        _check(MOVES, "reviewed_by <> ''", "reviewed_by_present"),
        _check(MOVES, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["product_group_id"],
            ["product_groups.product_group_id"],
            name=op.f(f"fk_{MOVES}_product_group_id_product_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["mapping_revision_id"],
            [f"{REVISIONS}.mapping_revision_id"],
            name=op.f(f"fk_{MOVES}_mapping_revision_id_{REVISIONS}"),
        ),
        sa.ForeignKeyConstraint(
            ["previous_mapping_revision_id"],
            [f"{REVISIONS}.mapping_revision_id"],
            name=op.f(f"fk_{MOVES}_previous_mapping_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("move_id", name=op.f(f"pk_{MOVES}")),
        sa.UniqueConstraint(
            "product_group_id", "sequence", name=op.f(f"uq_{MOVES}_product_group_id_sequence")
        ),
        sa.UniqueConstraint("mapping_revision_id", name=op.f(f"uq_{MOVES}_mapping_revision_id")),
    )

    for table in (REVISIONS, AXES, VALUES, MOVES):
        _immutable(table)

    for table in (AXES, VALUES):
        op.execute(
            f"CREATE TRIGGER trg_{table}_fact_guard BEFORE INSERT ON {table} BEGIN"
            " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM product_facts_fields f"
            " WHERE f.revision_id = NEW.source_revision_id"
            " AND f.field_key = NEW.source_field_key AND f.status = 'CONFIRMED'"
            " AND f.field_fingerprint = NEW.source_field_fingerprint"
            " AND json_type(f.value_json, NEW.source_json_path) IN ('text', 'integer')"
            " AND (json_type(f.value_json, NEW.source_json_path) <> 'text'"
            " OR json_extract(f.value_json, NEW.source_json_path) <> '')"
            " AND json_quote(json_extract(f.value_json, NEW.source_json_path))"
            " = NEW.source_value_json)"
            " THEN RAISE(ABORT, 'mapped Product Fact leaf is not exact and confirmed') END;"
            " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM product_facts_revisions r"
            " JOIN source_products s ON s.supplier_key = r.supplier_key"
            " AND s.source_product_id = r.source_product_id"
            " JOIN group_members g ON g.source_product_uid = s.source_product_uid"
            f" JOIN {REVISIONS} mr ON mr.mapping_revision_id = NEW.mapping_revision_id"
            " WHERE r.revision_id = NEW.source_revision_id"
            " AND g.product_group_id = mr.product_group_id AND g.status = 'CONFIRMED'"
            " AND NEW.source_revision_id = (SELECT c.revision_id"
            " FROM current_source_revision_moves c"
            " WHERE c.source_product_uid = s.source_product_uid"
            " ORDER BY c.sequence DESC LIMIT 1))"
            " THEN RAISE(ABORT, 'mapped Product Fact is not current product evidence') END; END"
        )

    op.execute(
        f"CREATE TRIGGER trg_{AXES}_target_guard BEFORE INSERT ON {AXES} BEGIN"
        f" SELECT CASE WHEN EXISTS (SELECT 1 FROM {MOVES}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id)"
        " THEN RAISE(ABORT, 'current option fact mapping is immutable') END;"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
        " JOIN common_sales_option_axes a"
        " ON a.revision_id = r.common_option_revision_id"
        " WHERE r.mapping_revision_id = NEW.mapping_revision_id"
        " AND a.axis_id = NEW.common_option_axis_id)"
        " THEN RAISE(ABORT, 'mapped axis belongs to another option revision') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {AXES}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id) >="
        f" (SELECT axis_mapping_count FROM {REVISIONS}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id)"
        " THEN RAISE(ABORT, 'axis mapping count exceeds declaration') END; END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{VALUES}_target_guard BEFORE INSERT ON {VALUES} BEGIN"
        f" SELECT CASE WHEN EXISTS (SELECT 1 FROM {MOVES}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id)"
        " THEN RAISE(ABORT, 'current option fact mapping is immutable') END;"
        f" SELECT CASE WHEN EXISTS (SELECT 1 FROM {AXES} am"
        " WHERE am.mapping_revision_id = NEW.mapping_revision_id"
        " AND am.source_revision_id = NEW.source_revision_id"
        " AND am.source_field_key = NEW.source_field_key"
        " AND am.source_json_path = NEW.source_json_path)"
        " THEN RAISE(ABORT, 'one Product Fact leaf cannot prove two option targets') END;"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {AXES} am"
        " JOIN common_sales_option_values v"
        " ON v.axis_id = am.common_option_axis_id"
        " WHERE am.axis_mapping_id = NEW.axis_mapping_id"
        " AND am.mapping_revision_id = NEW.mapping_revision_id"
        " AND v.value_id = NEW.common_option_value_id)"
        " THEN RAISE(ABORT, 'mapped value belongs to another axis') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {VALUES}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id) >="
        f" (SELECT value_mapping_count FROM {REVISIONS}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id)"
        " THEN RAISE(ABORT, 'value mapping count exceeds declaration') END; END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{MOVES}_complete_mapping BEFORE INSERT ON {MOVES} BEGIN"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
        " WHERE r.mapping_revision_id = NEW.mapping_revision_id"
        " AND r.product_group_id = NEW.product_group_id)"
        " THEN RAISE(ABORT, 'fact mapping belongs to another product') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {AXES}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id) <>"
        f" (SELECT axis_mapping_count FROM {REVISIONS}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id)"
        " THEN RAISE(ABORT, 'fact mapping has incomplete axes') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {VALUES}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id) <>"
        f" (SELECT value_mapping_count FROM {REVISIONS}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id)"
        " THEN RAISE(ABORT, 'fact mapping has incomplete values') END;"
        f" SELECT CASE WHEN (SELECT axis_mapping_count FROM {REVISIONS}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id) <>"
        f" (SELECT cor.axis_count FROM {REVISIONS} mr"
        " JOIN common_sales_option_revisions cor"
        " ON cor.revision_id = mr.common_option_revision_id"
        " WHERE mr.mapping_revision_id = NEW.mapping_revision_id)"
        " THEN RAISE(ABORT, 'fact mapping does not cover every option axis') END;"
        f" SELECT CASE WHEN (SELECT value_mapping_count FROM {REVISIONS}"
        " WHERE mapping_revision_id = NEW.mapping_revision_id) <>"
        f" (SELECT cor.value_count FROM {REVISIONS} mr"
        " JOIN common_sales_option_revisions cor"
        " ON cor.revision_id = mr.common_option_revision_id"
        " WHERE mr.mapping_revision_id = NEW.mapping_revision_id)"
        " THEN RAISE(ABORT, 'fact mapping does not cover every option value') END;"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
        " JOIN current_common_sales_option_revision_moves cm"
        " ON cm.revision_id = r.common_option_revision_id"
        " WHERE r.mapping_revision_id = NEW.mapping_revision_id"
        " AND cm.product_group_id = NEW.product_group_id"
        " AND cm.sequence = (SELECT MAX(cm2.sequence)"
        " FROM current_common_sales_option_revision_moves cm2"
        " WHERE cm2.product_group_id = NEW.product_group_id))"
        " THEN RAISE(ABORT, 'fact mapping target is not the current option revision') END;"
        f" SELECT CASE WHEN NEW.sequence <> COALESCE((SELECT MAX(sequence) + 1 FROM {MOVES}"
        " WHERE product_group_id = NEW.product_group_id), 1)"
        " THEN RAISE(ABORT, 'fact mapping move sequence is not next') END;"
        f" SELECT CASE WHEN NEW.sequence > 1 AND NEW.previous_mapping_revision_id IS NOT"
        f" (SELECT mapping_revision_id FROM {MOVES}"
        " WHERE product_group_id = NEW.product_group_id ORDER BY sequence DESC LIMIT 1)"
        " THEN RAISE(ABORT, 'fact mapping move does not follow current mapping') END; END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    held = sum(
        int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
        for table in (REVISIONS, AXES, VALUES, MOVES)
    )
    if held:
        raise RuntimeError(
            f"cannot downgrade 0043: {held} reviewed Common Option fact mapping row(s) exist;"
            " canonical evidence is never silently destroyed"
        )
    op.drop_table(MOVES)
    op.drop_table(VALUES)
    op.drop_table(AXES)
    op.drop_table(REVISIONS)
