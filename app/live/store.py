"""The only writer of the pre-LIVE safety tables (ADR-0018 §3, §3.4, §4; migration 0026).

It records and reads; it never decides whether a mutation may start — that is the safety stack's
(``app.live.stack``), which calls the unit below inside the one write unit that starts an attempt.
Every change is audited in the same unit of work, and every rule the database can hold is held
there too (CHECKs, the partial unique replay index and the triggers of migration 0026), so this
store is not the only guard.

- **Grants** (§3.2, §3.3): issued by an explicit protected operator action that names every field;
  no confirmation prose is accepted, persisted, hashed or logged. A grant is consumed by exactly one
  per started attempt, becomes ``EXHAUSTED`` when its budget is spent, and ``EXPIRED``, ``REVOKED``
  and ``EXHAUSTED`` never return to ``ACTIVE``.
- **The brake** (§4.1): no row, or an unreadable one, is ``ENGAGED``. Engaging is always allowed;
  releasing names a GitHub authorization and never touches a grant.
- **ASSET attempts** (§3.4): ``STARTED`` in the same unit that consumes the grant budget, then
  terminal exactly once. A ``STARTED`` attempt of an earlier process is settled ``UPLOAD_UNKNOWN``:
  a restart never erases the fence.
"""

import hashlib
import json
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.accounts import require_bound
from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database
from app.live.model import (
    ASSET_ENDPOINT_GROUP,
    CREATE_BUDGET,
    CREATE_ENDPOINT_GROUP,
    FENCING_UPLOAD_STATES,
    MAX_ASSET_BUDGET,
    MAX_GRANT_WINDOW_S,
    REPLAY_APPLIED_REUSE_NOT_ADOPTED,
    REPLAY_KEY_VERSION,
    REPLAY_UNRESOLVED,
    TERMINAL_UPLOAD_STATES,
    BrakeState,
    GrantState,
    MutationRefused,
    MutationStage,
    ProofVerdict,
    ReplayKey,
    UploadAttemptState,
    is_hex64,
)
from app.live.models import (
    AssetUploadAttempt,
    LiveGrant,
    ProtectedWriteBrake,
    RestoreDrill,
    RetentionProof,
)
from app.products.image_model import ImageAssetKind
from app.register.sanitize import require_clean, safe_provider_reference

GLOBAL_BRAKE = "GLOBAL"
_COMMENT_ID = re.compile(r"^[0-9]{6,20}$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MEDIA_TYPE = re.compile(r"^[a-z]+/[a-z0-9.+-]{1,48}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class ArtifactRef:
    """One selected artifact of an ASSET grant: provenance, never a replay-key field."""

    asset_kind: ImageAssetKind
    sha256: str
    derivation_id: str | None

    def canonical(self) -> dict[str, Any]:
        return {
            "asset_kind": self.asset_kind.value,
            "sha256": self.sha256,
            "derivation_id": self.derivation_id,
        }


def artifact_set_digest(artifacts: Sequence[ArtifactRef]) -> str:
    encoded = json.dumps(_artifact_document(artifacts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _artifact_document(artifacts: Sequence[ArtifactRef]) -> list[dict[str, Any]]:
    return sorted(
        (a.canonical() for a in artifacts),
        key=lambda d: (d["sha256"], d["asset_kind"], d["derivation_id"] or ""),
    )


@dataclass(frozen=True)
class GrantRecord:
    grant_id: str
    stage: MutationStage
    marketplace_key: str
    marketplace_account_id: str
    endpoint_group: str
    preparation_revision_id: str | None
    candidate_fingerprint: str | None
    artifacts: tuple[ArtifactRef, ...]
    asset_profile: str | None
    registration_snapshot_id: str | None
    intent_id: str | None
    idempotency_key: str | None
    create_attempt_no: int | None
    budget_max: int
    budget_used: int
    not_before: datetime
    expires_at: datetime
    state: GrantState
    approved_by: str
    authorization_ref: str

    def live_at(self, now: datetime) -> bool:
        """``ACTIVE``, inside its window and with budget left: the only grant that can match."""
        return (
            self.state is GrantState.ACTIVE
            and self.not_before <= now < self.expires_at
            and self.budget_used < self.budget_max
        )


@dataclass(frozen=True)
class BrakeRecord:
    state: BrakeState
    # False when no row exists: the fail-closed default, never a released brake.
    recorded: bool
    generation: int = 0
    changed_at: datetime | None = None
    changed_by: str | None = None
    reason_code: str | None = None
    authorization_ref: str | None = None


@dataclass(frozen=True)
class UploadAttemptRecord:
    attempt_id: str
    grant_id: str
    replay_key: str
    attempt_no: int
    marketplace_key: str
    marketplace_account_id: str
    wire_method: str
    wire_host: str
    wire_path: str
    content_sha256: str
    preparation_revision_id: str
    candidate_fingerprint: str
    asset_kind: ImageAssetKind
    artifact_sha256: str
    derivation_id: str | None
    asset_profile: str
    file_name: str
    media_type: str
    state: UploadAttemptState
    process_run_id: str
    started_at: datetime
    finished_at: datetime | None
    provider_asset_ref: str | None
    outcome_reason: str | None


@dataclass(frozen=True)
class UploadProvenance:
    """Why and under what an upload is attempted. None of it keys the replay fence (G3-28)."""

    preparation_revision_id: str
    candidate_fingerprint: str
    artifact: ArtifactRef
    asset_profile: str
    contract_label: str
    file_name: str
    media_type: str


# ---------------------------------------------------------------- the store


class LiveAuthorityStore:
    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    @contextmanager
    def transaction(self) -> Iterator["LiveUnit"]:
        with self._db.write() as session:
            yield LiveUnit(session, self._clock, self._audit)

    @contextmanager
    def reading(self) -> Iterator["LiveUnit"]:
        with self._db.read() as session:
            yield LiveUnit(session, self._clock, self._audit)

    def unit(self, session: Session) -> "LiveUnit":
        """This owner's writes inside another owner's open unit (the CREATE attempt start)."""
        return LiveUnit(session, self._clock, self._audit)

    def grant_record(self, grant_id: str) -> GrantRecord | None:
        with self.reading() as unit:
            return unit.grant_record(grant_id)

    def brake(self) -> BrakeRecord:
        with self.reading() as unit:
            return unit.brake()

    def attempts(self, replay_key: str) -> tuple[UploadAttemptRecord, ...]:
        with self.reading() as unit:
            return unit.attempts(replay_key)

    def attempt(self, attempt_id: str) -> UploadAttemptRecord | None:
        with self.reading() as unit:
            return unit.attempt(attempt_id)


class LiveUnit:
    """The pre-LIVE owners' writes and reads over one caller-owned session. It never commits."""

    def __init__(self, session: Session, clock: Clock, audit: AuditLog) -> None:
        self.session = session
        self._clock = clock
        self._audit = audit

    # ------------------------------------------------------------------ grants (§3)

    def issue_asset_grant(
        self,
        *,
        marketplace_key: str,
        marketplace_account_id: str,
        preparation_revision_id: str,
        candidate_fingerprint: str,
        artifacts: Sequence[ArtifactRef],
        asset_profile: str,
        budget: int,
        not_before: datetime,
        expires_at: datetime,
        approved_by: str,
        authorization_ref: str,
        correlation_id: str,
    ) -> GrantRecord:
        if not is_hex64(candidate_fingerprint):
            raise _invalid("the candidate fingerprint is not a SHA-256")
        if not artifacts:
            raise _invalid("an ASSET grant names at least one exact artifact")
        for artifact in artifacts:
            if not is_hex64(artifact.sha256):
                raise _invalid("an artifact identity is not a SHA-256")
        if not asset_profile:
            raise _invalid("an ASSET grant names its asset profile")
        if not 1 <= budget <= MAX_ASSET_BUDGET:
            raise _invalid(f"an ASSET budget is between 1 and {MAX_ASSET_BUDGET}")
        document = _artifact_document(artifacts)
        row = self._new_grant(
            MutationStage.ASSET,
            marketplace_key,
            marketplace_account_id,
            endpoint_group=ASSET_ENDPOINT_GROUP,
            budget=budget,
            not_before=not_before,
            expires_at=expires_at,
            approved_by=approved_by,
            authorization_ref=authorization_ref,
            correlation_id=correlation_id,
            preparation_revision_id=preparation_revision_id,
            candidate_fingerprint=candidate_fingerprint,
            artifact_set_json=json.dumps(document, sort_keys=True, separators=(",", ":")),
            artifact_set_digest=artifact_set_digest(artifacts),
            asset_profile=asset_profile,
        )
        return _grant_record(row)

    def issue_create_grant(
        self,
        *,
        marketplace_key: str,
        marketplace_account_id: str,
        registration_snapshot_id: str,
        intent_id: str,
        idempotency_key: str,
        create_attempt_no: int,
        not_before: datetime,
        expires_at: datetime,
        approved_by: str,
        authorization_ref: str,
        correlation_id: str,
    ) -> GrantRecord:
        if create_attempt_no < 1 or not idempotency_key:
            raise _invalid("a CREATE grant names the exact attempt and idempotency key")
        row = self._new_grant(
            MutationStage.CREATE,
            marketplace_key,
            marketplace_account_id,
            endpoint_group=CREATE_ENDPOINT_GROUP,
            budget=CREATE_BUDGET,
            not_before=not_before,
            expires_at=expires_at,
            approved_by=approved_by,
            authorization_ref=authorization_ref,
            correlation_id=correlation_id,
            registration_snapshot_id=registration_snapshot_id,
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            create_attempt_no=create_attempt_no,
        )
        return _grant_record(row)

    def _new_grant(
        self,
        stage: MutationStage,
        marketplace_key: str,
        marketplace_account_id: str,
        *,
        endpoint_group: str,
        budget: int,
        not_before: datetime,
        expires_at: datetime,
        approved_by: str,
        authorization_ref: str,
        correlation_id: str,
        **binding: Any,
    ) -> LiveGrant:
        require_bound(self.session, marketplace_key, marketplace_account_id)
        if not approved_by or not correlation_id:
            raise _invalid("a grant names its approver and correlation identity")
        if not _COMMENT_ID.match(authorization_ref):
            raise _invalid("the authorization reference is a GitHub comment identity")
        now = self._clock.now()
        if not expires_at > not_before:
            raise _invalid("a grant window ends after it starts")
        if expires_at - not_before > timedelta(seconds=MAX_GRANT_WINDOW_S):
            raise _invalid("a grant window is at most the server-owned maximum")
        if expires_at <= now:
            raise _invalid("a grant window must not already be over")
        row = LiveGrant(
            grant_id=str(uuid.uuid4()),
            stage=stage.value,
            marketplace_key=marketplace_key,
            marketplace_account_id=marketplace_account_id,
            endpoint_group=endpoint_group,
            budget_max=budget,
            budget_used=0,
            not_before=not_before,
            expires_at=expires_at,
            state=GrantState.ACTIVE.value,
            approved_by=approved_by,
            authorization_ref=authorization_ref,
            correlation_id=correlation_id,
            created_at=now,
            **binding,
        )
        self.session.add(row)
        self.session.flush()
        self._event(
            AuditEventType.LIVE_GRANT_ISSUED,
            "issue_grant",
            approved_by,
            correlation_id,
            f"live_grant:{row.grant_id}",
            after=_grant_audit(row),
            details={"stage": stage.value, "authorization_ref": authorization_ref},
        )
        return row

    def grant_record(self, grant_id: str) -> GrantRecord | None:
        row = self.session.get(LiveGrant, grant_id)
        return None if row is None else _grant_record(row)

    def grants(
        self, stage: MutationStage, marketplace_key: str, account: str
    ) -> tuple[GrantRecord, ...]:
        rows = self.session.scalars(
            select(LiveGrant)
            .where(
                LiveGrant.stage == stage.value,
                LiveGrant.marketplace_key == marketplace_key,
                LiveGrant.marketplace_account_id == account,
            )
            .order_by(LiveGrant.created_at, LiveGrant.grant_id)
        )
        return tuple(_grant_record(row) for row in rows)

    def consume(self, grant_id: str, *, actor: str, correlation_id: str) -> GrantRecord:
        """Spend one unit of budget for one started attempt, in the caller's unit (§3.4, G3-24)."""
        row = self._grant_row(grant_id)
        if not _grant_record(row).live_at(self._clock.now()):
            raise MutationRefused("LIVE_GRANT_NOT_LIVE", "the grant is not live; nothing is sent")
        before = row.budget_used
        row.budget_used = before + 1
        if row.budget_used >= row.budget_max:
            self._end(row, GrantState.EXHAUSTED, actor, "BUDGET_SPENT")
        self.session.flush()
        self._event(
            AuditEventType.LIVE_GRANT_CONSUMED,
            "consume_grant",
            actor,
            correlation_id,
            f"live_grant:{grant_id}",
            before={"budget_used": before},
            after={"budget_used": row.budget_used, "state": row.state},
        )
        return _grant_record(row)

    def revoke(
        self, grant_id: str, *, actor: str, reason_code: str, correlation_id: str
    ) -> GrantRecord:
        row = self._grant_row(grant_id)
        if row.state != GrantState.ACTIVE.value:
            raise InputValidationError(
                "LIVE_GRANT_TERMINAL",
                "a terminal grant never changes",
                details={"state": row.state},
            )
        _require_code(reason_code)
        self._end(row, GrantState.REVOKED, actor, reason_code)
        self.session.flush()
        self._event(
            AuditEventType.LIVE_GRANT_ENDED,
            "revoke_grant",
            actor,
            correlation_id,
            f"live_grant:{grant_id}",
            after={"state": row.state, "end_reason": reason_code},
        )
        return _grant_record(row)

    def expire_due(self, *, correlation_id: str) -> tuple[str, ...]:
        """Materialize every ``ACTIVE`` grant whose window is over as ``EXPIRED`` (terminal)."""
        now = self._clock.now()
        rows = self.session.scalars(
            select(LiveGrant).where(
                LiveGrant.state == GrantState.ACTIVE.value, LiveGrant.expires_at <= now
            )
        ).all()
        for row in rows:
            self._end(row, GrantState.EXPIRED, "system", "WINDOW_OVER")
            self.session.flush()
            self._event(
                AuditEventType.LIVE_GRANT_ENDED,
                "expire_grant",
                "system",
                correlation_id,
                f"live_grant:{row.grant_id}",
                after={"state": row.state, "end_reason": "WINDOW_OVER"},
            )
        return tuple(row.grant_id for row in rows)

    def _end(self, row: LiveGrant, state: GrantState, actor: str, reason: str) -> None:
        row.state = state.value
        row.ended_at = self._clock.now()
        row.ended_by = actor
        row.end_reason = reason

    def _grant_row(self, grant_id: str) -> LiveGrant:
        row = self.session.get(LiveGrant, grant_id)
        if row is None:
            raise NotFoundError("LIVE_GRANT_NOT_FOUND", "no such grant")
        return row

    # ------------------------------------------------------------------ the brake (§4)

    def brake(self) -> BrakeRecord:
        """The brake now. No row is ``ENGAGED``: absence is never a release (§4.1)."""
        row = self.session.get(ProtectedWriteBrake, GLOBAL_BRAKE)
        if row is None:
            return BrakeRecord(state=BrakeState.ENGAGED, recorded=False)
        state = BrakeState(row.state)
        return BrakeRecord(
            state=state,
            recorded=True,
            generation=row.generation,
            changed_at=row.changed_at,
            changed_by=row.changed_by,
            reason_code=row.reason_code,
            authorization_ref=row.authorization_ref,
        )

    def engage(self, *, actor: str, reason_code: str, correlation_id: str) -> BrakeRecord:
        """Always allowed, to an operator and to the system; it stops every attempt not started."""
        return self._set_brake(BrakeState.ENGAGED, actor, reason_code, None, correlation_id)

    def release(
        self, *, actor: str, reason_code: str, authorization_ref: str, correlation_id: str
    ) -> BrakeRecord:
        """A release answers a new explicit authorization; it resurrects and widens no grant."""
        if not _COMMENT_ID.match(authorization_ref):
            raise _invalid("a release names the GitHub comment identity that authorizes it")
        return self._set_brake(
            BrakeState.RELEASED, actor, reason_code, authorization_ref, correlation_id
        )

    def _set_brake(
        self,
        state: BrakeState,
        actor: str,
        reason_code: str,
        authorization_ref: str | None,
        correlation_id: str,
    ) -> BrakeRecord:
        if not actor or not correlation_id:
            raise _invalid("a brake change names its actor and correlation identity")
        _require_code(reason_code)
        before = self.brake()
        now = self._clock.now()
        row = self.session.get(ProtectedWriteBrake, GLOBAL_BRAKE)
        if row is None:
            row = ProtectedWriteBrake(brake_id=GLOBAL_BRAKE, generation=1)
            self.session.add(row)
        else:
            row.generation += 1
        row.state = state.value
        row.changed_at = now
        row.changed_by = actor
        row.reason_code = reason_code
        row.authorization_ref = authorization_ref
        row.correlation_id = correlation_id
        self.session.flush()
        self._event(
            AuditEventType.PROTECTED_WRITE_BRAKE_CHANGED,
            "engage_brake" if state is BrakeState.ENGAGED else "release_brake",
            actor,
            correlation_id,
            "system:protected_write_brake",
            before={"state": before.state.value, "generation": before.generation},
            after={"state": state.value, "generation": row.generation},
            details={"reason_code": reason_code, "authorization_ref": authorization_ref},
        )
        return self.brake()

    # ------------------------------------------------------------------ ASSET attempts (§3.4)

    def attempts(self, replay_key: str) -> tuple[UploadAttemptRecord, ...]:
        rows = self.session.scalars(
            select(AssetUploadAttempt)
            .where(AssetUploadAttempt.replay_key == replay_key)
            .order_by(AssetUploadAttempt.attempt_no)
        )
        return tuple(_attempt_record(row) for row in rows)

    def attempt(self, attempt_id: str) -> UploadAttemptRecord | None:
        row = self.session.get(AssetUploadAttempt, attempt_id)
        return None if row is None else _attempt_record(row)

    def fence(self, key: ReplayKey) -> str | None:
        """Why a fresh upload with this key is blocked, if it is: from the owner's rows (G3-29).

        The whole replay-conflict scope is read — every attempt with this key, whatever grant,
        preparation, candidate, derivation, kind, file name or MIME it was started under.
        """
        states = {attempt.state for attempt in self.attempts(key.digest)}
        if states & {UploadAttemptState.STARTED, UploadAttemptState.UPLOAD_UNKNOWN}:
            return REPLAY_UNRESOLVED
        if UploadAttemptState.APPLIED_PROVEN in states:
            return REPLAY_APPLIED_REUSE_NOT_ADOPTED
        return None

    def start_upload(
        self,
        *,
        grant: GrantRecord,
        key: ReplayKey,
        provenance: UploadProvenance,
        process_run_id: str,
        actor: str,
        correlation_id: str,
    ) -> UploadAttemptRecord:
        """Record ``STARTED`` and spend the grant's budget in this one unit (G3-24).

        Called by the safety stack only, after every layer allowed the upload. If the fence is
        closed — here or by the database's partial unique index — nothing is recorded, nothing is
        spent and nothing is sent.
        """
        _require_provenance(provenance)
        blocked = self.fence(key)
        if blocked is not None:
            raise MutationRefused(blocked, "the replay-conflict scope of this upload is closed")
        attempt_no = (
            self.session.scalar(
                select(func.max(AssetUploadAttempt.attempt_no)).where(
                    AssetUploadAttempt.replay_key == key.digest
                )
            )
            or 0
        ) + 1
        self.consume(grant.grant_id, actor=actor, correlation_id=correlation_id)
        artifact = provenance.artifact
        row = AssetUploadAttempt(
            attempt_id=str(uuid.uuid4()),
            grant_id=grant.grant_id,
            marketplace_key=key.marketplace_key,
            marketplace_account_id=key.marketplace_account_id,
            wire_method=key.endpoint.method,
            wire_host=key.endpoint.host,
            wire_path=key.endpoint.path,
            content_sha256=key.content_sha256,
            replay_key=key.digest,
            replay_key_version=REPLAY_KEY_VERSION,
            attempt_no=attempt_no,
            preparation_revision_id=provenance.preparation_revision_id,
            candidate_fingerprint=provenance.candidate_fingerprint,
            asset_kind=artifact.asset_kind.value,
            artifact_sha256=artifact.sha256,
            derivation_id=artifact.derivation_id,
            asset_profile=provenance.asset_profile,
            endpoint_group=ASSET_ENDPOINT_GROUP,
            contract_label=provenance.contract_label,
            file_name=provenance.file_name,
            media_type=provenance.media_type,
            process_run_id=process_run_id,
            correlation_id=correlation_id,
            state=UploadAttemptState.STARTED.value,
            started_at=self._clock.now(),
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            if "asset_upload_attempts.replay_key" not in str(exc):
                raise
            # The database's own fence caught what the read above did not: still closed.
            raise MutationRefused(REPLAY_UNRESOLVED, "the replay-conflict scope is closed") from exc
        self._event(
            AuditEventType.ASSET_UPLOAD_ATTEMPT_STARTED,
            "start_upload",
            actor,
            correlation_id,
            f"asset_upload_attempt:{row.attempt_id}",
            after={"state": row.state, "replay_key": row.replay_key, "attempt_no": attempt_no},
            details={"grant_id": grant.grant_id},
        )
        return _attempt_record(row)

    def finish_upload(
        self,
        attempt_id: str,
        *,
        state: UploadAttemptState,
        actor: str,
        correlation_id: str,
        provider_asset_ref: str | None = None,
        outcome_reason: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> UploadAttemptRecord:
        """Terminalize one ``STARTED`` attempt, exactly once (§3.4)."""
        if state not in TERMINAL_UPLOAD_STATES:
            raise _invalid("an attempt is terminalized as a terminal outcome")
        row = self.session.get(AssetUploadAttempt, attempt_id)
        if row is None:
            raise NotFoundError("LIVE_ASSET_ATTEMPT_NOT_FOUND", "no such upload attempt")
        if row.state != UploadAttemptState.STARTED.value:
            raise InputValidationError(
                "LIVE_ASSET_ATTEMPT_TERMINAL",
                "an attempt is terminalized exactly once",
                details={"state": row.state},
            )
        if state is UploadAttemptState.APPLIED_PROVEN:
            # Only APPLIED_PROVEN yields a known provider asset, and only a sanitizer-safe one.
            if provider_asset_ref is None or not safe_provider_reference(provider_asset_ref):
                raise _invalid("an applied upload carries one sanitizer-safe provider reference")
        elif provider_asset_ref is not None:
            raise _invalid("only APPLIED_PROVEN yields a provider asset identity")
        else:
            _require_code(outcome_reason or "")
        if evidence is not None:
            require_clean(dict(evidence))
        row.state = state.value
        row.finished_at = self._clock.now()
        row.provider_asset_ref = provider_asset_ref
        row.outcome_reason = outcome_reason
        row.evidence_json = (
            None if evidence is None else json.dumps(dict(evidence), sort_keys=True, default=str)
        )
        self.session.flush()
        self._event(
            AuditEventType.ASSET_UPLOAD_ATTEMPT_SETTLED,
            "finish_upload",
            actor,
            correlation_id,
            f"asset_upload_attempt:{attempt_id}",
            before={"state": UploadAttemptState.STARTED.value},
            after={"state": row.state, "outcome_reason": outcome_reason},
        )
        return _attempt_record(row)

    def settle_interrupted(self, *, process_run_id: str, correlation_id: str) -> tuple[str, ...]:
        """Every ``STARTED`` attempt of an earlier process becomes ``UPLOAD_UNKNOWN`` (G3-25).

        No evidence proves its transmission was precluded, so it is unresolved, never a failure
        and never discarded. An attempt of this process run is still in flight and is left alone.
        """
        rows = self.session.scalars(
            select(AssetUploadAttempt).where(
                AssetUploadAttempt.state == UploadAttemptState.STARTED.value,
                AssetUploadAttempt.process_run_id != process_run_id,
            )
        ).all()
        settled = []
        for row in rows:
            self.finish_upload(
                row.attempt_id,
                state=UploadAttemptState.UPLOAD_UNKNOWN,
                actor="system",
                correlation_id=correlation_id,
                outcome_reason="INTERRUPTED_OUTCOME_UNKNOWN",
            )
            settled.append(row.attempt_id)
        return tuple(settled)

    # ------------------------------------------------------------------ restore drills (§7)

    def record_drill(
        self,
        *,
        stage: MutationStage,
        unit_ref: str,
        target_digest: str,
        verdict: ProofVerdict,
        failure_code: str | None,
        schema_head: str,
        integrity: str | None,
        backup_digest: str | None,
        restore_root_digest: str,
        evidence: Mapping[str, Any],
        element_count: int,
        absent_count: int,
        started_at: datetime,
        actor: str,
        correlation_id: str,
    ) -> str:
        """Record one drill and its sanitized evidence. The only writer of ``restore_drills``."""
        require_clean(dict(evidence))
        row = RestoreDrill(
            drill_id=str(uuid.uuid4()),
            stage=stage.value,
            unit_ref=unit_ref,
            target_digest=target_digest,
            verdict=verdict.value,
            failure_code=failure_code,
            schema_head=schema_head,
            integrity=integrity,
            backup_digest=backup_digest,
            restore_root_digest=restore_root_digest,
            element_count=element_count,
            absent_count=absent_count,
            evidence_json=json.dumps(dict(evidence), sort_keys=True, default=str),
            started_at=started_at,
            finished_at=self._clock.now(),
            actor=actor,
            correlation_id=correlation_id,
        )
        self.session.add(row)
        self.session.flush()
        self._event(
            AuditEventType.RESTORE_DRILL_RECORDED,
            "record_restore_drill",
            actor,
            correlation_id,
            f"restore_drill:{row.drill_id}",
            after={
                "stage": row.stage,
                "verdict": row.verdict,
                "failure_code": failure_code,
                "target_digest": target_digest,
                "schema_head": schema_head,
            },
            details={"unit_ref": unit_ref},
        )
        return row.drill_id

    def passed_drill(self, stage: MutationStage, target_digest: str, schema_head: str) -> bool:
        """Whether a PASSED drill proves exactly this stage, target and schema head."""
        found = self.session.scalar(
            select(func.count())
            .select_from(RestoreDrill)
            .where(
                RestoreDrill.stage == stage.value,
                RestoreDrill.target_digest == target_digest,
                RestoreDrill.schema_head == schema_head,
                RestoreDrill.verdict == ProofVerdict.PASSED.value,
            )
        )
        return bool(found)

    def drill(self, drill_id: str) -> dict[str, Any] | None:
        row = self.session.get(RestoreDrill, drill_id)
        if row is None:
            return None
        return {
            "drill_id": row.drill_id,
            "stage": row.stage,
            "unit_ref": row.unit_ref,
            "target_digest": row.target_digest,
            "verdict": row.verdict,
            "failure_code": row.failure_code,
            "schema_head": row.schema_head,
            "integrity": row.integrity,
            "backup_digest": row.backup_digest,
            "element_count": row.element_count,
            "absent_count": row.absent_count,
            "evidence": json.loads(row.evidence_json),
        }

    # ------------------------------------------------------------------ retention proofs (§8)

    def record_retention_proof(
        self,
        *,
        verdict: ProofVerdict,
        failure_code: str | None,
        schema_head: str,
        check_digest: str,
        checks: Mapping[str, Any],
        actor: str,
        correlation_id: str,
    ) -> str:
        """Record one retention proof. The only writer of ``retention_proofs``."""
        require_clean(dict(checks))
        row = RetentionProof(
            proof_id=str(uuid.uuid4()),
            verdict=verdict.value,
            failure_code=failure_code,
            schema_head=schema_head,
            check_digest=check_digest,
            checks_json=json.dumps(dict(checks), sort_keys=True, default=str),
            recorded_at=self._clock.now(),
            actor=actor,
            correlation_id=correlation_id,
        )
        self.session.add(row)
        self.session.flush()
        self._event(
            AuditEventType.RETENTION_PROOF_RECORDED,
            "record_retention_proof",
            actor,
            correlation_id,
            f"retention_proof:{row.proof_id}",
            after={
                "verdict": row.verdict,
                "failure_code": failure_code,
                "check_digest": check_digest,
            },
        )
        return row.proof_id

    def retention_proven(self, check_digest: str, schema_head: str) -> bool:
        """Whether a PASSED retention proof holds for exactly these live checks and head."""
        found = self.session.scalar(
            select(func.count())
            .select_from(RetentionProof)
            .where(
                RetentionProof.check_digest == check_digest,
                RetentionProof.schema_head == schema_head,
                RetentionProof.verdict == ProofVerdict.PASSED.value,
            )
        )
        return bool(found)

    # ------------------------------------------------------------------ audit

    def owner_writes(self) -> int:
        """The owner-write fence read in this unit (``AuditLog.owner_writes``, Gate 2 G2-C)."""
        return self._audit.owner_writes(self.session)

    def refusal(
        self,
        *,
        stage: MutationStage,
        code: str,
        actor: str,
        correlation_id: str,
        target_ref: str,
        details: Mapping[str, Any],
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.LIVE_MUTATION_REFUSED,
                action=f"refuse_{stage.value.lower()}",
                actor=actor,
                outcome=AuditOutcome.DENIED,
                target_ref=target_ref,
                reason_code=code,
                details={"stage": stage.value, **dict(details)},
                correlation_id=correlation_id,
            ),
            session=self.session,
        )

    def _event(
        self,
        event_type: AuditEventType,
        action: str,
        actor: str,
        correlation_id: str,
        target_ref: str,
        *,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=event_type,
                action=action,
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=target_ref,
                before=before,
                after=after,
                details=details,
                correlation_id=correlation_id,
            ),
            session=self.session,
        )


# ---------------------------------------------------------------- helpers


def _invalid(message: str) -> InputValidationError:
    return InputValidationError("LIVE_INPUT_INVALID", message)


def _require_code(code: str) -> None:
    if not _CODE.match(code):
        raise _invalid("a reason is a code, never prose")


def _require_provenance(provenance: UploadProvenance) -> None:
    if not is_hex64(provenance.candidate_fingerprint) or not is_hex64(provenance.artifact.sha256):
        raise _invalid("the candidate and the artifact are SHA-256 identities")
    if not _FILE_NAME.match(provenance.file_name) or not _MEDIA_TYPE.match(provenance.media_type):
        raise _invalid("the file name and media type are safe local labels")
    if not provenance.asset_profile or not provenance.contract_label:
        raise _invalid("the asset profile and contract label are recorded as provenance")


def _grant_audit(row: LiveGrant) -> dict[str, Any]:
    """The safe identities of a new grant for the audit trail. No confirmation prose exists."""
    return {
        "stage": row.stage,
        "marketplace_key": row.marketplace_key,
        "marketplace_account_id": row.marketplace_account_id,
        "endpoint_group": row.endpoint_group,
        "preparation_revision_id": row.preparation_revision_id,
        "candidate_fingerprint": row.candidate_fingerprint,
        "artifact_set_digest": row.artifact_set_digest,
        "asset_profile": row.asset_profile,
        "registration_snapshot_id": row.registration_snapshot_id,
        "intent_id": row.intent_id,
        "create_attempt_no": row.create_attempt_no,
        "budget_max": row.budget_max,
        "not_before": row.not_before.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "state": row.state,
    }


def _grant_record(row: LiveGrant) -> GrantRecord:
    artifacts = tuple(
        ArtifactRef(
            asset_kind=ImageAssetKind(item["asset_kind"]),
            sha256=item["sha256"],
            derivation_id=item["derivation_id"],
        )
        for item in json.loads(row.artifact_set_json or "[]")
    )
    return GrantRecord(
        grant_id=row.grant_id,
        stage=MutationStage(row.stage),
        marketplace_key=row.marketplace_key,
        marketplace_account_id=row.marketplace_account_id,
        endpoint_group=row.endpoint_group,
        preparation_revision_id=row.preparation_revision_id,
        candidate_fingerprint=row.candidate_fingerprint,
        artifacts=artifacts,
        asset_profile=row.asset_profile,
        registration_snapshot_id=row.registration_snapshot_id,
        intent_id=row.intent_id,
        idempotency_key=row.idempotency_key,
        create_attempt_no=row.create_attempt_no,
        budget_max=row.budget_max,
        budget_used=row.budget_used,
        not_before=row.not_before,
        expires_at=row.expires_at,
        state=GrantState(row.state),
        approved_by=row.approved_by,
        authorization_ref=row.authorization_ref,
    )


def _attempt_record(row: AssetUploadAttempt) -> UploadAttemptRecord:
    return UploadAttemptRecord(
        attempt_id=row.attempt_id,
        grant_id=row.grant_id,
        replay_key=row.replay_key,
        attempt_no=row.attempt_no,
        marketplace_key=row.marketplace_key,
        marketplace_account_id=row.marketplace_account_id,
        wire_method=row.wire_method,
        wire_host=row.wire_host,
        wire_path=row.wire_path,
        content_sha256=row.content_sha256,
        preparation_revision_id=row.preparation_revision_id,
        candidate_fingerprint=row.candidate_fingerprint,
        asset_kind=ImageAssetKind(row.asset_kind),
        artifact_sha256=row.artifact_sha256,
        derivation_id=row.derivation_id,
        asset_profile=row.asset_profile,
        file_name=row.file_name,
        media_type=row.media_type,
        state=UploadAttemptState(row.state),
        process_run_id=row.process_run_id,
        started_at=row.started_at,
        finished_at=row.finished_at,
        provider_asset_ref=row.provider_asset_ref,
        outcome_reason=row.outcome_reason,
    )


__all__ = [
    "FENCING_UPLOAD_STATES",
    "ArtifactRef",
    "BrakeRecord",
    "GrantRecord",
    "LiveAuthorityStore",
    "LiveUnit",
    "UploadAttemptRecord",
    "UploadProvenance",
]
