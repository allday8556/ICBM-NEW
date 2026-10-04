"""Create the immutable Common Sales Option authoring owner.

Revision ID: 0042_common_sales_option_owner
Revises: 0041_category_catalog
Create Date: 2026-10-03

This is the C1/C2 foundation of the ADR-0013 owner amendment. It stores provider-neutral authored
axes and values in immutable revisions and an append-only current-revision move history. It stores
no Product Fact mapping, Atomic SKU or marketplace projection.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.stages.products.common_options import COMMON_OPTION_SIGNATURE_VERSION

revision: str = "0042_common_sales_option_owner"
down_revision: str | None = "0041_category_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVISIONS = "common_sales_option_revisions"
AXES = "common_sales_option_axes"
VALUES = "common_sales_option_values"
MOVES = "current_common_sales_option_revision_moves"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _immutable(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_{event.lower()} BEFORE {event} ON {table}"
            f" BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


def upgrade() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("structure_signature", sa.String(length=64), nullable=False),
        sa.Column("signature_version", sa.String(length=64), nullable=False),
        sa.Column("axis_count", sa.Integer(), nullable=False),
        sa.Column("value_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "revision_no >= 1", "revision_no_positive"),
        _check(REVISIONS, "axis_count >= 1", "axis_count_positive"),
        _check(REVISIONS, "value_count >= axis_count", "every_axis_has_value"),
        _check(
            REVISIONS,
            "length(structure_signature) = 64 AND structure_signature NOT GLOB '*[^0-9a-f]*'",
            "signature_hex",
        ),
        _check(
            REVISIONS,
            f"signature_version = '{COMMON_OPTION_SIGNATURE_VERSION}'",
            "signature_version",
        ),
        _check(REVISIONS, "reason <> ''", "reason_present"),
        _check(REVISIONS, "decided_by <> ''", "decided_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["product_group_id"],
            ["product_groups.product_group_id"],
            name=op.f(f"fk_{REVISIONS}_product_group_id_product_groups"),
        ),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{REVISIONS}")),
        sa.UniqueConstraint(
            "product_group_id",
            "revision_no",
            name=op.f(f"uq_{REVISIONS}_product_group_id_revision_no"),
        ),
    )
    op.create_table(
        AXES,
        sa.Column("axis_id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("semantic_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        _check(
            AXES,
            "semantic_key GLOB '[a-z]*' AND semantic_key NOT GLOB '*[^a-z0-9_]*'",
            "semantic_key_canonical",
        ),
        _check(AXES, "length(semantic_key) BETWEEN 1 AND 64", "semantic_key_bounded"),
        _check(AXES, "length(display_name) BETWEEN 1 AND 100", "display_name_bounded"),
        _check(AXES, "ordinal >= 0", "ordinal_non_negative"),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            [f"{REVISIONS}.revision_id"],
            name=op.f(f"fk_{AXES}_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("axis_id", name=op.f(f"pk_{AXES}")),
        sa.UniqueConstraint(
            "revision_id", "semantic_key", name=op.f(f"uq_{AXES}_revision_id_semantic_key")
        ),
        sa.UniqueConstraint("revision_id", "ordinal", name=op.f(f"uq_{AXES}_revision_id_ordinal")),
    )
    op.create_table(
        VALUES,
        sa.Column("value_id", sa.String(length=36), nullable=False),
        sa.Column("axis_id", sa.String(length=36), nullable=False),
        sa.Column("canonical_value", sa.String(length=200), nullable=False),
        sa.Column("unit_code", sa.String(length=32), nullable=False),
        sa.Column("display_value", sa.String(length=200), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        _check(
            VALUES,
            "length(canonical_value) BETWEEN 1 AND 200",
            "canonical_value_bounded",
        ),
        _check(VALUES, "length(display_value) BETWEEN 1 AND 200", "display_value_bounded"),
        _check(
            VALUES,
            "unit_code = '' OR (length(unit_code) BETWEEN 1 AND 32"
            " AND unit_code GLOB '[a-z]*'"
            " AND unit_code NOT GLOB '*[^a-z0-9_]*')",
            "unit_code_canonical",
        ),
        _check(VALUES, "ordinal >= 0", "ordinal_non_negative"),
        sa.ForeignKeyConstraint(
            ["axis_id"], [f"{AXES}.axis_id"], name=op.f(f"fk_{VALUES}_axis_id_{AXES}")
        ),
        sa.PrimaryKeyConstraint("value_id", name=op.f(f"pk_{VALUES}")),
        sa.UniqueConstraint("axis_id", "ordinal", name=op.f(f"uq_{VALUES}_axis_id_ordinal")),
        sa.UniqueConstraint(
            "axis_id",
            "canonical_value",
            "unit_code",
            name=op.f(f"uq_{VALUES}_axis_id_canonical_value_unit_code"),
        ),
    )
    op.create_table(
        MOVES,
        sa.Column("move_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_revision_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("decided_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(MOVES, "sequence >= 1", "sequence_positive"),
        _check(
            MOVES,
            "(sequence = 1) = (previous_revision_id IS NULL)",
            "first_move_has_no_previous",
        ),
        _check(MOVES, "reason <> ''", "reason_present"),
        _check(MOVES, "decided_by <> ''", "decided_by_present"),
        _check(MOVES, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["product_group_id"],
            ["product_groups.product_group_id"],
            name=op.f(f"fk_{MOVES}_product_group_id_product_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            [f"{REVISIONS}.revision_id"],
            name=op.f(f"fk_{MOVES}_revision_id_{REVISIONS}"),
        ),
        sa.ForeignKeyConstraint(
            ["previous_revision_id"],
            [f"{REVISIONS}.revision_id"],
            name=op.f(f"fk_{MOVES}_previous_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("move_id", name=op.f(f"pk_{MOVES}")),
        sa.UniqueConstraint(
            "product_group_id", "sequence", name=op.f(f"uq_{MOVES}_product_group_id_sequence")
        ),
        sa.UniqueConstraint("revision_id", name=op.f(f"uq_{MOVES}_revision_id")),
    )

    for table in (REVISIONS, AXES, VALUES, MOVES):
        _immutable(table)

    op.execute(
        f"CREATE TRIGGER trg_{AXES}_capacity BEFORE INSERT ON {AXES} BEGIN"
        f" SELECT CASE WHEN EXISTS (SELECT 1 FROM {MOVES} WHERE revision_id = NEW.revision_id)"
        " THEN RAISE(ABORT, 'current common option revision is immutable') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {AXES}"
        " WHERE revision_id = NEW.revision_id) >="
        f" (SELECT axis_count FROM {REVISIONS} WHERE revision_id = NEW.revision_id)"
        " THEN RAISE(ABORT, 'common option axis count exceeds revision declaration') END; END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{VALUES}_capacity BEFORE INSERT ON {VALUES} BEGIN"
        f" SELECT CASE WHEN EXISTS (SELECT 1 FROM {MOVES} m JOIN {AXES} a"
        " ON a.revision_id = m.revision_id WHERE a.axis_id = NEW.axis_id)"
        " THEN RAISE(ABORT, 'current common option revision is immutable') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {VALUES} v JOIN {AXES} a"
        " ON a.axis_id = v.axis_id WHERE a.revision_id ="
        f" (SELECT revision_id FROM {AXES} WHERE axis_id = NEW.axis_id)) >="
        f" (SELECT value_count FROM {REVISIONS} WHERE revision_id ="
        f" (SELECT revision_id FROM {AXES} WHERE axis_id = NEW.axis_id))"
        " THEN RAISE(ABORT, 'common option value count exceeds revision declaration') END; END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{MOVES}_complete_revision BEFORE INSERT ON {MOVES} BEGIN"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
        " WHERE r.revision_id = NEW.revision_id"
        " AND r.product_group_id = NEW.product_group_id)"
        " THEN RAISE(ABORT, 'common option revision belongs to another product') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {AXES}"
        " WHERE revision_id = NEW.revision_id) <>"
        f" (SELECT axis_count FROM {REVISIONS} WHERE revision_id = NEW.revision_id)"
        " THEN RAISE(ABORT, 'common option revision has incomplete axes') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {VALUES} v JOIN {AXES} a"
        " ON a.axis_id = v.axis_id WHERE a.revision_id = NEW.revision_id) <>"
        f" (SELECT value_count FROM {REVISIONS} WHERE revision_id = NEW.revision_id)"
        " THEN RAISE(ABORT, 'common option revision has incomplete values') END;"
        f" SELECT CASE WHEN EXISTS (SELECT 1 FROM {AXES} a WHERE a.revision_id ="
        " NEW.revision_id AND NOT EXISTS"
        f" (SELECT 1 FROM {VALUES} v WHERE v.axis_id = a.axis_id))"
        " THEN RAISE(ABORT, 'common option axis has no value') END;"
        f" SELECT CASE WHEN NEW.sequence <> COALESCE((SELECT MAX(sequence) + 1 FROM {MOVES}"
        " WHERE product_group_id = NEW.product_group_id), 1)"
        " THEN RAISE(ABORT, 'common option move sequence is not next') END;"
        f" SELECT CASE WHEN NEW.sequence > 1 AND NEW.previous_revision_id IS NOT"
        f" (SELECT revision_id FROM {MOVES} WHERE product_group_id = NEW.product_group_id"
        " ORDER BY sequence DESC LIMIT 1)"
        " THEN RAISE(ABORT, 'common option move does not follow current revision') END; END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    held = sum(
        int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
        for table in (REVISIONS, AXES, VALUES, MOVES)
    )
    if held:
        raise RuntimeError(
            f"cannot downgrade 0042: {held} Common Sales Option row(s) exist; canonical option"
            " authoring is never silently destroyed"
        )
    op.drop_table(MOVES)
    op.drop_table(VALUES)
    op.drop_table(AXES)
    op.drop_table(REVISIONS)
