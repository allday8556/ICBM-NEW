from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime


class AuditEventType(StrEnum):
    PROTECTED_ACTION = "PROTECTED_ACTION"
    JOB_DEAD_LETTERED = "JOB_DEAD_LETTERED"
    DIAGNOSTIC_REQUEST = "DIAGNOSTIC_REQUEST"


class AuditOutcome(StrEnum):
    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    RECORDED = "RECORDED"


class AuditEvent(Base):
    """Immutable audit row. The migration installs triggers rejecting UPDATE and DELETE."""

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("event_id"),
        Index("ix_audit_events_correlation_id", "correlation_id"),
        Index("ix_audit_events_occurred_at", "occurred_at"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    correlation_id: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target_ref: Mapped[str | None] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(20))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    details_json: Mapped[str] = mapped_column(Text)
