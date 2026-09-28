"""The CREATE stage's own gate, from the REGISTER owners (ADR-0018 §10; area 1 carry-forward).

``CREATE_MUTATION_READY`` may never report ``READY`` on the stack's layers alone: the stage's own
gate — the final preflight ``READY`` under current truth with the fingerprint the Snapshot froze, a
sendable Intent, and no unresolved conflict — is derived here from the owners and handed to the
readiness as :class:`~app.live.stack.StageGate`. Nothing is decided here that the owners do not
already decide; every reason is the owner's own code.

:class:`CanaryStageReadiness` is the other direction of the same seam: the overall canary readiness
only **summarizes** the two mutation stages (ADR-0018 §10), so it reads each stage's verdict from
the very owners the send-time stack uses instead of deriving a second, weaker one. Read-only: no
grant is consumed, no proof is recorded, and even ``READY`` is never permission to write.
"""

from collections.abc import Callable
from typing import Any, Protocol

from app.core.errors import AppError
from app.live.model import MutationStage
from app.live.stack import SafetyStack, StageGate, StageReadiness, Verdict
from app.live.store import LiveAuthorityStore
from app.products.model import ReadinessStatus
from app.register.model import IntentState
from app.register.store import IntentRecord, RegistrationStore

SENDABLE = (IntentState.PREPARED, IntentState.FAILED)


class ExecutionCopySource(Protocol):
    """``RegistrationPreparationService.execution_copy``: the send gate re-evaluated from the exact
    authored revision that produced the Snapshot."""

    def execution_copy(self, registration_snapshot_id: str) -> Any: ...


def create_stage_gate(
    *, registrations: RegistrationStore, preparations: ExecutionCopySource, intent: IntentRecord
) -> StageGate:
    reasons: list[str] = []
    if intent.state not in SENDABLE:
        reasons.append("REGISTER_NOT_SENDABLE")
    if registrations.conflicting_intents(intent.registration_snapshot_id):
        reasons.append("REGISTER_UNRESOLVED_CONFLICT")
    snapshot = registrations.snapshot(intent.registration_snapshot_id)
    try:
        copy = preparations.execution_copy(intent.registration_snapshot_id)
    except AppError as refused:
        reasons.append(refused.code)
    else:
        if copy.final.status is not ReadinessStatus.READY:
            reasons.append("REGISTER_SEND_PREFLIGHT_NOT_READY")
        if snapshot is None or copy.final.dependency_fingerprint != snapshot.preflight_fingerprint:
            reasons.append("REGISTER_SEND_FINGERPRINT_DRIFT")
    return StageGate(ready=not reasons, reasons=tuple(reasons))


class AssetReadinessSource(Protocol):
    """``AssetUploadService.readiness``: ``ASSET_MUTATION_READY`` of one grant's artifacts."""

    def readiness(self, grant_id: str) -> StageReadiness: ...


class CanaryStageReadiness:
    """``ASSET_MUTATION_READY`` / ``CREATE_MUTATION_READY`` of one provider-listing unit (§10).

    Every verdict comes from the owner that decides it — :meth:`SafetyStack.create_readiness` and
    :meth:`AssetUploadService.readiness` — so the whole safety stack of each stage (the execution
    mode and policy, the protected-write brake, the stage's exact grant, endpoint adoption and the
    sender, canary eligibility, the current restore proof, evidence retention, the recorded visual
    acceptance, the durable upload-attempt owner, the ADR-0014 §26 scope brake and the stage's own
    gate) reaches the canary summary. The endpoint-adoption layers are the whole §10 row — for
    CREATE the sender's contract **and** the positive-only reconcile path — and the residual-risk
    acceptance of §6.1 is a layer of each stage. The residual-risk acceptance reaches the summary
    only through these stage verdicts; the summary's own ``CREATE_ADOPTED`` and
    ``RECONCILE_PATH_ADOPTED`` lines read the adapter's adoption map and never stand in for a stage
    layer. Fail closed: an absent Intent, preparation or grant is not ``READY``.
    """

    def __init__(
        self,
        *,
        stack: SafetyStack,
        assets: AssetReadinessSource,
        live: LiveAuthorityStore,
        registrations: RegistrationStore,
        preparations: ExecutionCopySource,
        create_sender_available: Callable[[], bool],
        reconcile_path_adopted: Callable[[], bool],
        endpoint_group: str,
    ) -> None:
        self._stack = stack
        self._assets = assets
        self._live = live
        self._registrations = registrations
        self._preparations = preparations
        self._create_sender_available = create_sender_available
        self._reconcile_path_adopted = reconcile_path_adopted
        self._endpoint_group = endpoint_group

    def create_ready(self, intent_id: str) -> bool:
        """``CREATE_MUTATION_READY`` for the attempt this Intent would open next (§3.2, G3-27)."""
        intent = self._registrations.intent(intent_id)
        if intent is None:
            return False
        attempts = self._registrations.attempts(intent_id)
        readiness = self._stack.create_readiness(
            intent,
            # A grant binds an exact attempt number, so the readiness is judged on the attempt the
            # next send would actually open — never on a past one.
            attempt_no=max((attempt.attempt_no for attempt in attempts), default=0) + 1,
            endpoint_adopted=self._create_sender_available(),
            reconcile_path_adopted=self._reconcile_path_adopted(),
            scope=self._registrations.execution_scope(
                intent.marketplace_key, intent.marketplace_account_id, self._endpoint_group
            ),
            stage_gate=create_stage_gate(
                registrations=self._registrations,
                preparations=self._preparations,
                intent=intent,
            ),
        )
        return readiness.verdict is Verdict.READY

    def asset_ready(
        self, marketplace_key: str, marketplace_account_id: str, preparation_id: str
    ) -> bool:
        """``ASSET_MUTATION_READY`` of this unit's current preparation revision (§3.1, §10).

        An ASSET mutation is authorized per grant, so the stage is ready only when a grant of this
        account names exactly the preparation revision that is current now and its own readiness is
        ``READY``. A unit with no such grant has no ready ASSET stage — the grant layer is one of
        the stage's own requirements, and row absence never satisfies it.
        """
        preparation = self._registrations.preparation(preparation_id)
        if preparation is None:
            return False
        revision_id = preparation.current.preparation_revision_id
        with self._live.reading() as unit:
            grants = unit.grants(MutationStage.ASSET, marketplace_key, marketplace_account_id)
        return any(
            grant.preparation_revision_id == revision_id
            and self._assets.readiness(grant.grant_id).verdict is Verdict.READY
            for grant in grants
        )
