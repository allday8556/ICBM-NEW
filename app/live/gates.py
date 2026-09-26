"""The CREATE stage's own gate, from the REGISTER owners (ADR-0018 §10; area 1 carry-forward).

``CREATE_MUTATION_READY`` may never report ``READY`` on the stack's layers alone: the stage's own
gate — the final preflight ``READY`` under current truth with the fingerprint the Snapshot froze, a
sendable Intent, and no unresolved conflict — is derived here from the owners and handed to the
readiness as :class:`~app.live.stack.StageGate`. Nothing is decided here that the owners do not
already decide; every reason is the owner's own code.
"""

from typing import Any, Protocol

from app.core.errors import AppError
from app.live.stack import StageGate
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
