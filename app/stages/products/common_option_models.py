"""Durable Common Sales Option owner (ADR-0013 owner amendment A–D)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime
from app.stages.products.common_options import COMMON_OPTION_SIGNATURE_VERSION


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class CommonSalesOptionRevision(Base):
    __tablename__ = "common_sales_option_revisions"
    __table_args__ = (
        UniqueConstraint("product_group_id", "revision_no"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint("axis_count >= 1", name="axis_count_positive"),
        CheckConstraint("value_count >= axis_count", name="every_axis_has_value"),
        CheckConstraint(_hex64("structure_signature"), name="signature_hex"),
        CheckConstraint(
            f"signature_version = '{COMMON_OPTION_SIGNATURE_VERSION}'",
            name="signature_version",
        ),
        CheckConstraint("reason <> ''", name="reason_present"),
        CheckConstraint("decided_by <> ''", name="decided_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    structure_signature: Mapped[str] = mapped_column(String(64))
    signature_version: Mapped[str] = mapped_column(String(64))
    axis_count: Mapped[int] = mapped_column(Integer)
    value_count: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CommonSalesOptionAxis(Base):
    __tablename__ = "common_sales_option_axes"
    __table_args__ = (
        UniqueConstraint("revision_id", "semantic_key"),
        UniqueConstraint("revision_id", "ordinal"),
        CheckConstraint(
            "semantic_key GLOB '[a-z]*' AND semantic_key NOT GLOB '*[^a-z0-9_]*'",
            name="semantic_key_canonical",
        ),
        CheckConstraint("length(semantic_key) BETWEEN 1 AND 64", name="semantic_key_bounded"),
        CheckConstraint("length(display_name) BETWEEN 1 AND 100", name="display_name_bounded"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

    axis_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_sales_option_revisions.revision_id")
    )
    semantic_key: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(100))
    ordinal: Mapped[int] = mapped_column(Integer)


class CommonSalesOptionValue(Base):
    __tablename__ = "common_sales_option_values"
    __table_args__ = (
        UniqueConstraint("axis_id", "ordinal"),
        UniqueConstraint("axis_id", "canonical_value", "unit_code"),
        CheckConstraint(
            "length(canonical_value) BETWEEN 1 AND 200", name="canonical_value_bounded"
        ),
        CheckConstraint("length(display_value) BETWEEN 1 AND 200", name="display_value_bounded"),
        CheckConstraint(
            "unit_code = '' OR (length(unit_code) BETWEEN 1 AND 32"
            " AND unit_code GLOB '[a-z]*'"
            " AND unit_code NOT GLOB '*[^a-z0-9_]*')",
            name="unit_code_canonical",
        ),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
    )

    value_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    axis_id: Mapped[str] = mapped_column(String(36), ForeignKey("common_sales_option_axes.axis_id"))
    canonical_value: Mapped[str] = mapped_column(String(200))
    unit_code: Mapped[str] = mapped_column(String(32))
    display_value: Mapped[str] = mapped_column(String(200))
    ordinal: Mapped[int] = mapped_column(Integer)


class CurrentCommonSalesOptionRevisionMove(Base):
    __tablename__ = "current_common_sales_option_revision_moves"
    __table_args__ = (
        UniqueConstraint("product_group_id", "sequence"),
        UniqueConstraint("revision_id"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint(
            "(sequence = 1) = (previous_revision_id IS NULL)",
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
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("common_sales_option_revisions.revision_id")
    )
    sequence: Mapped[int] = mapped_column(Integer)
    previous_revision_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("common_sales_option_revisions.revision_id")
    )
    reason: Mapped[str] = mapped_column(String(200))
    decided_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)


__all__ = [
    "CommonSalesOptionAxis",
    "CommonSalesOptionRevision",
    "CommonSalesOptionValue",
    "CurrentCommonSalesOptionRevisionMove",
]
