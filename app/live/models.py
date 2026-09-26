"""Persistence of the pre-LIVE safety owners (ADR-0018 §3, §3.4, §4; migration 0026).

- ``live_grants``: one bounded, stage-bound LIVE grant each (§3.2). Its binding never changes;
  its budget only grows by one per started attempt; its state leaves ``ACTIVE`` once, for a
  terminal state it never leaves. No confirmation prose has a column (§3.3, G3-23).
- ``protected_write_brakes``: the one global brake row (§4.1). No row means ``ENGAGED``; every
  change moves its generation by exactly one; a release names its authorization.
- ``asset_upload_attempts``: the durable ASSET upload-attempt owner (§3.4). An attempt is recorded
  ``STARTED`` before any transmission and terminalized exactly once. The partial unique index on
  ``replay_key`` is the database's own replay fence: at most one attempt per key is ever
  ``STARTED``, ``UPLOAD_UNKNOWN`` or ``APPLIED_PROVEN``, whatever grant, preparation, candidate,
  derivation, kind, file name or MIME it was started under (G3-28, G3-29).

Every table is append-oriented: a delete is refused by a trigger, and an update may only move the
state machine forward.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime
from app.live.model import (
    CREATE_BUDGET,
    FENCING_UPLOAD_STATES,
    BrakeState,
    GrantState,
    MutationStage,
    ProofVerdict,
    UploadAttemptState,
)


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _in(column: str, values: type[StrEnum]) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({listed})"


def _account() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["marketplace_key", "marketplace_account_id"],
        ["marketplace_accounts.marketplace_key", "marketplace_accounts.marketplace_account_id"],
    )


_ASSET_BOUND = (
    "preparation_revision_id IS NOT NULL AND candidate_fingerprint IS NOT NULL"
    " AND artifact_set_json IS NOT NULL AND artifact_set_digest IS NOT NULL"
    " AND asset_profile IS NOT NULL AND asset_profile <> ''"
    " AND registration_snapshot_id IS NULL AND intent_id IS NULL"
    " AND idempotency_key IS NULL AND create_attempt_no IS NULL"
)
_CREATE_BOUND = (
    "registration_snapshot_id IS NOT NULL AND intent_id IS NOT NULL"
    " AND idempotency_key IS NOT NULL AND idempotency_key <> ''"
    " AND create_attempt_no IS NOT NULL AND create_attempt_no >= 1"
    " AND preparation_revision_id IS NULL AND candidate_fingerprint IS NULL"
    " AND artifact_set_json IS NULL AND artifact_set_digest IS NULL AND asset_profile IS NULL"
)


class LiveGrant(Base):
    """One bounded LIVE grant: one stage, one exact unit, a finite window and budget (§3.2)."""

    __tablename__ = "live_grants"
    __table_args__ = (
        _account(),
        CheckConstraint(_in("stage", MutationStage), name="stage_valid"),
        CheckConstraint(_in("state", GrantState), name="state_valid"),
        CheckConstraint("marketplace_key <> ''", name="marketplace_key_present"),
        CheckConstraint("endpoint_group <> ''", name="endpoint_group_present"),
        # One stage, one unit: the binding of the other stage is empty (§3.2, G3-21).
        CheckConstraint(
            f"(stage = 'ASSET' AND {_ASSET_BOUND}) OR (stage = 'CREATE' AND {_CREATE_BOUND})",
            name="stage_binding_exact",
        ),
        CheckConstraint(
            "candidate_fingerprint IS NULL OR " + _hex64("candidate_fingerprint"),
            name="candidate_fingerprint_hex",
        ),
        CheckConstraint(
            "artifact_set_digest IS NULL OR " + _hex64("artifact_set_digest"),
            name="artifact_set_digest_hex",
        ),
        CheckConstraint(
            "artifact_set_json IS NULL OR (json_valid(artifact_set_json)"
            " AND json_type(artifact_set_json) = 'array'"
            " AND json_array_length(artifact_set_json) >= 1)",
            name="artifact_set_is_array",
        ),
        CheckConstraint("budget_max >= 1", name="budget_finite_and_positive"),
        CheckConstraint(
            f"stage <> 'CREATE' OR budget_max = {CREATE_BUDGET}", name="create_budget_is_one"
        ),
        CheckConstraint("budget_used >= 0 AND budget_used <= budget_max", name="budget_bounded"),
        CheckConstraint("expires_at > not_before", name="window_finite"),
        CheckConstraint(
            "state <> 'EXHAUSTED' OR budget_used = budget_max", name="exhausted_means_spent"
        ),
        CheckConstraint(
            "(state = 'ACTIVE') = (ended_at IS NULL)"
            " AND (ended_at IS NULL) = (ended_by IS NULL)"
            " AND (ended_at IS NULL) = (end_reason IS NULL)",
            name="end_complete",
        ),
        CheckConstraint("approved_by <> ''", name="approved_by_present"),
        # The GitHub authorization reference the approval answers: a comment identity, digits only.
        CheckConstraint(
            "length(authorization_ref) BETWEEN 6 AND 20 AND authorization_ref NOT GLOB '*[^0-9]*'",
            name="authorization_ref_is_comment_id",
        ),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    grant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16))
    marketplace_key: Mapped[str] = mapped_column(String(40))
    marketplace_account_id: Mapped[str] = mapped_column(String(40))
    endpoint_group: Mapped[str] = mapped_column(String(64))
    # ASSET binding (§3.2): the exact preparation revision, candidate, artifact set and profile.
    preparation_revision_id: Mapped[str | None] = mapped_column(String(36))
    candidate_fingerprint: Mapped[str | None] = mapped_column(String(64))
    artifact_set_json: Mapped[str | None] = mapped_column(Text)
    artifact_set_digest: Mapped[str | None] = mapped_column(String(64))
    asset_profile: Mapped[str | None] = mapped_column(String(64))
    # CREATE binding (§3.2): the exact Snapshot, Intent, idempotency key and the one attempt.
    registration_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    intent_id: Mapped[str | None] = mapped_column(String(36))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    create_attempt_no: Mapped[int | None] = mapped_column(Integer)
    budget_max: Mapped[int] = mapped_column(Integer)
    budget_used: Mapped[int] = mapped_column(Integer)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    state: Mapped[str] = mapped_column(String(16))
    approved_by: Mapped[str] = mapped_column(String(64))
    authorization_ref: Mapped[str] = mapped_column(String(20))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ended_by: Mapped[str | None] = mapped_column(String(64))
    end_reason: Mapped[str | None] = mapped_column(String(64))


class ProtectedWriteBrake(Base):
    """The one global protected-write brake (§4.1). An absent row is ``ENGAGED``."""

    __tablename__ = "protected_write_brakes"
    __table_args__ = (
        CheckConstraint("brake_id = 'GLOBAL'", name="one_global_brake"),
        CheckConstraint(_in("state", BrakeState), name="state_valid"),
        CheckConstraint("generation >= 1", name="generation_positive"),
        CheckConstraint("changed_by <> ''", name="changed_by_present"),
        CheckConstraint("reason_code <> ''", name="reason_code_present"),
        # A release answers an explicit authorization (§4.1); an engage needs none.
        CheckConstraint(
            "state <> 'RELEASED' OR (authorization_ref IS NOT NULL"
            " AND length(authorization_ref) BETWEEN 6 AND 20"
            " AND authorization_ref NOT GLOB '*[^0-9]*')",
            name="release_is_authorized",
        ),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    brake_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    generation: Mapped[int] = mapped_column(Integer)
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    changed_by: Mapped[str] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(64))
    authorization_ref: Mapped[str | None] = mapped_column(String(20))
    correlation_id: Mapped[str] = mapped_column(String(64))


# Every state that keeps the fence closed (G3-29): all but a proven non-application.
_FENCING = ", ".join(f"'{state.value}'" for state in sorted(FENCING_UPLOAD_STATES))


class AssetUploadAttempt(Base):
    """One ASSET upload attempt (§3.4). Provenance columns never enter ``replay_key``."""

    __tablename__ = "asset_upload_attempts"
    __table_args__ = (
        _account(),
        UniqueConstraint("replay_key", "attempt_no"),
        # The replay fence (G3-29): one fencing attempt per key, ever, until reconcile or reuse.
        Index(
            "ix_asset_upload_attempts_replay_fence",
            "replay_key",
            unique=True,
            sqlite_where=text(f"state IN ({_FENCING})"),
        ),
        Index("ix_asset_upload_attempts_grant_id", "grant_id"),
        CheckConstraint(_in("state", UploadAttemptState), name="state_valid"),
        CheckConstraint(_hex64("replay_key"), name="replay_key_hex"),
        CheckConstraint(_hex64("content_sha256"), name="content_sha256_hex"),
        CheckConstraint(_hex64("candidate_fingerprint"), name="candidate_fingerprint_hex"),
        # The bytes sent are the artifact's own bytes: M4 artifacts are content-addressed.
        CheckConstraint("content_sha256 = artifact_sha256", name="content_is_the_artifact"),
        CheckConstraint("wire_method IN ('POST', 'PUT', 'PATCH', 'DELETE')", name="method_valid"),
        CheckConstraint("wire_host <> '' AND wire_host = lower(wire_host)", name="host_normalized"),
        CheckConstraint("substr(wire_path, 1, 1) = '/'", name="path_absolute"),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(
            "asset_kind IN ('SOURCE_ASSET', 'DERIVED_ARTIFACT')", name="asset_kind_valid"
        ),
        CheckConstraint(
            "(state = 'STARTED') = (finished_at IS NULL)", name="started_until_terminal"
        ),
        CheckConstraint(
            "(state = 'APPLIED_PROVEN') = (provider_asset_ref IS NOT NULL)",
            name="only_applied_yields_an_asset",
        ),
        CheckConstraint(
            "state IN ('STARTED', 'APPLIED_PROVEN') OR outcome_reason IS NOT NULL",
            name="unapplied_outcome_names_why",
        ),
        CheckConstraint(
            "evidence_json IS NULL OR (json_valid(evidence_json)"
            " AND json_type(evidence_json) = 'object')",
            name="evidence_is_object",
        ),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
        CheckConstraint("process_run_id <> ''", name="process_run_present"),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(36), ForeignKey("live_grants.grant_id"))
    marketplace_key: Mapped[str] = mapped_column(String(40))
    marketplace_account_id: Mapped[str] = mapped_column(String(40))
    # The replay-conflict key and its four fields (G3-28).
    wire_method: Mapped[str] = mapped_column(String(8))
    wire_host: Mapped[str] = mapped_column(String(253))
    wire_path: Mapped[str] = mapped_column(String(255))
    content_sha256: Mapped[str] = mapped_column(String(64))
    replay_key: Mapped[str] = mapped_column(String(64))
    replay_key_version: Mapped[str] = mapped_column(String(40))
    attempt_no: Mapped[int] = mapped_column(Integer)
    # Provenance only: why and under what the attempt was made. Never a key field.
    preparation_revision_id: Mapped[str] = mapped_column(String(36))
    candidate_fingerprint: Mapped[str] = mapped_column(String(64))
    asset_kind: Mapped[str] = mapped_column(String(32))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    derivation_id: Mapped[str | None] = mapped_column(String(36))
    asset_profile: Mapped[str] = mapped_column(String(64))
    endpoint_group: Mapped[str] = mapped_column(String(64))
    contract_label: Mapped[str] = mapped_column(String(64))
    file_name: Mapped[str] = mapped_column(String(128))
    media_type: Mapped[str] = mapped_column(String(64))
    process_run_id: Mapped[str] = mapped_column(String(36))
    correlation_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    provider_asset_ref: Mapped[str | None] = mapped_column(Text)
    outcome_reason: Mapped[str | None] = mapped_column(String(64))
    evidence_json: Mapped[str | None] = mapped_column(Text)


class RestoreDrill(Base):
    """One restore drill (ADR-0018 §7): its target, its verdict and its sanitized evidence.

    Append-only. A PASSED drill is a restore proof only for exactly its stage and target digest,
    at exactly its schema head; any change of that state makes it stale by construction.
    """

    __tablename__ = "restore_drills"
    __table_args__ = (
        CheckConstraint(_in("stage", MutationStage), name="stage_valid"),
        CheckConstraint(_in("verdict", ProofVerdict), name="verdict_valid"),
        CheckConstraint(_hex64("target_digest"), name="target_digest_hex"),
        CheckConstraint(
            "backup_digest IS NULL OR " + _hex64("backup_digest"), name="backup_digest_hex"
        ),
        CheckConstraint(_hex64("restore_root_digest"), name="restore_root_digest_hex"),
        CheckConstraint(
            "(verdict = 'PASSED') = (failure_code IS NULL)"
            " AND (verdict <> 'PASSED' OR (integrity = 'ok' AND backup_digest IS NOT NULL))",
            name="passed_is_complete",
        ),
        CheckConstraint(
            "json_valid(evidence_json) AND json_type(evidence_json) = 'object'",
            name="evidence_is_object",
        ),
        CheckConstraint("unit_ref <> '' AND schema_head <> ''", name="identity_present"),
        CheckConstraint("actor <> '' AND correlation_id <> ''", name="actor_present"),
        Index("ix_restore_drills_stage_target_digest", "stage", "target_digest"),
    )

    drill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16))
    unit_ref: Mapped[str] = mapped_column(String(64))
    target_digest: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(16))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    schema_head: Mapped[str] = mapped_column(String(64))
    integrity: Mapped[str | None] = mapped_column(String(32))
    backup_digest: Mapped[str | None] = mapped_column(String(64))
    restore_root_digest: Mapped[str] = mapped_column(String(64))
    element_count: Mapped[int] = mapped_column(Integer)
    absent_count: Mapped[int] = mapped_column(Integer)
    evidence_json: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime] = mapped_column(UTCDateTime)
    actor: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))


class RetentionProof(Base):
    """One evidence-retention proof (ADR-0018 §8): the checks it ran and their verdict.

    Append-only. It is current only while its check digest equals the live checks' digest at the
    same schema head.
    """

    __tablename__ = "retention_proofs"
    __table_args__ = (
        CheckConstraint(_in("verdict", ProofVerdict), name="verdict_valid"),
        CheckConstraint(_hex64("check_digest"), name="check_digest_hex"),
        CheckConstraint("(verdict = 'PASSED') = (failure_code IS NULL)", name="passed_is_clean"),
        CheckConstraint(
            "json_valid(checks_json) AND json_type(checks_json) = 'object'",
            name="checks_is_object",
        ),
        CheckConstraint("schema_head <> ''", name="schema_head_present"),
        CheckConstraint("actor <> '' AND correlation_id <> ''", name="actor_present"),
    )

    proof_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(16))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    schema_head: Mapped[str] = mapped_column(String(64))
    check_digest: Mapped[str] = mapped_column(String(64))
    checks_json: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)
    actor: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
