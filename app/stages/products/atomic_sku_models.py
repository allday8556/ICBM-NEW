"""Durable source-proven Atomic SKU identities and revision membership."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime
from app.stages.products.atomic_sku import ATOMIC_SKU_SIGNATURE_VERSION


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class AtomicSKUSetRevision(Base):
    __tablename__ = "atomic_sku_set_revisions"
    __table_args__ = (
        UniqueConstraint("product_group_id", "revision_no"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint("atomic_sku_count >= 1", name="atomic_sku_count_positive"),
        CheckConstraint("selection_count >= atomic_sku_count", name="selection_count_positive"),
        CheckConstraint(_hex64("set_signature"), name="set_signature_hex"),
        CheckConstraint(
            f"signature_version = '{ATOMIC_SKU_SIGNATURE_VERSION}'", name="signature_version"
        ),
        CheckConstraint("reason <> ''", name="reason_present"),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    sku_set_revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    common_option_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_sales_option_revisions.revision_id")
    )
    fact_mapping_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_option_fact_mapping_revisions.mapping_revision_id")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    set_signature: Mapped[str] = mapped_column(String(64))
    signature_version: Mapped[str] = mapped_column(String(64))
    atomic_sku_count: Mapped[int] = mapped_column(Integer)
    selection_count: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AtomicSKU(Base):
    """Stable canonical identity; never recreated when set membership is revised."""

    __tablename__ = "atomic_skus"
    __table_args__ = (
        UniqueConstraint("product_group_id", "selection_signature"),
        CheckConstraint(_hex64("selection_signature"), name="selection_signature_hex"),
    )

    atomic_sku_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    selection_signature: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AtomicSKUSelection(Base):
    """Stable revision-independent semantic selection of one value for one axis."""

    __tablename__ = "atomic_sku_selections"
    __table_args__ = (
        UniqueConstraint("atomic_sku_id", "semantic_key"),
        UniqueConstraint("atomic_sku_id", "ordinal"),
        CheckConstraint(
            "semantic_key GLOB '[a-z]*' AND semantic_key NOT GLOB '*[^a-z0-9_]*'",
            name="semantic_key_canonical",
        ),
        CheckConstraint("length(semantic_key) BETWEEN 1 AND 64", name="semantic_key_bounded"),
        CheckConstraint(
            "length(canonical_value) BETWEEN 1 AND 200", name="canonical_value_bounded"
        ),
        CheckConstraint(
            "unit_code = '' OR (length(unit_code) BETWEEN 1 AND 32"
            " AND unit_code GLOB '[a-z]*'"
            " AND unit_code NOT GLOB '*[^a-z0-9_]*')",
            name="unit_code_canonical",
        ),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

    selection_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    atomic_sku_id: Mapped[str] = mapped_column(String(36), ForeignKey("atomic_skus.atomic_sku_id"))
    semantic_key: Mapped[str] = mapped_column(String(64))
    canonical_value: Mapped[str] = mapped_column(String(200))
    unit_code: Mapped[str] = mapped_column(String(32))
    ordinal: Mapped[int] = mapped_column(Integer)


class AtomicSKURevisionMember(Base):
    """One stable AtomicSKU's membership and current source proof in a set revision."""

    __tablename__ = "atomic_sku_revision_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_revision_id", "source_field_key"],
            ["product_facts_fields.revision_id", "product_facts_fields.field_key"],
        ),
        UniqueConstraint("sku_set_revision_id", "atomic_sku_id"),
        UniqueConstraint("sku_set_revision_id", "ordinal"),
        UniqueConstraint(
            "sku_set_revision_id",
            "source_revision_id",
            "source_field_key",
            "source_configuration_path",
        ),
        CheckConstraint("source_configuration_path LIKE '$%'", name="source_path_present"),
        CheckConstraint("json_valid(source_configuration_json)", name="source_configuration_json"),
        CheckConstraint(_hex64("source_field_fingerprint"), name="field_fingerprint_hex"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

    revision_member_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sku_set_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_set_revisions.sku_set_revision_id")
    )
    atomic_sku_id: Mapped[str] = mapped_column(String(36), ForeignKey("atomic_skus.atomic_sku_id"))
    source_revision_id: Mapped[str] = mapped_column(String(36))
    source_field_key: Mapped[str] = mapped_column(String(40))
    source_configuration_path: Mapped[str] = mapped_column(String(300))
    source_configuration_json: Mapped[str] = mapped_column(Text)
    source_field_fingerprint: Mapped[str] = mapped_column(String(64))
    supplier_sku_id: Mapped[str | None] = mapped_column(String(200))
    ordinal: Mapped[int] = mapped_column(Integer)


class AtomicSKURevisionSelectionEvidence(Base):
    __tablename__ = "atomic_sku_revision_selection_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_revision_id", "source_field_key"],
            ["product_facts_fields.revision_id", "product_facts_fields.field_key"],
        ),
        UniqueConstraint("revision_member_id", "atomic_sku_selection_id"),
        UniqueConstraint("revision_member_id", "common_option_axis_id"),
        UniqueConstraint("revision_member_id", "common_option_value_id"),
        UniqueConstraint("revision_member_id", "source_json_path"),
        CheckConstraint("source_json_path LIKE '$%'", name="source_path_present"),
        CheckConstraint("json_valid(source_value_json)", name="source_value_json"),
        CheckConstraint(_hex64("source_field_fingerprint"), name="field_fingerprint_hex"),
    )

    evidence_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision_member_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_revision_members.revision_member_id")
    )
    atomic_sku_selection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_selections.selection_id")
    )
    common_option_axis_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_sales_option_axes.axis_id")
    )
    common_option_value_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_sales_option_values.value_id")
    )
    source_revision_id: Mapped[str] = mapped_column(String(36))
    source_field_key: Mapped[str] = mapped_column(String(40))
    source_json_path: Mapped[str] = mapped_column(String(300))
    source_value_json: Mapped[str] = mapped_column(Text)
    source_field_fingerprint: Mapped[str] = mapped_column(String(64))


class CurrentAtomicSKUSetMove(Base):
    __tablename__ = "current_atomic_sku_set_moves"
    __table_args__ = (
        UniqueConstraint("product_group_id", "sequence"),
        UniqueConstraint("sku_set_revision_id"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "(sequence = 1) = (previous_sku_set_revision_id IS NULL)",
            name="first_move_has_no_previous",
        ),
        CheckConstraint("reason <> ''", name="reason_present"),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    move_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    sku_set_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("atomic_sku_set_revisions.sku_set_revision_id")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    previous_sku_set_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("atomic_sku_set_revisions.sku_set_revision_id")
    )
    reason: Mapped[str] = mapped_column(String(200))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)


__all__ = [
    "AtomicSKU",
    "AtomicSKURevisionMember",
    "AtomicSKURevisionSelectionEvidence",
    "AtomicSKUSelection",
    "AtomicSKUSetRevision",
    "CurrentAtomicSKUSetMove",
]
