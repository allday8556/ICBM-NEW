"""Create AtomicSKU-qualified Product Item identities.

Revision ID: 0045_atomic_sku_product_items
Revises: 0044_source_proven_atomic_skus
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_atomic_sku_product_items"
down_revision: str | None = "0044_source_proven_atomic_skus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "atomic_sku_product_items"


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{TABLE}_{name}"))


def _hex(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("atomic_sku_item_id", sa.String(36), primary_key=True),
        sa.Column("product_group_id", sa.String(36), nullable=False),
        sa.Column("composition_id", sa.String(36), nullable=False),
        sa.Column("composition_signature", sa.String(64), nullable=False),
        sa.Column("atomic_sku_id", sa.String(36), nullable=False),
        sa.Column("atomic_sku_selection_signature", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(_hex("composition_signature"), "composition_signature_hex"),
        _check(_hex("atomic_sku_selection_signature"), "atomic_sku_selection_signature_hex"),
        sa.ForeignKeyConstraint(["product_group_id"], ["product_groups.product_group_id"]),
        sa.ForeignKeyConstraint(["composition_id"], ["listing_compositions.composition_id"]),
        sa.ForeignKeyConstraint(["atomic_sku_id"], ["atomic_skus.atomic_sku_id"]),
        sa.UniqueConstraint("product_group_id", "composition_signature", "atomic_sku_id"),
        sa.UniqueConstraint(
            "product_group_id", "composition_signature", "atomic_sku_selection_signature"
        ),
    )
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER trg_{TABLE}_no_{event.lower()} BEFORE {event} ON {TABLE}"
            f" BEGIN SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
        )
    op.execute(
        f"CREATE TRIGGER trg_{TABLE}_identity_guard BEFORE INSERT ON {TABLE} BEGIN"
        " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM listing_compositions c"
        " WHERE c.composition_id = NEW.composition_id"
        " AND c.composition_signature = NEW.composition_signature)"
        " THEN RAISE(ABORT, 'AtomicSKU Item composition signature is not exact') END;"
        " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM product_groups g"
        " WHERE g.product_group_id = NEW.product_group_id AND g.status = 'ACTIVE')"
        " THEN RAISE(ABORT, 'AtomicSKU Item belongs to an ACTIVE group') END;"
        " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM atomic_skus s"
        " WHERE s.atomic_sku_id = NEW.atomic_sku_id"
        " AND s.product_group_id = NEW.product_group_id"
        " AND s.selection_signature = NEW.atomic_sku_selection_signature)"
        " THEN RAISE(ABORT, 'AtomicSKU Item identity is not exact') END;"
        " SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM current_atomic_sku_set_moves m"
        " JOIN atomic_sku_set_revisions sr"
        " ON sr.sku_set_revision_id = m.sku_set_revision_id"
        " JOIN atomic_sku_revision_members r"
        " ON r.sku_set_revision_id = m.sku_set_revision_id"
        " JOIN common_option_fact_mapping_revisions mr"
        " ON mr.mapping_revision_id = sr.fact_mapping_revision_id"
        " WHERE m.product_group_id = NEW.product_group_id"
        " AND r.atomic_sku_id = NEW.atomic_sku_id"
        " AND m.sequence = (SELECT MAX(sequence) FROM current_atomic_sku_set_moves"
        " WHERE product_group_id = NEW.product_group_id)"
        " AND sr.common_option_revision_id = (SELECT revision_id"
        " FROM current_common_sales_option_revision_moves"
        " WHERE product_group_id = NEW.product_group_id ORDER BY sequence DESC LIMIT 1)"
        " AND sr.fact_mapping_revision_id = (SELECT mapping_revision_id"
        " FROM current_common_option_fact_mapping_moves"
        " WHERE product_group_id = NEW.product_group_id ORDER BY sequence DESC LIMIT 1)"
        " AND mr.product_group_id = NEW.product_group_id"
        " AND mr.common_option_revision_id = sr.common_option_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM atomic_sku_revision_members dependency"
        " WHERE dependency.sku_set_revision_id = sr.sku_set_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM product_facts_revisions pr"
        " JOIN source_products sp ON sp.supplier_key = pr.supplier_key"
        " AND sp.source_product_id = pr.source_product_id"
        " JOIN group_members gm ON gm.source_product_uid = sp.source_product_uid"
        " JOIN current_source_revision_moves sm"
        " ON sm.source_product_uid = sp.source_product_uid"
        " WHERE pr.revision_id = dependency.source_revision_id"
        " AND gm.product_group_id = NEW.product_group_id AND gm.status = 'CONFIRMED'"
        " AND sm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = sp.source_product_uid)"
        " AND sm.revision_id = dependency.source_revision_id))"
        " AND NOT EXISTS (SELECT 1 FROM common_option_fact_axis_mappings dependency"
        " WHERE dependency.mapping_revision_id = sr.fact_mapping_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM product_facts_revisions pr"
        " JOIN source_products sp ON sp.supplier_key = pr.supplier_key"
        " AND sp.source_product_id = pr.source_product_id"
        " JOIN group_members gm ON gm.source_product_uid = sp.source_product_uid"
        " JOIN current_source_revision_moves sm"
        " ON sm.source_product_uid = sp.source_product_uid"
        " WHERE pr.revision_id = dependency.source_revision_id"
        " AND gm.product_group_id = NEW.product_group_id AND gm.status = 'CONFIRMED'"
        " AND sm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = sp.source_product_uid)"
        " AND sm.revision_id = dependency.source_revision_id))"
        " AND NOT EXISTS (SELECT 1 FROM common_option_fact_value_mappings dependency"
        " WHERE dependency.mapping_revision_id = sr.fact_mapping_revision_id"
        " AND NOT EXISTS (SELECT 1 FROM product_facts_revisions pr"
        " JOIN source_products sp ON sp.supplier_key = pr.supplier_key"
        " AND sp.source_product_id = pr.source_product_id"
        " JOIN group_members gm ON gm.source_product_uid = sp.source_product_uid"
        " JOIN current_source_revision_moves sm"
        " ON sm.source_product_uid = sp.source_product_uid"
        " WHERE pr.revision_id = dependency.source_revision_id"
        " AND gm.product_group_id = NEW.product_group_id AND gm.status = 'CONFIRMED'"
        " AND sm.sequence = (SELECT MAX(sequence) FROM current_source_revision_moves"
        " WHERE source_product_uid = sp.source_product_uid)"
        " AND sm.revision_id = dependency.source_revision_id)))"
        " THEN RAISE(ABORT, 'AtomicSKU is not current for use') END; END"
    )


def downgrade() -> None:
    connection = op.get_bind()
    held = int(connection.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one())
    if held:
        raise RuntimeError(f"cannot downgrade 0045: {held} AtomicSKU Item row(s) exist")
    op.drop_table(TABLE)
