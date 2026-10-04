"""Durable reviewed Product Fact -> Common Sales Option mapping owner."""

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
from app.stages.products.common_option_mapping import FACT_MAPPING_SIGNATURE_VERSION


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class CommonOptionFactMappingRevision(Base):
    __tablename__ = "common_option_fact_mapping_revisions"
    __table_args__ = (
        UniqueConstraint("product_group_id", "revision_no"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint("axis_mapping_count >= 1", name="axis_mapping_count_positive"),
        CheckConstraint(
            "value_mapping_count >= axis_mapping_count", name="every_axis_maps_a_value"
        ),
        CheckConstraint(_hex64("mapping_signature"), name="mapping_signature_hex"),
        CheckConstraint(
            f"signature_version = '{FACT_MAPPING_SIGNATURE_VERSION}'",
            name="signature_version",
        ),
        CheckConstraint("evidence_reference <> ''", name="evidence_reference_present"),
        CheckConstraint("reason <> ''", name="reason_present"),
        CheckConstraint("reviewed_by <> ''", name="reviewed_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    mapping_revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    common_option_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_sales_option_revisions.revision_id")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    mapping_signature: Mapped[str] = mapped_column(String(64))
    signature_version: Mapped[str] = mapped_column(String(64))
    axis_mapping_count: Mapped[int] = mapped_column(Integer)
    value_mapping_count: Mapped[int] = mapped_column(Integer)
    evidence_reference: Mapped[str] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(String(200))
    reviewed_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    reviewed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CommonOptionFactAxisMapping(Base):
    __tablename__ = "common_option_fact_axis_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_revision_id", "source_field_key"],
            ["product_facts_fields.revision_id", "product_facts_fields.field_key"],
        ),
        UniqueConstraint("mapping_revision_id", "common_option_axis_id"),
        UniqueConstraint("mapping_revision_id", "ordinal"),
        UniqueConstraint(
            "mapping_revision_id", "source_revision_id", "source_field_key", "source_json_path"
        ),
        CheckConstraint("source_field_key <> ''", name="source_field_key_present"),
        CheckConstraint("source_json_path LIKE '$%'", name="source_json_path_present"),
        CheckConstraint("json_valid(source_value_json)", name="source_value_is_json"),
        CheckConstraint(_hex64("source_field_fingerprint"), name="field_fingerprint_hex"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

    axis_mapping_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mapping_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_option_fact_mapping_revisions.mapping_revision_id")
    )
    common_option_axis_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_sales_option_axes.axis_id")
    )
    source_revision_id: Mapped[str] = mapped_column(String(36))
    source_field_key: Mapped[str] = mapped_column(String(40))
    source_json_path: Mapped[str] = mapped_column(String(300))
    source_value_json: Mapped[str] = mapped_column(Text)
    source_field_fingerprint: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)


class CommonOptionFactValueMapping(Base):
    __tablename__ = "common_option_fact_value_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_revision_id", "source_field_key"],
            ["product_facts_fields.revision_id", "product_facts_fields.field_key"],
        ),
        UniqueConstraint("mapping_revision_id", "common_option_value_id"),
        UniqueConstraint("axis_mapping_id", "ordinal"),
        UniqueConstraint(
            "mapping_revision_id", "source_revision_id", "source_field_key", "source_json_path"
        ),
        CheckConstraint("source_field_key <> ''", name="source_field_key_present"),
        CheckConstraint("source_json_path LIKE '$%'", name="source_json_path_present"),
        CheckConstraint("json_valid(source_value_json)", name="source_value_is_json"),
        CheckConstraint(_hex64("source_field_fingerprint"), name="field_fingerprint_hex"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

    value_mapping_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mapping_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_option_fact_mapping_revisions.mapping_revision_id")
    )
    axis_mapping_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_option_fact_axis_mappings.axis_mapping_id")
    )
    common_option_value_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_sales_option_values.value_id")
    )
    source_revision_id: Mapped[str] = mapped_column(String(36))
    source_field_key: Mapped[str] = mapped_column(String(40))
    source_json_path: Mapped[str] = mapped_column(String(300))
    source_value_json: Mapped[str] = mapped_column(Text)
    source_field_fingerprint: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)


class CurrentCommonOptionFactMappingMove(Base):
    __tablename__ = "current_common_option_fact_mapping_moves"
    __table_args__ = (
        UniqueConstraint("product_group_id", "sequence"),
        UniqueConstraint("mapping_revision_id"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "(sequence = 1) = (previous_mapping_revision_id IS NULL)",
            name="first_move_has_no_previous",
        ),
        CheckConstraint("reason <> ''", name="reason_present"),
        CheckConstraint("reviewed_by <> ''", name="reviewed_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    move_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    mapping_revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_option_fact_mapping_revisions.mapping_revision_id")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    previous_mapping_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("common_option_fact_mapping_revisions.mapping_revision_id")
    )
    reason: Mapped[str] = mapped_column(String(200))
    reviewed_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)


__all__ = [
    "CommonOptionFactAxisMapping",
    "CommonOptionFactMappingRevision",
    "CommonOptionFactValueMapping",
    "CurrentCommonOptionFactMappingMove",
]
