"""ORM models of the Adaptive capture owner (migration 0027)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime

CANDIDATE_STATUSES = ("CAPTURED", "REFUSED")
COMMAND_OUTCOMES = ("APPLIED", "RECOVERED", "NOT_APPLIED")
READ_CLASSES = (
    "PRODUCT_READ",
    "IMAGE_REQUEST",
    "POLICY_READ",
    "CONNECT_CONTROL_READ",
    "CONNECT_PROTECTED_READ",
    "CONNECT_AUTHENTICATE",
)
BUDGET_SCOPES = ("CAMPAIGN", "ATTEMPT")
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


class PhaseCCommand(Base):
    """One Phase C harness command, reserved in the data root under its stable correlation before
    it changes anything here. Never updated or deleted."""

    __tablename__ = "adaptive_phase_c_commands"
    __table_args__ = (
        Index("ix_adaptive_phase_c_commands_campaign_id", "campaign_id"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
        CheckConstraint("campaign_id <> ''", name="campaign_present"),
        CheckConstraint("command <> ''", name="command_present"),
    )

    correlation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(64))
    command: Mapped[str] = mapped_column(String(24))
    reserved_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PhaseCCommandResult(Base):
    """What one reserved command did, proven against owner truth: at most one per command."""

    __tablename__ = "adaptive_phase_c_command_results"
    __table_args__ = (
        CheckConstraint("outcome IN ('APPLIED', 'RECOVERED', 'NOT_APPLIED')", name="outcome_valid"),
    )

    correlation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("adaptive_phase_c_commands.correlation_id"), primary_key=True
    )
    outcome: Mapped[str] = mapped_column(String(12))
    settled_at: Mapped[datetime] = mapped_column(UTCDateTime)


_CLASS_CHECK = "request_class IN (" + ", ".join(f"'{c}'" for c in READ_CLASSES) + ")"


class PhaseCReadBudget(Base):
    """One frozen Phase C read ceiling of one campaign's stage and request class (C1 PREP-0).

    Registered once, with the capture request that first names the campaign, and never changed:
    ``CAMPAIGN`` bounds the campaign's whole stage, ``ATTEMPT`` bounds one collection attempt."""

    __tablename__ = "adaptive_phase_c_read_budgets"
    __table_args__ = (
        CheckConstraint("campaign_id <> ''", name="campaign_present"),
        CheckConstraint(_CLASS_CHECK, name="class_valid"),
        CheckConstraint("scope IN ('CAMPAIGN', 'ATTEMPT')", name="scope_valid"),
        CheckConstraint("ceiling >= 0", name="ceiling_valid"),
    )

    campaign_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stage: Mapped[str] = mapped_column(String(4), primary_key=True)
    request_class: Mapped[str] = mapped_column(String(24), primary_key=True)
    scope: Mapped[str] = mapped_column(String(8))
    ceiling: Mapped[int] = mapped_column(Integer)
    registered_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PhaseCRead(Base):
    """One actual Phase C send, durably reserved before it was transmitted (C1 PREP-0)."""

    __tablename__ = "adaptive_phase_c_reads"
    __table_args__ = (
        Index("ix_adaptive_phase_c_reads_campaign_id", "campaign_id"),
        CheckConstraint(_CLASS_CHECK, name="class_valid"),
        CheckConstraint("attempt_no >= 1", name="attempt_valid"),
        CheckConstraint(_hex64("subject_digest"), name="subject_hex"),
    )

    read_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(4))
    request_class: Mapped[str] = mapped_column(String(24))
    collection_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("collection_runs.collection_run_id")
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    subject_digest: Mapped[str] = mapped_column(String(64))
    reserved_at: Mapped[datetime] = mapped_column(UTCDateTime)


class PhaseCReadRefusal(Base):
    """One Phase C send refused before transmission, and why (C1 PREP-0)."""

    __tablename__ = "adaptive_phase_c_read_refusals"
    __table_args__ = (
        Index("ix_adaptive_phase_c_read_refusals_campaign_id", "campaign_id"),
        CheckConstraint("reason <> ''", name="reason_present"),
    )

    refusal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(4))
    request_class: Mapped[str] = mapped_column(String(24))
    collection_run_id: Mapped[str] = mapped_column(String(36))
    attempt_no: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    refused_at: Mapped[datetime] = mapped_column(UTCDateTime)
