"""Persistence of marketplace capability truth (M2 PR-B).

Each axis is its own column, and each human-action overlay is a row keyed by its typed scope.
CHECK constraints repeat the contract's single-table invariants, so no write path can store a
forbidden value even if it bypasses the domain; the cross-table invariants are enforced by the
domain on every load and save. No secret, token or provider identity is stored here.
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.connect.marketplace.capability import (
    AuthStatus,
    ContractFreshness,
    EvidenceStrength,
    PauseReason,
    RemoteOutcome,
    WorkflowScope,
    WorkflowState,
    WriteScopeStatus,
)
from app.core.errors import ErrorClass
from app.db.base import Base
from app.db.types import UTCDateTime


def _in(column: str, values: Iterable[str], *, nullable: bool = False) -> str:
    clause = f"{column} IN ({', '.join(repr(str(v)) for v in values)})"
    return f"{column} IS NULL OR {clause}" if nullable else clause


class MarketplaceCapability(Base):
    """One row per marketplace with an adopted capability contract (SmartStore in M2)."""

    __tablename__ = "marketplace_capabilities"
    __table_args__ = (
        CheckConstraint(_in("auth", AuthStatus), name="auth_valid"),
        CheckConstraint("auth <> 'READY' OR auth_verified_at IS NOT NULL", name="ready_is_proven"),
        CheckConstraint(_in("write_scope_status", WriteScopeStatus), name="write_scope_valid"),
        CheckConstraint(
            _in("evidence_strength", EvidenceStrength, nullable=True), name="strength_valid"
        ),
        CheckConstraint(
            "(write_scope_status = 'UNKNOWN') = (evidence_strength IS NULL)",
            name="strength_matches_scope",
        ),
        # W1: product write cannot be READY in M2; M5 relaxes this with its own migration.
        CheckConstraint(_in("write_status", ("UNVERIFIED", "BLOCKED")), name="m2_write_unproven"),
        CheckConstraint(
            "write_scope_status <> 'MISSING' OR write_status = 'BLOCKED'",
            name="missing_scope_blocks_write",
        ),
        CheckConstraint(_in("contract_freshness", ContractFreshness), name="freshness_valid"),
        # F2/F7: UNRECORDED has no determination; every recorded determination carries its time.
        CheckConstraint(
            "(contract_freshness = 'UNRECORDED') = (freshness_recorded_at IS NULL)",
            name="freshness_recorded",
        ),
        CheckConstraint(_in("error_class", ErrorClass, nullable=True), name="error_class_valid"),
        CheckConstraint(
            _in("remote_outcome", RemoteOutcome, nullable=True), name="remote_outcome_valid"
        ),
    )

    marketplace_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    auth: Mapped[str] = mapped_column(String(20))
    auth_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    write_scope_status: Mapped[str] = mapped_column(String(20))
    evidence_strength: Mapped[str | None] = mapped_column(String(20))
    write_status: Mapped[str] = mapped_column(String(20))
    contract_freshness: Mapped[str] = mapped_column(String(20))
    freshness_recorded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_class: Mapped[str | None] = mapped_column(String(20))
    remote_outcome: Mapped[str | None] = mapped_column(String(20))
    session_generation_floor: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class MarketplacePermissionAttestation(Base):
    """SMARTSTORE-A0-PERMISSION evidence (M2 PR-C): one row per operator attestation.

    Append-only (the migration installs triggers rejecting UPDATE and DELETE): history is never
    rewritten, and the latest row is the evidence judged for current use. The attested status is
    derived from the groups, never stored as a second truth. Strength and source are pinned: an
    attestation can never be stored as MACHINE_VERIFIED (§17 #18).
    """

    __tablename__ = "marketplace_permission_attestations"
    __table_args__ = (
        CheckConstraint(
            "evidence_source = 'OPERATOR_ATTESTED_PROVIDER_ADMIN'", name="source_is_provider_admin"
        ),
        CheckConstraint("evidence_strength = 'OPERATOR_ATTESTED'", name="a0_never_promoted"),
        CheckConstraint("application_fingerprint <> ''", name="bound_to_application"),
        CheckConstraint("required_groups <> ''", name="required_groups_present"),
        CheckConstraint("endpoint_mapping_revision <> ''", name="bound_to_mapping_revision"),
        Index("ix_marketplace_permission_attestations_marketplace_key", "marketplace_key"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    evidence_source: Mapped[str] = mapped_column(String(40))
    evidence_strength: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Keyed, non-reversible (PERMISSIONS_SCOPES §7.1): the client_id itself is never stored.
    application_fingerprint: Mapped[str] = mapped_column(String(64))
    # Comma-joined ApiGroup values, sorted.
    required_groups: Mapped[str] = mapped_column(String(200))
    observed_groups: Mapped[str] = mapped_column(String(200))
    endpoint_mapping_revision: Mapped[str] = mapped_column(String(64))
    recorded_by: Mapped[str] = mapped_column(String(100))


class MarketplaceWorkflowOverlay(Base):
    """One scoped human-action overlay: at most one per ``workflow_scope`` (primary key)."""

    __tablename__ = "marketplace_workflow_overlays"
    __table_args__ = (
        CheckConstraint(_in("workflow_scope", WorkflowScope), name="scope_valid"),
        CheckConstraint(_in("workflow_state", WorkflowState), name="state_valid"),
        CheckConstraint(_in("reason_code", PauseReason, nullable=True), name="reason_valid"),
        CheckConstraint(
            "(workflow_state = 'PAUSED') = (reason_code IS NOT NULL)", name="paused_has_reason"
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code = 'ACCOUNT_RESTRICTED'"
            " OR (reason_code = 'SCOPE_INSUFFICIENT' AND workflow_scope = 'PRODUCT_REGISTRATION')"
            " OR (reason_code IN ('AUTH_RETRY_LIMIT', 'APPLICATION_REAUTH_REQUIRED')"
            " AND workflow_scope = 'AUTHENTICATION')",
            name="reason_fits_scope",
        ),
        CheckConstraint(
            "(COALESCE(reason_code, '') = 'APPLICATION_REAUTH_REQUIRED')"
            " = (session_generation IS NOT NULL)",
            name="reauth_records_generation",
        ),
    )

    marketplace_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("marketplace_capabilities.marketplace_key"), primary_key=True
    )
    workflow_scope: Mapped[str] = mapped_column(String(30), primary_key=True)
    workflow_state: Mapped[str] = mapped_column(String(20))
    reason_code: Mapped[str | None] = mapped_column(String(40))
    session_generation: Mapped[int | None] = mapped_column(Integer)
    entered_at: Mapped[datetime] = mapped_column(UTCDateTime)
