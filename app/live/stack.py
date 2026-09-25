"""The send-time safety stack (ADR-0018 §4.3) and the derived stage readiness (§10).

**Deny by default.** A marketplace mutation may start only when every layer allows it, checked in
the one write unit that starts its attempt. Every layer is evaluated and every refusal is named —
never only the first — and a refusal happens before any transmission and is audited after the
unit rolled back, so no half-started attempt and no spent budget survive it.

The layers (§4.3, §10), in order:

1. the execution mode is ``LIVE`` and the execution policy permits live writes — at this main the
   policy is ``M0_DRY_RUN_ONLY``, so this layer refuses every mutation, whatever else is recorded;
2. the protected-write brake is ``RELEASED`` (absent or unreadable is ``ENGAGED``);
3. an ``ACTIVE`` grant of the mutation's stage matches its exact unit, in its window, with budget;
4. the endpoint is adopted and, for an upload, a sender is wired;
5. canary eligibility, a current restore proof, evidence retention and visual acceptance are
   proven — no slice that proves them exists yet, so the production :class:`UnprovenStageProofs`
   refuses each of them;
6. for an upload: the ASSET attempt owner is readable and the replay-conflict scope of the exact
   key is open (§3.4); for a CREATE, ADR-0014 §26's execution-scope brake stays the CREATE-only
   owner it is and is checked by the REGISTER execution owner before this stack runs;
7. the stage's own gate: the current candidate preflight is ``READY`` with the grant's fingerprint.

Readiness is **derived and read-only**: it reports the same layers without consuming anything, and
even ``READY`` is never permission to write.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.execution import ExecutionMode
from app.live.model import (
    ARTIFACT_NOT_GRANTED,
    ATTEMPT_OWNER_UNREADABLE,
    BRAKE_ENGAGED,
    BRAKE_UNREADABLE,
    CANDIDATE_DRIFT,
    CANDIDATE_NOT_READY,
    ELIGIBILITY_UNPROVEN,
    ENDPOINT_NOT_ADOPTED,
    GRANT_MISSING,
    MODE_NOT_LIVE,
    REPLAY_APPLIED_REUSE_NOT_ADOPTED,
    REPLAY_KEY_UNDETERMINABLE,
    REPLAY_UNRESOLVED,
    RESTORE_PROOF_ABSENT,
    RETENTION_UNPROVEN,
    SCOPE_NOT_ACTIVE,
    SENDER_NOT_WIRED,
    TRUTH_MOVED,
    VISUAL_UNRECORDED,
    BrakeState,
    Layer,
    MutationRefused,
    MutationStage,
    ReplayKey,
    WireIdentityError,
)
from app.live.store import (
    ArtifactRef,
    GrantRecord,
    LiveAuthorityStore,
    LiveUnit,
    UploadAttemptRecord,
    UploadProvenance,
)
from app.register.model import ExecutionScopeState
from app.register.store import IntentRecord, ScopeRecord

# A second selection of the same outbound bytes in one unit: one upload may serve it only through
# a reuse/rebind path, which is not adopted. The liveness limit is reported, never hidden.
REPLAY_DUPLICATE_IN_UNIT = "LIVE_ASSET_REPLAY_DUPLICATE_IN_UNIT_REUSE_NOT_ADOPTED"
# The upload's provenance is not the unit its grant names (profile, artifact, revision, candidate).
PROVENANCE_NOT_GRANTED = "LIVE_ASSET_PROVENANCE_NOT_GRANTED"
# Another selected artifact of the grant is STARTED or UPLOAD_UNKNOWN: the stage is held (G3-26).
REPLAY_UNRESOLVED_IN_UNIT = "LIVE_ASSET_REPLAY_UNRESOLVED_IN_UNIT"


class Verdict(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class LayerView:
    layer: Layer
    satisfied: bool
    reason_code: str | None = None

    def canonical(self) -> dict[str, Any]:
        return {"layer": self.layer.value, "satisfied": self.satisfied, "reason": self.reason_code}


@dataclass(frozen=True)
class ArtifactReadiness:
    """One selected artifact of an ASSET grant, judged on its own replay key (non-blocking 1)."""

    artifact: ArtifactRef
    replay_key: str | None
    uploadable: bool
    reason_code: str | None


@dataclass(frozen=True)
class StageReadiness:
    """``ASSET_MUTATION_READY`` / ``CREATE_MUTATION_READY``: derived, read-only, not permission."""

    stage: MutationStage
    verdict: Verdict
    layers: tuple[LayerView, ...]
    artifacts: tuple[ArtifactReadiness, ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(v.reason_code or v.layer.value for v in self.layers if not v.satisfied)


class ModeReader(Protocol):
    """The execution-mode owner's read side (``ExecutionModeService.state``)."""

    def state(self) -> Any: ...


class StageProofs(Protocol):
    """The proven prerequisites of §5, §7, §8 and §9: each a later, separately authorized slice."""

    def canary_non_regulated(self, stage: MutationStage, unit_ref: str) -> bool: ...

    def restore_proof(self, stage: MutationStage, target_digest: str) -> bool: ...

    def evidence_retention_ready(self) -> bool: ...

    def visual_acceptance_recorded(self) -> bool: ...


class UnprovenStageProofs:
    """Production at this main: no eligibility, restore, retention or visual slice exists."""

    def canary_non_regulated(self, stage: MutationStage, unit_ref: str) -> bool:
        return False

    def restore_proof(self, stage: MutationStage, target_digest: str) -> bool:
        return False

    def evidence_retention_ready(self) -> bool:
        return False

    def visual_acceptance_recorded(self) -> bool:
        return False


@dataclass(frozen=True)
class CandidateState:
    """The candidate preflight of a preparation, now (§10 'the stage's own gate')."""

    preparation_revision_id: str
    # False when the grant's revision is no longer the preparation's current revision.
    current: bool
    ready: bool
    fingerprint: str | None


@dataclass(frozen=True)
class AssetTarget:
    """What one upload would do, as the stack judges it."""

    grant_id: str
    key: ReplayKey
    artifact: ArtifactRef
    candidate: CandidateState
    endpoint_adopted: bool
    sender_wired: bool


class SafetyStack:
    def __init__(
        self,
        *,
        store: LiveAuthorityStore,
        mode: ModeReader,
        proofs: StageProofs,
        clock: Any,
    ) -> None:
        self._store = store
        self._mode = mode
        self._proofs = proofs
        self._clock = clock

    # ------------------------------------------------------------------ CREATE (§3.2, G3-27)

    def admit_create(
        self,
        session: Session,
        *,
        intent: IntentRecord,
        attempt_no: int,
        endpoint_adopted: bool,
        scope: ScopeRecord,
        truth_fence: int,
        actor: str,
        correlation_id: str,
    ) -> GrantRecord:
        """Admit one CREATE attempt inside the unit that opens it, and spend its grant.

        The grant must name this exact Snapshot, Intent, idempotency key **and attempt number**:
        after a ``NOT_APPLIED_PROVEN`` attempt the next attempt needs a new grant (G3-27). ``scope``
        is ADR-0014 §26's execution-scope brake as its owner reads it in this same unit: its state
        and generation are part of the restore target a CREATE proof must match (§7).
        """
        unit = self._store.unit(session)
        # First, before this unit writes anything: no owner wrote since the final preflight was
        # evaluated, while the write coordinator now keeps every other writer out (§4.3).
        truth_held = unit.owner_writes() == truth_fence
        grant = self._create_grant(unit, intent, attempt_no)
        layers = self._common(
            unit,
            MutationStage.CREATE,
            unit_ref=f"registration_intent:{intent.intent_id}",
            target_digest=_create_digest(intent, attempt_no, scope),
            endpoint_adopted=endpoint_adopted,
        )
        layers.insert(2, _layer(Layer.GRANT, grant is not None, GRANT_MISSING))
        layers.append(_scope_layer(scope))
        layers.append(_layer(Layer.SEND_TIME_TRUTH, truth_held, TRUTH_MOVED))
        _refuse_unless_all(layers)
        assert grant is not None
        return unit.consume(grant.grant_id, actor=actor, correlation_id=correlation_id)

    def create_readiness(
        self,
        intent: IntentRecord,
        *,
        attempt_no: int,
        endpoint_adopted: bool,
        scope: ScopeRecord,
    ) -> StageReadiness:
        with self._store.reading() as unit:
            grant = self._create_grant(unit, intent, attempt_no)
            layers = self._common(
                unit,
                MutationStage.CREATE,
                unit_ref=f"registration_intent:{intent.intent_id}",
                target_digest=_create_digest(intent, attempt_no, scope),
                endpoint_adopted=endpoint_adopted,
            )
        layers.insert(2, _layer(Layer.GRANT, grant is not None, GRANT_MISSING))
        # §10: the §26 brake ACTIVE is its own requirement, whatever a restore proof says.
        layers.append(_scope_layer(scope))
        return _readiness(MutationStage.CREATE, layers)

    def _create_grant(
        self, unit: LiveUnit, intent: IntentRecord, attempt_no: int
    ) -> GrantRecord | None:
        now = self._clock.now()
        for grant in unit.grants(
            MutationStage.CREATE, intent.marketplace_key, intent.marketplace_account_id
        ):
            if (
                grant.live_at(now)
                and grant.intent_id == intent.intent_id
                and grant.registration_snapshot_id == intent.registration_snapshot_id
                and grant.idempotency_key == intent.idempotency_key
                and grant.create_attempt_no == attempt_no
            ):
                return grant
        return None

    # ------------------------------------------------------------------ ASSET (§3.4)

    def admit_asset(
        self,
        unit: LiveUnit,
        target: AssetTarget,
        provenance: UploadProvenance,
        *,
        truth_fence: int,
        process_run_id: str,
        actor: str,
        correlation_id: str,
    ) -> UploadAttemptRecord:
        """Admit one upload and record it ``STARTED`` with its budget spent, in ``unit``.

        ``truth_fence`` is the owner-write count read just before the candidate was evaluated.
        Read again here first, under the write coordinator, it proves no owner — preparation, M4,
        policy, metadata, capability, account, grant or brake — wrote in between, so the candidate
        this admission rests on is still the current one at the mutation-start boundary (§4.3).
        """
        truth_held = unit.owner_writes() == truth_fence
        grant = unit.grant_record(target.grant_id)
        layers = self._asset_layers(unit, target, grant)
        layers.insert(
            4,
            _layer(
                Layer.GRANT,
                grant is not None
                and provenance.asset_profile == grant.asset_profile
                and provenance.artifact == target.artifact
                and provenance.preparation_revision_id == target.candidate.preparation_revision_id
                and provenance.candidate_fingerprint == target.candidate.fingerprint,
                PROVENANCE_NOT_GRANTED,
            ),
        )
        layers.append(_layer(Layer.SEND_TIME_TRUTH, truth_held, TRUTH_MOVED))
        _refuse_unless_all(layers)
        assert grant is not None
        return unit.start_upload(
            grant=grant,
            key=target.key,
            provenance=provenance,
            process_run_id=process_run_id,
            actor=actor,
            correlation_id=correlation_id,
        )

    def asset_readiness(
        self,
        grant_id: str,
        *,
        keys: Callable[[ArtifactRef], ReplayKey],
        candidate: CandidateState,
        endpoint_adopted: bool,
        sender_wired: bool,
    ) -> StageReadiness:
        """``ASSET_MUTATION_READY`` of every artifact the grant names, each on its own key.

        The fence is per artifact (non-blocking item 1): an applied or unresolved upload of one
        artifact never blocks another artifact of the same unit. Two selections of the same
        outbound bytes share one key, and only one upload may serve them while no reuse/rebind
        path is adopted, so the second is reported as blocked — the liveness limit is named.
        """
        with self._store.reading() as unit:
            grant = unit.grant_record(grant_id)
            artifacts: list[ArtifactReadiness] = []
            seen: set[str] = set()
            first_key: ReplayKey | None = None
            owner_unreadable = False
            for artifact in () if grant is None else grant.artifacts:
                try:
                    key = keys(artifact)
                except (WireIdentityError, MutationRefused) as undeterminable:
                    artifacts.append(ArtifactReadiness(artifact, None, False, undeterminable.code))
                    continue
                first_key = first_key or key
                if key.digest in seen:
                    artifacts.append(
                        ArtifactReadiness(artifact, key.digest, False, REPLAY_DUPLICATE_IN_UNIT)
                    )
                    continue
                seen.add(key.digest)
                try:
                    blocked = _fence(unit, key)
                except SQLAlchemyError:
                    blocked = ATTEMPT_OWNER_UNREADABLE
                    owner_unreadable = True
                artifacts.append(ArtifactReadiness(artifact, key.digest, blocked is None, blocked))
            if grant is None or first_key is None:
                layers = [
                    _layer(Layer.GRANT, grant is not None, GRANT_MISSING),
                    _layer(Layer.REPLAY_FENCE, False, REPLAY_KEY_UNDETERMINABLE),
                ]
            else:
                layers = self._asset_layers(
                    unit,
                    AssetTarget(
                        grant_id=grant_id,
                        key=first_key,
                        artifact=grant.artifacts[0],
                        candidate=candidate,
                        endpoint_adopted=endpoint_adopted,
                        sender_wired=sender_wired,
                    ),
                    grant,
                    fence=False,
                )
        if owner_unreadable:
            layers.append(_layer(Layer.ATTEMPT_OWNER, False, ATTEMPT_OWNER_UNREADABLE))
        if any(a.reason_code == REPLAY_UNRESOLVED for a in artifacts):
            # G3-26: one unresolved selected artifact holds the whole stage.
            layers.append(_layer(Layer.REPLAY_FENCE, False, REPLAY_UNRESOLVED_IN_UNIT))
        if not any(a.uploadable for a in artifacts):
            reason = artifacts[0].reason_code if artifacts else GRANT_MISSING
            layers.append(_layer(Layer.REPLAY_FENCE, False, reason or GRANT_MISSING))
        return _readiness(MutationStage.ASSET, layers, tuple(artifacts))

    def _asset_layers(
        self,
        unit: LiveUnit,
        target: AssetTarget,
        grant: GrantRecord | None,
        *,
        fence: bool = True,
    ) -> list[LayerView]:
        now = self._clock.now()
        matching = (
            grant is not None
            and grant.stage is MutationStage.ASSET
            and grant.live_at(now)
            and grant.marketplace_key == target.key.marketplace_key
            and grant.marketplace_account_id == target.key.marketplace_account_id
            and grant.preparation_revision_id == target.candidate.preparation_revision_id
        )
        try:
            target_digest = _asset_digest(unit, target, grant)
        except SQLAlchemyError:
            # No readable attempt truth, no restore target: never inferred from row absence.
            target_digest = ""
        layers = self._common(
            unit,
            MutationStage.ASSET,
            unit_ref=f"preparation_revision:{target.candidate.preparation_revision_id}",
            target_digest=target_digest,
            endpoint_adopted=target.endpoint_adopted,
        )
        layers.insert(2, _layer(Layer.GRANT, matching, GRANT_MISSING))
        layers.insert(
            3,
            _layer(
                Layer.GRANT,
                grant is not None and target.artifact in grant.artifacts,
                ARTIFACT_NOT_GRANTED,
            ),
        )
        layers.append(_layer(Layer.SENDER_WIRED, target.sender_wired, SENDER_NOT_WIRED))
        if fence:
            try:
                blocked = _fence(unit, target.key)
                unresolved = _unresolved_elsewhere(unit, target.key, grant)
            except SQLAlchemyError:
                layers.append(_layer(Layer.ATTEMPT_OWNER, False, ATTEMPT_OWNER_UNREADABLE))
            else:
                layers.append(_layer(Layer.ATTEMPT_OWNER, True, None))
                layers.append(_layer(Layer.REPLAY_FENCE, blocked is None, blocked or ""))
                # G3-26: a STARTED or UPLOAD_UNKNOWN of **any** selected artifact holds the whole
                # stage. An APPLIED_PROVEN stays artifact-local (cross-audit 7 item 1).
                layers.append(_layer(Layer.REPLAY_FENCE, not unresolved, REPLAY_UNRESOLVED_IN_UNIT))
        candidate = target.candidate
        layers.append(
            _layer(
                Layer.STAGE_GATE,
                candidate.current and candidate.ready,
                CANDIDATE_NOT_READY,
            )
        )
        layers.append(
            _layer(
                Layer.STAGE_GATE,
                grant is not None
                and candidate.current
                and candidate.fingerprint == grant.candidate_fingerprint,
                CANDIDATE_DRIFT,
            )
        )
        return layers

    # ------------------------------------------------------------------ the common layers

    def _common(
        self,
        unit: LiveUnit,
        stage: MutationStage,
        *,
        unit_ref: str,
        target_digest: str,
        endpoint_adopted: bool,
    ) -> list[LayerView]:
        state = self._mode.state()
        live = bool(state.live_writes_permitted) and state.mode is ExecutionMode.LIVE
        try:
            brake = unit.brake()
        except SQLAlchemyError:
            brake_layer = _layer(Layer.PROTECTED_WRITE_BRAKE, False, BRAKE_UNREADABLE)
        else:
            brake_layer = _layer(
                Layer.PROTECTED_WRITE_BRAKE, brake.state is BrakeState.RELEASED, BRAKE_ENGAGED
            )
        proofs = self._proofs
        return [
            _layer(Layer.EXECUTION_MODE, live, MODE_NOT_LIVE),
            brake_layer,
            _layer(Layer.ENDPOINT_ADOPTED, endpoint_adopted, ENDPOINT_NOT_ADOPTED),
            _layer(
                Layer.CANARY_NON_REGULATED,
                proofs.canary_non_regulated(stage, unit_ref),
                ELIGIBILITY_UNPROVEN,
            ),
            _layer(
                Layer.RESTORE_PROOF,
                bool(target_digest) and proofs.restore_proof(stage, target_digest),
                RESTORE_PROOF_ABSENT,
            ),
            _layer(Layer.EVIDENCE_RETENTION, proofs.evidence_retention_ready(), RETENTION_UNPROVEN),
            _layer(Layer.VISUAL_ACCEPTANCE, proofs.visual_acceptance_recorded(), VISUAL_UNRECORDED),
        ]

    # ------------------------------------------------------------------ the send-time fence

    def truth_fence(self) -> int:
        """The owner-write count now: read just before a stage gate is evaluated, and again as the
        first read of the mutation-start unit (Gate 2 G2-C's fence, used at send time)."""
        with self._store.reading() as unit:
            return unit.owner_writes()

    # ------------------------------------------------------------------ refusals

    def record_refusal(
        self,
        refusal: MutationRefused,
        *,
        stage: MutationStage,
        target_ref: str,
        actor: str,
        correlation_id: str,
    ) -> None:
        """Audit a refusal in its own unit: the refused unit rolled back and left nothing."""
        with self._store.transaction() as unit:
            unit.refusal(
                stage=stage,
                code=refusal.code,
                actor=actor,
                correlation_id=correlation_id,
                target_ref=target_ref,
                details={"layers": refusal.details.get("layers", [])},
            )


# ---------------------------------------------------------------- helpers


def _layer(layer: Layer, satisfied: bool, reason: str | None) -> LayerView:
    return LayerView(layer=layer, satisfied=satisfied, reason_code=None if satisfied else reason)


def _scope_layer(scope: ScopeRecord) -> LayerView:
    return _layer(
        Layer.EXECUTION_SCOPE, scope.state is ExecutionScopeState.ACTIVE, SCOPE_NOT_ACTIVE
    )


def _refuse_unless_all(layers: Sequence[LayerView]) -> None:
    failing = [layer for layer in layers if not layer.satisfied]
    if failing:
        raise MutationRefused(
            failing[0].reason_code or failing[0].layer.value,
            "a layer of the send-time safety stack refuses this mutation; nothing is sent",
            details={"layers": [layer.canonical() for layer in failing]},
        )


def _readiness(
    stage: MutationStage,
    layers: Sequence[LayerView],
    artifacts: tuple[ArtifactReadiness, ...] = (),
) -> StageReadiness:
    blocked = any(not layer.satisfied for layer in layers)
    return StageReadiness(
        stage=stage,
        verdict=Verdict.BLOCKED if blocked else Verdict.READY,
        layers=tuple(layers),
        artifacts=artifacts,
    )


def _fence(unit: LiveUnit, key: ReplayKey) -> str | None:
    return unit.fence(key)


def _unresolved_elsewhere(unit: LiveUnit, key: ReplayKey, grant: GrantRecord | None) -> bool:
    """Whether any **other** selected artifact of the grant has a STARTED or UPLOAD_UNKNOWN attempt
    under the same account and wire endpoint: the whole-set read G3-26 requires before a start."""
    if grant is None:
        return False
    for artifact in grant.artifacts:
        if artifact.sha256 == key.content_sha256:
            continue
        if _fence(unit, replace(key, content_sha256=artifact.sha256)) == REPLAY_UNRESOLVED:
            return True
    return False


def _create_digest(intent: IntentRecord, attempt_no: int, scope: ScopeRecord) -> str:
    """The CREATE restore target (§7): the Snapshot, the Intent with its state and idempotency
    key, the attempt it would open, and ADR-0014 §26's execution-scope brake with its state and
    generation. A proof is stale once any of it moves — a pause and a later resume included."""
    return _digest(
        {
            "registration_snapshot_id": intent.registration_snapshot_id,
            "intent_id": intent.intent_id,
            "idempotency_key": intent.idempotency_key,
            "state": intent.state.value,
            "remote_outcome": (
                None if intent.remote_outcome is None else intent.remote_outcome.value
            ),
            "attempt_no": attempt_no,
            "scope": {
                "marketplace_key": scope.marketplace_key,
                "marketplace_account_id": scope.marketplace_account_id,
                "endpoint_group": scope.endpoint_group,
                "state": scope.state.value,
                "pause_reason": None if scope.pause_reason is None else scope.pause_reason.value,
                "paused_at": scope.paused_at,
                "resume_generation": scope.resume_generation,
                "resumed_at": scope.resumed_at,
            },
        }
    )


def _asset_digest(unit: LiveUnit, target: "AssetTarget", grant: GrantRecord | None) -> str:
    """The ASSET restore target (§7): the preparation revision, candidate, artifact set and
    profile, and **for every selected artifact** its replay key with the durable attempt history
    of its whole replay-conflict scope — whatever grant, candidate or profile each attempt was
    started under. A readable owner with no attempt records an explicit empty history; an
    unreadable owner raises, and no target exists at all."""
    artifacts = () if grant is None else grant.artifacts
    endpoint = target.key.endpoint
    scopes: dict[str, Any] = {}
    for artifact in artifacts or (target.artifact,):
        key = replace(target.key, content_sha256=artifact.sha256)
        scopes[key.digest] = {
            "content_sha256": key.content_sha256,
            "attempts": [
                [a.attempt_id, a.attempt_no, a.state.value, a.finished_at]
                for a in unit.attempts(key.digest)
            ],
        }
    return _digest(
        {
            "preparation_revision_id": target.candidate.preparation_revision_id,
            "candidate_fingerprint": target.candidate.fingerprint,
            "artifact_set_digest": None if grant is None else _artifacts_digest(grant),
            "asset_profile": None if grant is None else grant.asset_profile,
            "endpoint": [endpoint.method, endpoint.host, endpoint.path],
            "account": [target.key.marketplace_key, target.key.marketplace_account_id],
            "replay_scopes": scopes,
        }
    )


def _artifacts_digest(grant: GrantRecord) -> str:
    return _digest([a.canonical() for a in grant.artifacts])


def _digest(document: Any) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "REPLAY_APPLIED_REUSE_NOT_ADOPTED",
    "REPLAY_DUPLICATE_IN_UNIT",
    "ArtifactReadiness",
    "AssetTarget",
    "CandidateState",
    "LayerView",
    "ModeReader",
    "SafetyStack",
    "StageProofs",
    "StageReadiness",
    "UnprovenStageProofs",
    "Verdict",
]
