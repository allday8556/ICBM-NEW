"""Create stable, source-proven Atomic SKU identities and revision membership.

Revision ID: 0044_source_proven_atomic_skus
Revises: 0043_common_option_fact_mapping
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.stages.products.atomic_sku import ATOMIC_SKU_SIGNATURE_VERSION

revision: str = "0044_source_proven_atomic_skus"
down_revision: str | None = "0043_common_option_fact_mapping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVISIONS = "atomic_sku_set_revisions"
SKUS = "atomic_skus"
SELECTIONS = "atomic_sku_selections"
MEMBERS = "atomic_sku_revision_members"
EVIDENCE = "atomic_sku_revision_selection_evidence"
MOVES = "current_atomic_sku_set_moves"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _immutable(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_{event.lower()} BEFORE {event} ON {table}"
            f" BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


def _hex(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def upgrade() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("sku_set_revision_id", sa.String(36), primary_key=True),
        sa.Column("product_group_id", sa.String(36), nullable=False),
        sa.Column("common_option_revision_id", sa.String(36), nullable=False),
        sa.Column("fact_mapping_revision_id", sa.String(36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("set_signature", sa.String(64), nullable=False),
        sa.Column("signature_version", sa.String(64), nullable=False),
        sa.Column("atomic_sku_count", sa.Integer(), nullable=False),
        sa.Column("selection_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "revision_no >= 1", "revision_no_positive"),
        _check(REVISIONS, "atomic_sku_count >= 1", "atomic_sku_count_positive"),
        _check(REVISIONS, "selection_count >= atomic_sku_count", "selection_count_positive"),
        _check(REVISIONS, _hex("set_signature"), "set_signature_hex"),
        _check(
            REVISIONS,
            f"signature_version = '{ATOMIC_SKU_SIGNATURE_VERSION}'",
            "signature_version",
        ),
        _check(REVISIONS, "reason <> ''", "reason_present"),
        _check(REVISIONS, "decided_by <> ''", "decided_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(["product_group_id"], ["product_groups.product_group_id"]),
        sa.ForeignKeyConstraint(
            ["common_option_revision_id"], ["common_sales_option_revisions.revision_id"]
        ),
        sa.ForeignKeyConstraint(
            ["fact_mapping_revision_id"],
            ["common_option_fact_mapping_revisions.mapping_revision_id"],
        ),
        sa.UniqueConstraint("product_group_id", "revision_no"),
    )
    op.create_table(
        SKUS,
        sa.Column("atomic_sku_id", sa.String(36), primary_key=True),
        sa.Column("product_group_id", sa.String(36), nullable=False),
        sa.Column("selection_signature", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(SKUS, _hex("selection_signature"), "selection_signature_hex"),
        sa.ForeignKeyConstraint(["product_group_id"], ["product_groups.product_group_id"]),
        sa.UniqueConstraint("product_group_id", "selection_signature"),
    )
    op.create_table(
        SELECTIONS,
        sa.Column("selection_id", sa.String(36), primary_key=True),
        sa.Column("atomic_sku_id", sa.String(36), nullable=False),
        sa.Column("semantic_key", sa.String(64), nullable=False),
        sa.Column("canonical_value", sa.String(200), nullable=False),
        sa.Column("unit_code", sa.String(32), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        _check(
            SELECTIONS,
            "semantic_key GLOB '[a-z]*' AND semantic_key NOT GLOB '*[^a-z0-9_]*'",
            "semantic_key_canonical",
        ),
        _check(SELECTIONS, "length(semantic_key) BETWEEN 1 AND 64", "semantic_key_bounded"),
        _check(
            SELECTIONS,
            "length(canonical_value) BETWEEN 1 AND 200",
            "canonical_value_bounded",
        ),
        _check(
            SELECTIONS,
            "unit_code = '' OR (length(unit_code) BETWEEN 1 AND 32"
            " AND unit_code GLOB '[a-z]*'"
            " AND unit_code NOT GLOB '*[^a-z0-9_]*')",
            "unit_code_canonical",
        ),
        _check(SELECTIONS, "ordinal >= 0", "ordinal_non_negative"),
        sa.ForeignKeyConstraint(["atomic_sku_id"], [f"{SKUS}.atomic_sku_id"]),
        sa.UniqueConstraint("atomic_sku_id", "semantic_key"),
        sa.UniqueConstraint("atomic_sku_id", "ordinal"),
    )
    op.create_table(
        MEMBERS,
        sa.Column("revision_member_id", sa.String(36), primary_key=True),
        sa.Column("sku_set_revision_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_id", sa.String(36), nullable=False),
        sa.Column("source_revision_id", sa.String(36), nullable=False),
        sa.Column("source_field_key", sa.String(40), nullable=False),
        sa.Column("source_configuration_path", sa.String(300), nullable=False),
        sa.Column("source_configuration_json", sa.Text(), nullable=False),
        sa.Column("source_field_fingerprint", sa.String(64), nullable=False),
        sa.Column("supplier_sku_id", sa.String(200), nullable=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        _check(MEMBERS, "source_configuration_path LIKE '$%'", "source_path_present"),
        _check(MEMBERS, "json_valid(source_configuration_json)", "source_configuration_json"),
        _check(MEMBERS, _hex("source_field_fingerprint"), "field_fingerprint_hex"),
        _check(MEMBERS, "ordinal >= 0", "ordinal_non_negative"),
        sa.ForeignKeyConstraint(["sku_set_revision_id"], [f"{REVISIONS}.sku_set_revision_id"]),
        sa.ForeignKeyConstraint(["atomic_sku_id"], [f"{SKUS}.atomic_sku_id"]),
        sa.ForeignKeyConstraint(
            ["source_revision_id", "source_field_key"],
            ["product_facts_fields.revision_id", "product_facts_fields.field_key"],
        ),
        sa.UniqueConstraint("sku_set_revision_id", "atomic_sku_id"),
        sa.UniqueConstraint("sku_set_revision_id", "ordinal"),
        sa.UniqueConstraint(
            "sku_set_revision_id",
            "source_revision_id",
            "source_field_key",
            "source_configuration_path",
        ),
    )
    op.create_table(
        EVIDENCE,
        sa.Column("evidence_id", sa.String(36), primary_key=True),
        sa.Column("revision_member_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_selection_id", sa.String(36), nullable=False),
        sa.Column("common_option_axis_id", sa.String(36), nullable=False),
        sa.Column("common_option_value_id", sa.String(36), nullable=False),
        sa.Column("source_revision_id", sa.String(36), nullable=False),
        sa.Column("source_field_key", sa.String(40), nullable=False),
        sa.Column("source_json_path", sa.String(300), nullable=False),
        sa.Column("source_value_json", sa.Text(), nullable=False),
        sa.Column("source_field_fingerprint", sa.String(64), nullable=False),
        _check(EVIDENCE, "source_json_path LIKE '$%'", "source_path_present"),
        _check(EVIDENCE, "json_valid(source_value_json)", "source_value_json"),
        _check(EVIDENCE, _hex("source_field_fingerprint"), "field_fingerprint_hex"),
        sa.ForeignKeyConstraint(["revision_member_id"], [f"{MEMBERS}.revision_member_id"]),
        sa.ForeignKeyConstraint(["atomic_sku_selection_id"], [f"{SELECTIONS}.selection_id"]),
        sa.ForeignKeyConstraint(["common_option_axis_id"], ["common_sales_option_axes.axis_id"]),
        sa.ForeignKeyConstraint(
            ["common_option_value_id"], ["common_sales_option_values.value_id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_revision_id", "source_field_key"],
            ["product_facts_fields.revision_id", "product_facts_fields.field_key"],
        ),
        sa.UniqueConstraint("revision_member_id", "atomic_sku_selection_id"),
        sa.UniqueConstraint("revision_member_id", "common_option_axis_id"),
        sa.UniqueConstraint("revision_member_id", "common_option_value_id"),
        sa.UniqueConstraint("revision_member_id", "source_json_path"),
    )
    op.create_table(
        MOVES,
        sa.Column("move_id", sa.String(36), primary_key=True),
        sa.Column("product_group_id", sa.String(36), nullable=False),
        sa.Column("sku_set_revision_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_sku_set_revision_id", sa.String(36), nullable=True),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("decided_by", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(MOVES, "sequence >= 1", "sequence_positive"),
        _check(
            MOVES,
            "(sequence = 1) = (previous_sku_set_revision_id IS NULL)",
            "first_move_has_no_previous",
        ),
        _check(MOVES, "reason <> ''", "reason_present"),
        _check(MOVES, "decided_by <> ''", "decided_by_present"),
        _check(MOVES, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(["product_group_id"], ["product_groups.product_group_id"]),
        sa.ForeignKeyConstraint(["sku_set_revision_id"], [f"{REVISIONS}.sku_set_revision_id"]),
        sa.ForeignKeyConstraint(
            ["previous_sku_set_revision_id"], [f"{REVISIONS}.sku_set_revision_id"]
        ),
        sa.UniqueConstraint("product_group_id", "sequence"),
        sa.UniqueConstraint("sku_set_revision_id"),
    )

    for table in (REVISIONS, SKUS, SELECTIONS, MEMBERS, EVIDENCE, MOVES):
        _immutable(table)

    op.execute(
        f"CREATE TRIGGER trg_{MEMBERS}_source_guard BEFORE INSERT ON {MEMBERS} BEGIN"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {REVISIONS} sr JOIN {SKUS} s"
        " ON s.atomic_sku_id = NEW.atomic_sku_id"
        " WHERE sr.sku_set_revision_id = NEW.sku_set_revision_id"
        " AND sr.product_group_id = s.product_group_id)"
        " THEN RAISE(ABORT, 'Atomic SKU belongs to another product') END;"
        " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM product_facts_fields f"
        " JOIN product_facts_revisions pr ON pr.revision_id = f.revision_id"
        " JOIN source_products sp ON sp.supplier_key = pr.supplier_key"
        " AND sp.source_product_id = pr.source_product_id"
        " JOIN group_members gm ON gm.source_product_uid = sp.source_product_uid"
        f" JOIN {REVISIONS} sr ON sr.sku_set_revision_id = NEW.sku_set_revision_id"
        " WHERE f.revision_id = NEW.source_revision_id"
        " AND f.field_key = NEW.source_field_key AND f.status = 'CONFIRMED'"
        " AND f.field_fingerprint = NEW.source_field_fingerprint"
        " AND json_type(f.value_json, NEW.source_configuration_path) = 'object'"
        " AND json(json_extract(f.value_json, NEW.source_configuration_path))"
        " = json(NEW.source_configuration_json)"
        " AND NEW.supplier_sku_id IS json_extract("
        " NEW.source_configuration_json, '$.supplier_sku_id')"
        " AND gm.product_group_id = sr.product_group_id AND gm.status = 'CONFIRMED'"
        " AND NEW.source_revision_id = (SELECT revision_id FROM current_source_revision_moves"
        " WHERE source_product_uid = sp.source_product_uid ORDER BY sequence DESC LIMIT 1))"
        " THEN RAISE(ABORT, 'Atomic SKU source configuration is not exact current evidence') END; END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{EVIDENCE}_source_guard BEFORE INSERT ON {EVIDENCE} BEGIN"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {MEMBERS} m"
        f" JOIN {SELECTIONS} s ON s.selection_id = NEW.atomic_sku_selection_id"
        f" JOIN {REVISIONS} sr ON sr.sku_set_revision_id = m.sku_set_revision_id"
        " JOIN common_sales_option_axes a ON a.axis_id = NEW.common_option_axis_id"
        " JOIN common_sales_option_values v ON v.value_id = NEW.common_option_value_id"
        " JOIN product_facts_fields f ON f.revision_id = m.source_revision_id"
        " AND f.field_key = m.source_field_key"
        " WHERE m.revision_member_id = NEW.revision_member_id"
        " AND s.atomic_sku_id = m.atomic_sku_id"
        " AND a.revision_id = sr.common_option_revision_id"
        " AND v.axis_id = a.axis_id"
        " AND s.semantic_key = a.semantic_key"
        " AND s.canonical_value = v.canonical_value"
        " AND s.unit_code = v.unit_code"
        " AND NEW.source_revision_id = m.source_revision_id"
        " AND NEW.source_field_key = m.source_field_key"
        " AND NEW.source_field_fingerprint = m.source_field_fingerprint"
        " AND json_type(f.value_json, NEW.source_json_path) = 'text'"
        " AND json_quote(json_extract(f.value_json, NEW.source_json_path))"
        " = NEW.source_value_json)"
        " THEN RAISE(ABORT, 'Atomic SKU selection evidence is not exact') END; END"
    )
    op.execute(
        f"CREATE TRIGGER trg_{MOVES}_complete_set BEFORE INSERT ON {MOVES} BEGIN"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {MEMBERS}"
        " WHERE sku_set_revision_id = NEW.sku_set_revision_id) <>"
        f" (SELECT atomic_sku_count FROM {REVISIONS}"
        " WHERE sku_set_revision_id = NEW.sku_set_revision_id)"
        " THEN RAISE(ABORT, 'Atomic SKU set is incomplete') END;"
        f" SELECT CASE WHEN (SELECT COUNT(*) FROM {EVIDENCE} e JOIN {MEMBERS} m"
        " ON m.revision_member_id = e.revision_member_id"
        " WHERE m.sku_set_revision_id = NEW.sku_set_revision_id) <>"
        f" (SELECT selection_count FROM {REVISIONS}"
        " WHERE sku_set_revision_id = NEW.sku_set_revision_id)"
        " THEN RAISE(ABORT, 'Atomic SKU evidence is incomplete') END;"
        f" SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {REVISIONS} sr"
        " JOIN current_common_sales_option_revision_moves cm"
        " ON cm.revision_id = sr.common_option_revision_id"
        " JOIN current_common_option_fact_mapping_moves fm"
        " ON fm.mapping_revision_id = sr.fact_mapping_revision_id"
        " WHERE sr.sku_set_revision_id = NEW.sku_set_revision_id"
        " AND sr.product_group_id = NEW.product_group_id"
        " AND cm.sequence = (SELECT MAX(sequence) FROM current_common_sales_option_revision_moves"
        " WHERE product_group_id = NEW.product_group_id)"
        " AND fm.sequence = (SELECT MAX(sequence) FROM current_common_option_fact_mapping_moves"
        " WHERE product_group_id = NEW.product_group_id))"
        " THEN RAISE(ABORT, 'Atomic SKU dependencies are not current') END;"
        f" SELECT CASE WHEN NEW.sequence <> COALESCE((SELECT MAX(sequence) + 1 FROM {MOVES}"
        " WHERE product_group_id = NEW.product_group_id), 1)"
        " THEN RAISE(ABORT, 'Atomic SKU move sequence is not next') END;"
        f" SELECT CASE WHEN NEW.sequence > 1 AND NEW.previous_sku_set_revision_id IS NOT"
        f" (SELECT sku_set_revision_id FROM {MOVES} WHERE product_group_id = NEW.product_group_id"
        " ORDER BY sequence DESC LIMIT 1)"
        " THEN RAISE(ABORT, 'Atomic SKU move does not follow current set') END; END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    tables = (REVISIONS, SKUS, SELECTIONS, MEMBERS, EVIDENCE, MOVES)
    held = sum(
        int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
        for table in tables
    )
    if held:
        raise RuntimeError(f"cannot downgrade 0044: {held} Atomic SKU row(s) exist")
    for table in (MOVES, EVIDENCE, MEMBERS, SELECTIONS, SKUS, REVISIONS):
        op.drop_table(table)
