"""The protected operator actions of the pre-LIVE owners (ADR-0018 §3.2, §3.3, §4.1).

A grant is created only by an explicit protected action that names every field (§3); this owner
validates each named identity against its own owner before the store records it:

- an ASSET grant names a preparation revision that exists and belongs to the named canonical
  account, the exact candidate fingerprint, the exact selected artifact set and the asset profile;
- a CREATE grant names an Intent that exists, belongs to the named account, is ``PREPARED`` or
  proven not applied, with its own Snapshot and idempotency key, and **the next attempt number**
  — so it authorizes exactly that one attempt, and a retry after ``NOT_APPLIED_PROVEN`` needs a
  new grant (G3-27).

There is no confirmation-prose parameter anywhere: the durable grant keeps only safe identities,
the approver and the authorization reference (G3-23). A grant changes nothing else: no adoption,
capability, readiness, preflight, ComplianceGate, provider truth, write status or ReviewItem.
"""

from collections.abc import Sequence
from datetime import datetime

from app.core.errors import InputValidationError, NotFoundError
from app.live.model import MutationStage
from app.live.store import ArtifactRef, BrakeRecord, GrantRecord, LiveAuthorityStore
from app.register.model import IntentState
from app.register.store import RegistrationStore

SENDABLE = (IntentState.PREPARED, IntentState.FAILED)


class LiveAuthorityService:
    def __init__(self, *, store: LiveAuthorityStore, registrations: RegistrationStore) -> None:
        self._store = store
        self._registrations = registrations

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
        preparation = self._registrations.preparation_of_revision(preparation_revision_id)
        if preparation is None:
            raise NotFoundError("LIVE_GRANT_PREPARATION_NOT_FOUND", "no such preparation revision")
        if (preparation.marketplace_key, preparation.marketplace_account_id) != (
            marketplace_key,
            marketplace_account_id,
        ):
            raise InputValidationError(
                "LIVE_GRANT_ACCOUNT_MISMATCH", "the preparation belongs to another account"
            )
        with self._store.transaction() as unit:
            return unit.issue_asset_grant(
                marketplace_key=marketplace_key,
                marketplace_account_id=marketplace_account_id,
                preparation_revision_id=preparation_revision_id,
                candidate_fingerprint=candidate_fingerprint,
                artifacts=artifacts,
                asset_profile=asset_profile,
                budget=budget,
                not_before=not_before,
                expires_at=expires_at,
                approved_by=approved_by,
                authorization_ref=authorization_ref,
                correlation_id=correlation_id,
            )

    def issue_create_grant(
        self,
        *,
        intent_id: str,
        not_before: datetime,
        expires_at: datetime,
        approved_by: str,
        authorization_ref: str,
        correlation_id: str,
    ) -> GrantRecord:
        intent = self._registrations.intent(intent_id)
        if intent is None:
            raise NotFoundError("LIVE_GRANT_INTENT_NOT_FOUND", "no such registration Intent")
        if intent.state not in SENDABLE:
            raise InputValidationError(
                "LIVE_GRANT_INTENT_NOT_SENDABLE",
                "only a PREPARED Intent or one proven not applied may be granted a CREATE",
                details={"state": intent.state.value},
            )
        next_attempt = len(self._registrations.attempts(intent_id)) + 1
        with self._store.transaction() as unit:
            return unit.issue_create_grant(
                marketplace_key=intent.marketplace_key,
                marketplace_account_id=intent.marketplace_account_id,
                registration_snapshot_id=intent.registration_snapshot_id,
                intent_id=intent.intent_id,
                idempotency_key=intent.idempotency_key,
                create_attempt_no=next_attempt,
                not_before=not_before,
                expires_at=expires_at,
                approved_by=approved_by,
                authorization_ref=authorization_ref,
                correlation_id=correlation_id,
            )

    def revoke(
        self, grant_id: str, *, actor: str, reason_code: str, correlation_id: str
    ) -> GrantRecord:
        with self._store.transaction() as unit:
            return unit.revoke(
                grant_id, actor=actor, reason_code=reason_code, correlation_id=correlation_id
            )

    def expire_due(self, *, correlation_id: str) -> tuple[str, ...]:
        with self._store.transaction() as unit:
            return unit.expire_due(correlation_id=correlation_id)

    def grant_record(self, grant_id: str) -> GrantRecord | None:
        return self._store.grant_record(grant_id)

    def grants(
        self, stage: MutationStage, marketplace_key: str, marketplace_account_id: str
    ) -> tuple[GrantRecord, ...]:
        with self._store.reading() as unit:
            return unit.grants(stage, marketplace_key, marketplace_account_id)

    # ------------------------------------------------------------------ the brake (§4.1)

    def brake(self) -> BrakeRecord:
        return self._store.brake()

    def engage_brake(self, *, actor: str, reason_code: str, correlation_id: str) -> BrakeRecord:
        with self._store.transaction() as unit:
            return unit.engage(actor=actor, reason_code=reason_code, correlation_id=correlation_id)

    def release_brake(
        self, *, actor: str, reason_code: str, authorization_ref: str, correlation_id: str
    ) -> BrakeRecord:
        with self._store.transaction() as unit:
            return unit.release(
                actor=actor,
                reason_code=reason_code,
                authorization_ref=authorization_ref,
                correlation_id=correlation_id,
            )
