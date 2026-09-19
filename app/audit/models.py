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
    # Supplier CONNECT (Issue #7 §11). Payloads are built with app.connect.safe_payload only.
    SUPPLIER_CREDENTIALS_UPDATED = "SUPPLIER_CREDENTIALS_UPDATED"
    SUPPLIER_CONNECTION_TEST_REQUESTED = "SUPPLIER_CONNECTION_TEST_REQUESTED"
    SUPPLIER_AUTH_SUCCEEDED = "SUPPLIER_AUTH_SUCCEEDED"
    SUPPLIER_AUTH_FAILED = "SUPPLIER_AUTH_FAILED"
    SUPPLIER_SESSION_REUSED = "SUPPLIER_SESSION_REUSED"
    SUPPLIER_SESSION_REFRESHED = "SUPPLIER_SESSION_REFRESHED"
    SUPPLIER_CONNECTION_VERIFIED = "SUPPLIER_CONNECTION_VERIFIED"
    SUPPLIER_CONNECTION_FAILED = "SUPPLIER_CONNECTION_FAILED"
    SUPPLIER_AUTH_PAUSED = "SUPPLIER_AUTH_PAUSED"
    SUPPLIER_AUTH_RESUMED = "SUPPLIER_AUTH_RESUMED"
    SUPPLIER_AUTO_CONNECT_CHANGED = "SUPPLIER_AUTO_CONNECT_CHANGED"
    # READY demoted because its persisted session is missing or unreadable (self-healing).
    SUPPLIER_CONNECTION_DEMOTED = "SUPPLIER_CONNECTION_DEMOTED"
    # Marketplace capability truth changed (M2 PR-B): axes before/after, enum values only.
    MARKETPLACE_CAPABILITY_CHANGED = "MARKETPLACE_CAPABILITY_CHANGED"
    # SMARTSTORE-A0-PERMISSION recorded (M2 PR-C): operator-attested, never provider-measured.
    MARKETPLACE_PERMISSION_ATTESTED = "MARKETPLACE_PERMISSION_ATTESTED"
    # SmartStore CONNECT (M2 PR-A): generations only, never credential or account content.
    MARKETPLACE_CREDENTIALS_UPDATED = "MARKETPLACE_CREDENTIALS_UPDATED"
    MARKETPLACE_SESSION_COMMITTED = "MARKETPLACE_SESSION_COMMITTED"
    MARKETPLACE_ACCOUNT_BOUND = "MARKETPLACE_ACCOUNT_BOUND"
    # The canonical seller and marketplace-account identity (M5 PR-B, ACCOUNT_IDENTITY §2):
    # ICBM identifiers only, never a provider account identifier or display name.
    SELLER_ENTITY_RECORDED = "SELLER_ENTITY_RECORDED"
    MARKETPLACE_ACCOUNT_ESTABLISHED = "MARKETPLACE_ACCOUNT_ESTABLISHED"
    # M4 PR-C materialization (ADR-0013 §3, §6): identifiers, reasons and versions only, never a
    # source fact value, page text or URL. Committed in the same unit of work as the change.
    PRODUCT_CURRENT_SOURCE_REVISION_MOVED = "PRODUCT_CURRENT_SOURCE_REVISION_MOVED"
    PRODUCT_MATERIALIZED = "PRODUCT_MATERIALIZED"
    # M4 PR-D pricing (ADR-0013 §7): identifiers, versions, basis/guard enums, fingerprints and the
    # calculated amounts only. Committed with the snapshot and its current-pointer move.
    PRODUCT_PRICING_SNAPSHOT_RECORDED = "PRODUCT_PRICING_SNAPSHOT_RECORDED"
    PRODUCT_CURRENT_PRICING_SNAPSHOT_MOVED = "PRODUCT_CURRENT_PRICING_SNAPSHOT_MOVED"
    # M4 PR-E images (ADR-0013 §9): ids, hashes, versions, roles and order, decision and verdict
    # enums, finding codes. Never a URL, OCR or translation text, prompt or secret.
    PRODUCT_DERIVED_IMAGE_RECORDED = "PRODUCT_DERIVED_IMAGE_RECORDED"
    PRODUCT_IMAGE_SELECTION_RECORDED = "PRODUCT_IMAGE_SELECTION_RECORDED"
    PRODUCT_CURRENT_IMAGE_SELECTION_MOVED = "PRODUCT_CURRENT_IMAGE_SELECTION_MOVED"
    PRODUCT_IMAGE_QA_RECORDED = "PRODUCT_IMAGE_QA_RECORDED"
    # M5 PR-B registration foundation (ADR-0014): identifiers, enums, versions and sanitized
    # digests only. Never a payload value, a URL, a credential or provider response content.
    REGISTRATION_DRAFT_RECORDED = "REGISTRATION_DRAFT_RECORDED"
    REGISTRATION_SNAPSHOT_FROZEN = "REGISTRATION_SNAPSHOT_FROZEN"
    REGISTRATION_INTENT_RECORDED = "REGISTRATION_INTENT_RECORDED"
    REGISTRATION_ATTEMPT_RECORDED = "REGISTRATION_ATTEMPT_RECORDED"
    REGISTRATION_OUTCOME_RESOLVED = "REGISTRATION_OUTCOME_RESOLVED"
    REGISTRATION_VERIFICATION_RECORDED = "REGISTRATION_VERIFICATION_RECORDED"
    REGISTRATION_EXTERNAL_ABSENCE_RECORDED = "REGISTRATION_EXTERNAL_ABSENCE_RECORDED"
    REGISTRATION_DUPLICATE_OVERRIDE_RECORDED = "REGISTRATION_DUPLICATE_OVERRIDE_RECORDED"


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
