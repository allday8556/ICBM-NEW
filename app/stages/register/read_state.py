"""The user-facing registration read state of ADR-0014 §28.5 (M5-35, M5-36).

The operator sees four registration states. They are **one server-side partition** of the durable
per-Intent state: total (no valid durable state is unclassified), disjoint (no state has two
labels) and derived on read — never stored as a second truth, like ``PARTIAL`` (§12). This module
is that one partition. Every screen, card, panel and counter reads it; no client recalculates it.

Pure: no database, no clock and no I/O. The caller reads the owners' durable facts — the Intent,
its Attempts, the CREATE job and the reconcile checks — and passes the current time; nothing here
decides an outcome, and an unmatched durable state is refused (``REGISTER_READ_STATE_UNCLASSIFIED``)
and surfaced, never defaulted to a label.

**Only failure is red, and only proven failure is failure** (§22, §28.5): 등록실패 is a
machine-proven non-application or a definitive rejection with no automatic retry scheduled, or a
pre-send failure that transmitted nothing. A timeout, a lost response, an unknown outcome and a
zero-result search are never 등록실패.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from app.platform.core.errors import AppError
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.model import IntentState, VerificationState

# Versioned with the partition: a change to a row or to the deadline is a new version.
READ_STATE_PARTITION_VERSION: Final = "registration-read-state/v1"
# §28.5: how long an applied, not yet verified CREATE stays 등록중 before it needs a re-check. A
# server policy value of this partition version, never UI logic.
VERIFICATION_DEADLINE: Final = timedelta(minutes=30)

UNCLASSIFIED: Final = "REGISTER_READ_STATE_UNCLASSIFIED"


class ReadState(StrEnum):
    """ADR-0014 §28.5: the four states the operator sees."""

    REGISTERING = "REGISTERING"
    REGISTERED = "REGISTERED"
    RECHECK_REQUIRED = "RECHECK_REQUIRED"
    FAILED = "FAILED"


# The operator-facing label of each state, exactly as ADR-0014 §28.5 names it.
READ_STATE_LABELS: Final[Mapping[ReadState, str]] = {
    ReadState.REGISTERING: "등록중",
    ReadState.REGISTERED: "등록성공",
    ReadState.RECHECK_REQUIRED: "재확인필요",
    ReadState.FAILED: "등록실패",
}

# Why a unit is in its state, each read from an owner's own fact. A provider or job cause, when one
# exists, is shown instead (``reason_code``).
REASON_QUEUED: Final = "REGISTER_READ_QUEUED"
REASON_SENDABLE: Final = "REGISTER_READ_SENDABLE"
REASON_IN_FLIGHT: Final = "REGISTER_READ_SEND_IN_FLIGHT"
REASON_AWAITING_VERIFICATION: Final = "REGISTER_READ_AWAITING_VERIFICATION"
REASON_RETRY_SCHEDULED: Final = "REGISTER_READ_RETRY_SCHEDULED"
REASON_VERIFIED: Final = "REGISTER_READ_VERIFIED"
REASON_OUTCOME_UNKNOWN: Final = "REGISTER_READ_OUTCOME_UNKNOWN"
REASON_READBACK_MISMATCH: Final = "REGISTER_READ_READBACK_MISMATCH"
REASON_VERIFICATION_OVERDUE: Final = "REGISTER_READ_VERIFICATION_OVERDUE"
REASON_NOT_APPLIED: Final = "REGISTER_READ_NOT_APPLIED"
REASON_PRE_SEND_FAILED: Final = "REGISTER_READ_PRE_SEND_FAILED"


@dataclass(frozen=True)
class IntentReadFacts:
    """The durable facts one Intent's read state is derived from, each read from its owner.

    - ``attempt_in_flight``: an Attempt is open (started, not finished) — the store's own row.
    - ``applied_at``: when the applied outcome was established (the applied Attempt finished, or
      its evidence-backed resolution was recorded); the verification deadline runs from it.
    - ``create_job_live``: the CREATE job of this Intent is queued, running or scheduled for an
      automatic retry under §9 (the job owner's active states).
    - ``create_job_dead``: the latest CREATE job of this Intent ended without a retry.
    """

    state: IntentState
    remote_outcome: RemoteOutcome | None
    verification_state: VerificationState
    attempt_in_flight: bool = False
    applied_at: datetime | None = None
    create_job_live: bool = False
    create_job_dead: bool = False
    attempted: bool = False


@dataclass(frozen=True)
class ReadVerdict:
    state: ReadState
    reason_code: str

    @property
    def label(self) -> str:
        return READ_STATE_LABELS[self.state]


def _unclassified(facts: IntentReadFacts) -> AppError:
    return AppError(
        UNCLASSIFIED,
        "this durable registration state has no read state; it is a defect, never a label",
        details={
            "state": facts.state.value,
            "remote_outcome": None if facts.remote_outcome is None else facts.remote_outcome.value,
            "verification_state": facts.verification_state.value,
        },
    )


def classify(facts: IntentReadFacts, *, now: datetime) -> ReadVerdict:
    """The one read state of one Intent (ADR-0014 §28.5), or a refusal for an unmatched state.

    ```text
    CONFIRMED                                                     등록성공
    UNKNOWN, unresolved                                           재확인필요
    SENT + APPLIED_PROVEN + MISMATCH                              재확인필요
    SENT + APPLIED_PROVEN + NOT_VERIFIED, within the deadline     등록중
    SENT + APPLIED_PROVEN + NOT_VERIFIED, past the deadline       재확인필요
    SENT with an Attempt in flight and no outcome                 등록중
    PREPARED, queued or sendable                                  등록중
    FAILED with an automatic retry scheduled (§9)                 등록중
    PREPARED, nothing transmitted, terminal pre-send failure      등록실패
    FAILED with no automatic retry scheduled                      등록실패
    ```
    """
    state, outcome, verification = facts.state, facts.remote_outcome, facts.verification_state
    if state is IntentState.CONFIRMED:
        if outcome is RemoteOutcome.APPLIED_PROVEN and verification is VerificationState.PASS:
            return ReadVerdict(ReadState.REGISTERED, REASON_VERIFIED)
        raise _unclassified(facts)
    if verification is VerificationState.PASS:
        raise _unclassified(facts)
    if state is IntentState.UNKNOWN:
        if outcome is RemoteOutcome.UNKNOWN and verification is VerificationState.NOT_VERIFIED:
            return ReadVerdict(ReadState.RECHECK_REQUIRED, REASON_OUTCOME_UNKNOWN)
        raise _unclassified(facts)
    if state is IntentState.SENT:
        if outcome is RemoteOutcome.APPLIED_PROVEN:
            if verification is VerificationState.MISMATCH:
                return ReadVerdict(ReadState.RECHECK_REQUIRED, REASON_READBACK_MISMATCH)
            if facts.applied_at is None:
                raise _unclassified(facts)
            if now - facts.applied_at <= VERIFICATION_DEADLINE:
                return ReadVerdict(ReadState.REGISTERING, REASON_AWAITING_VERIFICATION)
            return ReadVerdict(ReadState.RECHECK_REQUIRED, REASON_VERIFICATION_OVERDUE)
        if (
            outcome is None
            and verification is VerificationState.NOT_VERIFIED
            and facts.attempt_in_flight
        ):
            return ReadVerdict(ReadState.REGISTERING, REASON_IN_FLIGHT)
        raise _unclassified(facts)
    if verification is not VerificationState.NOT_VERIFIED:
        raise _unclassified(facts)
    if state is IntentState.PREPARED:
        if outcome is not None or facts.attempted:
            raise _unclassified(facts)
        if facts.create_job_live:
            return ReadVerdict(ReadState.REGISTERING, REASON_QUEUED)
        if facts.create_job_dead:
            return ReadVerdict(ReadState.FAILED, REASON_PRE_SEND_FAILED)
        return ReadVerdict(ReadState.REGISTERING, REASON_SENDABLE)
    if state is IntentState.FAILED and outcome is RemoteOutcome.NOT_APPLIED_PROVEN:
        if facts.create_job_live:
            return ReadVerdict(ReadState.REGISTERING, REASON_RETRY_SCHEDULED)
        return ReadVerdict(ReadState.FAILED, REASON_NOT_APPLIED)
    raise _unclassified(facts)


def counts(states: Iterable[ReadState]) -> dict[ReadState, int]:
    """Every read state's count, zeros included: the four always sum to the Intents counted."""
    found = Counter(states)
    return {state: found.get(state, 0) for state in ReadState}


__all__ = [
    "READ_STATE_LABELS",
    "READ_STATE_PARTITION_VERSION",
    "UNCLASSIFIED",
    "VERIFICATION_DEADLINE",
    "IntentReadFacts",
    "ReadState",
    "ReadVerdict",
    "classify",
    "counts",
]
