"""Persistence of the structured enrichment results (ADR-0026 §5; AIF-3), migration 0055.

``product_enrichment_results`` is append-only: one row per recorded result of one task's result
key for one canonical product and one optional target, numbered by exactly one per subject. The
newest row of a subject is its current result. The database refuses an update, a delete, a gap,
a subject key that is not its parts, a partial target, an ``OK`` row without a value object and a
``FAILED`` row with one.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


def _json_object(column: str) -> str:
    return f"json_valid({column}) AND json_type({column}) = 'object'"


class ProductEnrichmentResult(Base):
    __tablename__ = "product_enrichment_results"
    __table_args__ = (
        UniqueConstraint("subject_key", "sequence"),
        ForeignKeyConstraint(
            ["marketplace_key", "marketplace_account_id"],
            ["marketplace_accounts.marketplace_key", "marketplace_accounts.marketplace_account_id"],
        ),
        Index("ix_product_enrichment_results_product_group_id", "product_group_id"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
        CheckConstraint("status IN ('OK', 'FAILED')", name="status_known"),
        CheckConstraint(
            "length(input_fingerprint) = 64 AND input_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="input_fingerprint_hex",
        ),
        CheckConstraint(_json_object("inputs_json"), name="inputs_is_object"),
        CheckConstraint(_json_object("provenance_json"), name="provenance_is_object"),
        CheckConstraint(
            "(marketplace_key IS NULL) = (marketplace_account_id IS NULL)", name="target_is_whole"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        CheckConstraint(
            "(status = 'OK' AND value_json IS NOT NULL AND json_valid(value_json)"
            " AND json_type(value_json) = 'object' AND error_class IS NULL AND error_code IS NULL)"
            " OR (status = 'FAILED' AND value_json IS NULL AND evidence_json IS NULL"
            " AND confidence IS NULL AND requires_review IS NULL"
            " AND error_class IS NOT NULL AND error_code IS NOT NULL)",
            name="status_shape",
        ),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    result_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # ``product|task|result key|marketplace|account``: one numbering per subject.
    subject_key: Mapped[str] = mapped_column(String(255))
    sequence: Mapped[int] = mapped_column(Integer)
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    task_key: Mapped[str] = mapped_column(String(64))
    result_key: Mapped[str] = mapped_column(String(64))
    marketplace_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    marketplace_account_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(8))
    value_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    requires_review: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    inputs_json: Mapped[str] = mapped_column(Text)
    enrichment_schema_version: Mapped[str] = mapped_column(String(64))
    provenance_json: Mapped[str] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)
