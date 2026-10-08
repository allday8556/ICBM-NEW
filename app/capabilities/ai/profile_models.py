"""Persistence of the AI provider profile and its call ledger (ADR-0027 §2; AIS-1), migration 0056.

A profile has an immutable identity, append-only revisions (one JSON content object each, never a
secret) and one current pointer that only moves forward. ``ai_provider_calls`` is the append-only
ledger of the calls a profile made, counted per UTC day for the daily cap.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class AIProviderProfile(Base):
    __tablename__ = "ai_provider_profiles"
    __table_args__ = (CheckConstraint("profile_key <> ''", name="key_present"),)

    profile_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIProviderProfileRevision(Base):
    __tablename__ = "ai_provider_profile_revisions"
    __table_args__ = (
        UniqueConstraint("profile_key", "revision_no"),
        CheckConstraint("revision_no >= 1", name="revision_no_positive"),
        CheckConstraint(
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            name="content_is_object",
        ),
        CheckConstraint(
            "length(content_fingerprint) = 64 AND content_fingerprint NOT GLOB '*[^0-9a-f]*'",
            name="content_fingerprint_hex",
        ),
        CheckConstraint(
            "action IN ('CONFIGURE', 'APPROVE_EXECUTABLE', 'APPROVE_ROUTING', 'DATA_TRANSFER')",
            name="action_known",
        ),
        CheckConstraint("authored_by <> ''", name="authored_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("ai_provider_profiles.profile_key")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[str] = mapped_column(Text)
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    authored_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    authored_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIProviderProfileCurrent(Base):
    __tablename__ = "ai_provider_profile_current"
    __table_args__ = (
        CheckConstraint("moved_by <> ''", name="moved_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    profile_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("ai_provider_profiles.profile_key"), primary_key=True
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_provider_profile_revisions.revision_id")
    )
    moved_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    moved_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AIProviderCall(Base):
    __tablename__ = "ai_provider_calls"
    __table_args__ = (
        Index("ix_ai_provider_calls_profile_key_call_day", "profile_key", "call_day"),
        CheckConstraint("outcome IN ('OK', 'FAILED')", name="outcome_known"),
        CheckConstraint(
            "call_day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", name="call_day_iso"
        ),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    profile_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("ai_provider_profiles.profile_key")
    )
    revision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_provider_profile_revisions.revision_id")
    )
    call_day: Mapped[str] = mapped_column(String(10))
    task_key: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(8))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64))
    called_at: Mapped[datetime] = mapped_column(UTCDateTime)
