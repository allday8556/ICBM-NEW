"""ORM models of the Adaptive capture owner (migration 0027)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime

CANDIDATE_STATUSES = ("CAPTURED", "REFUSED")
REQUEST_MAX_HOURS = 24


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


class CaptureRequest(Base):
    """One Phase C campaign's request that the next ordinary collection of one target keep a
    capture candidate. Consumed by at most one run; unconsumed after ``expires_at``, it lapses."""

    __tablename__ = "adaptive_capture_requests"
    __table_args__ = (
        Index(
            "ix_adaptive_capture_requests_supplier_key_target_digest",
            "supplier_key",
            "target_digest",
        ),
        CheckConstraint("campaign_id <> ''", name="campaign_present"),
        CheckConstraint("supplier_key <> ''", name="supplier_present"),
        CheckConstraint(_hex64("target_digest"), name="target_hex"),
        CheckConstraint("requested_by <> ''", name="author_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
        CheckConstraint("expires_at > requested_at", name="expires_after_request"),
    )

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(64))
    supplier_key: Mapped[str] = mapped_column(String(40))
    target_digest: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CaptureCandidateRecord(Base):
    """What one requested run's capture produced: a sanitized candidate, or why there is none."""

    __tablename__ = "adaptive_capture_candidates"
    __table_args__ = (
        CheckConstraint("status IN ('CAPTURED', 'REFUSED')", name="status_valid"),
        CheckConstraint(
            "(status = 'CAPTURED' AND structure_json IS NOT NULL AND excluded_json IS NOT NULL"
            " AND removals_json IS NOT NULL AND candidate_digest IS NOT NULL AND refusal IS NULL)"
            " OR (status = 'REFUSED' AND structure_json IS NULL AND excluded_json IS NULL"
            " AND removals_json IS NULL AND candidate_digest IS NULL AND refusal IS NOT NULL"
            " AND refusal <> '')",
            name="status_shape",
        ),
        CheckConstraint(
            "candidate_digest IS NULL OR (" + _hex64("candidate_digest") + ")", name="digest_hex"
        ),
    )

    collection_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collection_runs.collection_run_id"), primary_key=True
    )
    request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("adaptive_capture_requests.request_id")
    )
    supplier_key: Mapped[str] = mapped_column(String(40))
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    status: Mapped[str] = mapped_column(String(10))
    structure_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    excluded_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    removals_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refusal: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime)
