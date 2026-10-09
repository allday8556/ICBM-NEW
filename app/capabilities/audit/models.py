from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class AuditEventType(StrEnum):
    PROTECTED_ACTION = "PROTECTED_ACTION"
    JOB_DEAD_LETTERED = "JOB_DEAD_LETTERED"
    DIAGNOSTIC_REQUEST = "DIAGNOSTIC_REQUEST"
    # Supplier CONNECT (Issue #7 §11). Payloads are built with app.stages.connect.safe_payload only.
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
    SUPPLIER_COMMON_IMAGE_DECIDED = "SUPPLIER_COMMON_IMAGE_DECIDED"
    # M5 PR-B registration foundation (ADR-0014): identifiers, enums, versions and sanitized
    # digests only. Never a payload value, a URL, a credential or provider response content.
    REGISTRATION_DRAFT_RECORDED = "REGISTRATION_DRAFT_RECORDED"
    # Owner decision 2026-10-03: an operator created a labelled synthetic test product.
    SYNTHETIC_TEST_PRODUCT_RECORDED = "SYNTHETIC_TEST_PRODUCT_RECORDED"
    REGISTRATION_SNAPSHOT_FROZEN = "REGISTRATION_SNAPSHOT_FROZEN"
    REGISTRATION_INTENT_RECORDED = "REGISTRATION_INTENT_RECORDED"
    REGISTRATION_ATTEMPT_RECORDED = "REGISTRATION_ATTEMPT_RECORDED"
    REGISTRATION_OUTCOME_RESOLVED = "REGISTRATION_OUTCOME_RESOLVED"
    REGISTRATION_VERIFICATION_RECORDED = "REGISTRATION_VERIFICATION_RECORDED"
    REGISTRATION_EXTERNAL_ABSENCE_RECORDED = "REGISTRATION_EXTERNAL_ABSENCE_RECORDED"
    # ADR-0018 §3.5: one deletion attempt of a confirmed registration, its outcome or its read-back.
    REGISTRATION_DELETION_RECORDED = "REGISTRATION_DELETION_RECORDED"
    REGISTRATION_DUPLICATE_OVERRIDE_RECORDED = "REGISTRATION_DUPLICATE_OVERRIDE_RECORDED"
    # M5 PR-E (ADR-0014 §26): the REGISTER send brake of one execution scope. Scope key, cause,
    # policy version, generation and safe actor/reason labels only — the authoritative state is
    # the registration_execution_scopes row, and this is its history.
    REGISTRATION_EXECUTION_SCOPE_PAUSED = "REGISTRATION_EXECUTION_SCOPE_PAUSED"
    REGISTRATION_EXECUTION_SCOPE_RESUMED = "REGISTRATION_EXECUTION_SCOPE_RESUMED"
    # M5 PR-F (ADR-0014 §27): the operator-authored preparation of one provider-listing unit.
    # Identifiers, revision numbers, Item counts and the inputs fingerprint only — never an
    # authored value, which lives in the revision row and never in the audit log.
    REGISTRATION_PREPARATION_RECORDED = "REGISTRATION_PREPARATION_RECORDED"
    REGISTRATION_PREPARATION_REVISED = "REGISTRATION_PREPARATION_REVISED"
    # Gate 1 G1-A (ADR-0015 §2): a revision of one account's registration target policy, appended
    # by the server and made current. Scope, identifiers, revision numbers and the content
    # fingerprint only — never a policy value, which lives in the revision row.
    REGISTRATION_TARGET_POLICY_REVISED = "REGISTRATION_TARGET_POLICY_REVISED"
    # ADR-0026 AIF-1: a PromptTemplate or PlatformPolicy revision (an operator save or reset).
    # Keys, fields, revision ids and fingerprints only, never prompt text.
    AI_PROMPT_TEMPLATE_REVISED = "AI_PROMPT_TEMPLATE_REVISED"
    AI_PLATFORM_POLICY_REVISED = "AI_PLATFORM_POLICY_REVISED"
    # ADR-0026 AIF-3: the results of one task run recorded (statuses, fingerprint, models,
    # billing mode, error code). Never a value, a prompt or a fact.
    AI_ENRICHMENT_RESULT_RECORDED = "AI_ENRICHMENT_RESULT_RECORDED"
    # ADR-0026 AIF-4: an enrichment result applied to one Preparation field as AI_SUGGESTION
    # (result ids, sequence and fingerprint). Never the value.
    AI_ENRICHMENT_APPLIED = "AI_ENRICHMENT_APPLIED"
    # ADR-0027 AIS-1: the AI provider profile configured or approved (executable, routing,
    # data transfer). Paths, hashes, fingerprints and flags only; never a key.
    AI_PROVIDER_PROFILE_REVISED = "AI_PROVIDER_PROFILE_REVISED"
    # Gate 1 G1-B (ADR-0015 §3): a revision of one marketplace × taxonomy × category's reviewed
    # metadata, appended by the server and made current. Key, identifiers, revision number, content
    # fingerprint and the review flag only — never a metadata value.
    REGISTRATION_CATEGORY_METADATA_RECORDED = "REGISTRATION_CATEGORY_METADATA_RECORDED"
    # Official marketplace leaf-category catalog snapshot. Identifiers, counts and digests only.
    MARKETPLACE_CATEGORY_CATALOG_RECORDED = "MARKETPLACE_CATEGORY_CATALOG_RECORDED"
    # ADR-0014 §27.1 (Issue #89 5907626428): a server-owned authoring profile revision — category
    # mapping or detail composition — appended by the server. Kind, scope, identifiers, sequence
    # and the content fingerprint only.
    REGISTRATION_AUTHORING_REVISION_APPENDED = "REGISTRATION_AUTHORING_REVISION_APPENDED"
    # Gate 2 G2-A (ADR-0016 §9): one transition of a ReviewItem, or one human resolution of it.
    # Item identifiers, states, generation, review key, basis, successor and disposition only —
    # never a note, which lives in the event row, and never an owner value.
    REVIEW_ITEM_OPENED = "REVIEW_ITEM_OPENED"
    REVIEW_ITEM_REOPENED = "REVIEW_ITEM_REOPENED"
    REVIEW_ITEM_SUPERSEDED = "REVIEW_ITEM_SUPERSEDED"
    REVIEW_ITEM_RESOLVED = "REVIEW_ITEM_RESOLVED"
    REVIEW_ITEM_RESOLUTION_RECORDED = "REVIEW_ITEM_RESOLUTION_RECORDED"
    # Gate 2 G2-B (ADR-0016 §4, §7): a producer's known indexing failure, and its recovery by a
    # later complete full pass. Producer name and our own failure code only.
    REVIEW_COVERAGE_FAILURE_RECORDED = "REVIEW_COVERAGE_FAILURE_RECORDED"
    REVIEW_COVERAGE_RECOVERED = "REVIEW_COVERAGE_RECOVERED"
    # Gate 3 area 1 (ADR-0018 §3, §3.4, §4): the pre-LIVE safety owners. Every grant transition,
    # every brake change, every ASSET attempt start and settlement, and every refused mutation.
    LIVE_GRANT_ISSUED = "LIVE_GRANT_ISSUED"
    LIVE_GRANT_CONSUMED = "LIVE_GRANT_CONSUMED"
    LIVE_GRANT_ENDED = "LIVE_GRANT_ENDED"
    PROTECTED_WRITE_BRAKE_CHANGED = "PROTECTED_WRITE_BRAKE_CHANGED"
    ASSET_UPLOAD_ATTEMPT_STARTED = "ASSET_UPLOAD_ATTEMPT_STARTED"
    ASSET_UPLOAD_ATTEMPT_SETTLED = "ASSET_UPLOAD_ATTEMPT_SETTLED"
    LIVE_MUTATION_REFUSED = "LIVE_MUTATION_REFUSED"
    # Gate 3 area 2 (ADR-0018 §7, §8): every restore drill and every evidence-retention proof.
    RESTORE_DRILL_RECORDED = "RESTORE_DRILL_RECORDED"
    RETENTION_PROOF_RECORDED = "RETENTION_PROOF_RECORDED"
    # Gate 3 area 3 (ADR-0018 §9): every reviewed populated visual acceptance recorded.
    VISUAL_ACCEPTANCE_RECORDED = "VISUAL_ACCEPTANCE_RECORDED"
    # ADR-0018 §5.1 (Issue #89 5910018106): one canary-eligibility record appended for one exact
    # lineage. Identifiers, sequence, verdict and the packet digest only — never a check's
    # evidence reference, which lives in the record row.
    CANARY_ELIGIBILITY_RECORDED = "CANARY_ELIGIBILITY_RECORDED"
    # ADR-0018 §6.1, G3-30: one durable proof of the user and architect residual-risk acceptance
    # recorded for one account and one risk contract. Identifiers and digests only.
    RESIDUAL_RISK_ACCEPTANCE_RECORDED = "RESIDUAL_RISK_ACCEPTANCE_RECORDED"
    # M6-D (ADR-0023 §7): one order's encrypted shipping record was stored, opened for the
    # detail view, or deleted at the end of its retention. The product-order id only, never
    # a recipient, phone or address.
    ORDER_SHIPPING_STORED = "ORDER_SHIPPING_STORED"
    ORDER_SHIPPING_OPENED = "ORDER_SHIPPING_OPENED"
    ORDER_SHIPPING_DELETED = "ORDER_SHIPPING_DELETED"
    # M6-E (ADR-0024): a SmartStore listing ICBM did not create was adopted by its seller-code
    # convention, or provider evidence ended an adoption. Identifiers only.
    LISTING_ADOPTED = "LISTING_ADOPTED"
    ADOPTED_LISTING_REMOVED = "ADOPTED_LISTING_REMOVED"
    # M6.5 (ADR-0025 §3, §4): the supplier order the operator placed by hand was recorded or
    # amended, and its carrier and tracking number captured. By product-order id only.
    SUPPLIER_ORDER_RECORDED = "SUPPLIER_ORDER_RECORDED"
    ORDER_TRACKING_CAPTURED = "ORDER_TRACKING_CAPTURED"
    # M6.5-C (ADR-0025 §5): one dispatch attempt opened, ended or verified. Ids and codes only.
    ORDER_DISPATCH_RECORDED = "ORDER_DISPATCH_RECORDED"


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
