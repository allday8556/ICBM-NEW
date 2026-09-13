from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    SUCCEEDED = "SUCCEEDED"
    DEAD = "DEAD"


DUE_STATES = (JobState.QUEUED, JobState.RETRY_SCHEDULED)


class AttemptOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class Job(Base):
    """One durable unit of background work (core fields: ARCHITECTURE.md §8)."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('QUEUED', 'RUNNING', 'RETRY_SCHEDULED', 'SUCCEEDED', 'DEAD')",
            name="state_valid",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        Index("ix_jobs_state_next_attempt_at", "state", "next_attempt_at"),
        Index("ix_jobs_correlation_id", "correlation_id"),
        Index("ix_jobs_job_type", "job_type"),
    )

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64))
    target_ref: Mapped[str | None] = mapped_column(String(200))
    payload_json: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20))
    attempt_count: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    correlation_id: Mapped[str] = mapped_column(String(64))
    last_error_class: Mapped[str | None] = mapped_column(String(20))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class JobAttempt(Base):
    """Append-style history of each attempt; it is the evidence that retries ran on schedule."""

    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('SUCCEEDED', 'FAILED', 'INTERRUPTED')",
            name="outcome_valid",
        ),
    )

    attempt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.job_id", ondelete="RESTRICT"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    outcome: Mapped[str | None] = mapped_column(String(20))
    error_class: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
