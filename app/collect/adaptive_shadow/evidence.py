"""Phase C evidence, as pure functions (ADR-0017 §10.5, §11; carry-forward `5818794101`).

Nothing here reads or writes. The store hands these functions the rows it holds and records what
they return, so every rule that decides what an eligible run counts as, and what a window or a
bundle may claim, has exactly one implementation.

**Effective state.** A run's effective state is a pure fold over its ledger events, in sequence:
it starts from ``OUTCOME_RECORDED``; a ``RESOLUTION_RECORDED`` replaces ``UNRESOLVED_MISMATCH``
with what the resolution yields; a ``RAW_PRUNED_UNRESOLVED`` replaces it with ``INCOMPLETE``,
cause ``PRUNED_BEFORE_RESOLUTION``. ``UNRESOLVED_MISMATCH`` is the only non-terminal state, and no
event ever follows a terminal one.

**Exact bundles (carry-forward item 1).** An EPR is content-addressed: a content-identical EPR is
the same digest, the same bundle and the same evidence, so no nonce, counter or re-save can make
a new bundle out of it. ``PRUNED_BEFORE_RESOLUTION``, ``IMAGE_UNMATCHABLE`` and ``SHADOW_MISSING``
therefore block that exact bundle for ever, and a ``FAIL`` window disqualifies it: no further
window is declared for it. Only a genuinely content-different EPR — a different digest — starts
fresh evidence. ``SHADOW_MISSING_AFTER_RECOVERY`` is the one cause a same-bundle window may be
superseded for (§11.2), and ``UNRESOLVED_MISMATCH`` clears only by a recorded resolution.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class CountAs(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    INCOMPLETE = "INCOMPLETE"


class Cause(StrEnum):
    UNRESOLVED_MISMATCH = "UNRESOLVED_MISMATCH"
    PRUNED_BEFORE_RESOLUTION = "PRUNED_BEFORE_RESOLUTION"
    IMAGE_UNMATCHABLE = "IMAGE_UNMATCHABLE"
    SHADOW_MISSING = "SHADOW_MISSING"
    SHADOW_MISSING_AFTER_RECOVERY = "SHADOW_MISSING_AFTER_RECOVERY"


class RunVerdict(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    TEMPLATE_UNMATCHED = "TEMPLATE_UNMATCHED"
    TEMPLATE_AMBIGUOUS = "TEMPLATE_AMBIGUOUS"
    IMAGE_UNMATCHABLE = "IMAGE_UNMATCHABLE"
    SHADOW_FAILED = "SHADOW_FAILED"


class Resolution(StrEnum):
    CURRENT_CORRECT = "CURRENT_CORRECT"
    ADAPTIVE_CORRECT = "ADAPTIVE_CORRECT"
    BOTH_WRONG = "BOTH_WRONG"
    SOURCE_AMBIGUOUS = "SOURCE_AMBIGUOUS"


class EventKind(StrEnum):
    OUTCOME_RECORDED = "OUTCOME_RECORDED"
    RESOLUTION_RECORDED = "RESOLUTION_RECORDED"
    RAW_PRUNED_UNRESOLVED = "RAW_PRUNED_UNRESOLVED"


class WindowVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


class BundleVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCOMPLETE = "INCOMPLETE"


# §11.3: the causes that keep a bundle from passing while any run holds one.
BLOCKING = frozenset(
    {
        Cause.UNRESOLVED_MISMATCH,
        Cause.PRUNED_BEFORE_RESOLUTION,
        Cause.IMAGE_UNMATCHABLE,
        Cause.SHADOW_MISSING,
    }
)
# Carry-forward `5818794101` item 1: the blocking causes nothing can ever clear for their bundle.
PERMANENT = BLOCKING - {Cause.UNRESOLVED_MISMATCH}
# The verdicts a human resolves against the source; the rest settle on their own.
_TEMPLATE = frozenset({RunVerdict.TEMPLATE_UNMATCHED, RunVerdict.TEMPLATE_AMBIGUOUS})
_RESOLVABLE = _TEMPLATE | {RunVerdict.MISMATCH, RunVerdict.IDENTITY_MISMATCH}


class LedgerInvalid(ValueError):
    """A ledger stream that no legal sequence of events produces."""


@dataclass(frozen=True)
class State:
    count_as: CountAs
    cause: Cause | None = None

    def __post_init__(self) -> None:
        if (self.count_as is CountAs.INCOMPLETE) != (self.cause is not None):
            raise ValueError("an INCOMPLETE state carries exactly one cause, and only it")

    @property
    def terminal(self) -> bool:
        return self.cause is not Cause.UNRESOLVED_MISMATCH

    @property
    def blocking(self) -> bool:
        return self.cause in BLOCKING


SUCCESS = State(CountAs.SUCCESS)
FAILURE = State(CountAs.FAILURE)
UNRESOLVED = State(CountAs.INCOMPLETE, Cause.UNRESOLVED_MISMATCH)
PRUNED = State(CountAs.INCOMPLETE, Cause.PRUNED_BEFORE_RESOLUTION)


def outcome_state(verdict: RunVerdict | None, missing: Cause | None = None) -> State:
    """What an eligible run counts as when its outcome is first recorded (§11.1 table)."""
    if verdict is None:
        if missing not in (Cause.SHADOW_MISSING, Cause.SHADOW_MISSING_AFTER_RECOVERY):
            raise ValueError("a missing shadow outcome names why it is missing")
        return State(CountAs.INCOMPLETE, missing)
    if verdict is RunVerdict.MATCH:
        return SUCCESS
    if verdict is RunVerdict.SHADOW_FAILED:
        return FAILURE
    if verdict is RunVerdict.IMAGE_UNMATCHABLE:
        return State(CountAs.INCOMPLETE, Cause.IMAGE_UNMATCHABLE)
    return UNRESOLVED


def resolution_state(
    verdict: RunVerdict, resolution: Resolution, *, adaptive_failed_closed: bool
) -> State:
    """What a human resolution of an unresolved run yields (§11.1 table)."""
    if verdict not in _RESOLVABLE:
        raise ValueError(f"a {verdict.value} outcome is never resolved")
    if verdict in _TEMPLATE:
        return SUCCESS if resolution is Resolution.SOURCE_AMBIGUOUS else FAILURE
    if resolution is Resolution.ADAPTIVE_CORRECT:
        return SUCCESS
    if resolution is Resolution.SOURCE_AMBIGUOUS:
        return SUCCESS if adaptive_failed_closed else FAILURE
    return FAILURE


@dataclass(frozen=True)
class Event:
    seq: int
    kind: EventKind
    state: State
    verdict: RunVerdict | None = None
    resolution: Resolution | None = None
    adaptive_failed_closed: bool | None = None
    window_id: str | None = None
    process_run_id: str | None = None
    revision_id: str | None = None


def fold(events: Sequence[Event]) -> State:
    """The run's effective state, recomputed from its events alone; every stored state must agree
    with the one the fold derives, or the stream is refused."""
    if not events:
        raise LedgerInvalid("a run in the ledger starts with its OUTCOME_RECORDED")
    state: State | None = None
    outcome: Event | None = None
    for position, event in enumerate(events, start=1):
        if event.seq != position:
            raise LedgerInvalid("ledger events follow one another without a gap")
        if outcome is not None and event.window_id != outcome.window_id:
            raise LedgerInvalid("every event of a run belongs to one window")
        if event.kind is EventKind.OUTCOME_RECORDED:
            if position != 1:
                raise LedgerInvalid("OUTCOME_RECORDED is a run's first event and only its first")
            missing = event.state.cause if event.verdict is None else None
            derived = outcome_state(event.verdict, missing)
            outcome = event
        else:
            if state is None or outcome is None or state.terminal:
                raise LedgerInvalid("no event follows a terminal state")
            if event.kind is EventKind.RAW_PRUNED_UNRESOLVED:
                derived = PRUNED
            else:
                if event.resolution is None or event.adaptive_failed_closed is None:
                    raise LedgerInvalid("a resolution names what it resolved to")
                if outcome.verdict is None:
                    raise LedgerInvalid("only a recorded comparison is ever resolved")
                derived = resolution_state(
                    outcome.verdict,
                    event.resolution,
                    adaptive_failed_closed=event.adaptive_failed_closed,
                )
        if derived != event.state:
            raise LedgerInvalid("a stored state disagrees with the fold of its events")
        state = derived
    assert state is not None
    return state


# ---------------------------------------------------------------- windows and bundles


def window_verdict(states: Iterable[State], *, min_size: int, restart_seen: bool) -> WindowVerdict:
    """§11.3: PASS only when every eligible run succeeded, there are at least K of them and one came
    after a process restart; FAIL when any failed; INCOMPLETE otherwise."""
    held = list(states)
    if any(state.count_as is CountAs.FAILURE for state in held):
        return WindowVerdict.FAIL
    if (
        len(held) >= min_size
        and restart_seen
        and all(state.count_as is CountAs.SUCCESS for state in held)
    ):
        return WindowVerdict.PASS
    return WindowVerdict.INCOMPLETE


def only_after_recovery(states: Iterable[State]) -> bool:
    """True when every state that is not a success is ``SHADOW_MISSING_AFTER_RECOVERY`` and at
    least one is: the one case a same-bundle window may be superseded for (§11.2)."""
    held = [state for state in states if state.count_as is not CountAs.SUCCESS]
    return bool(held) and all(state.cause is Cause.SHADOW_MISSING_AFTER_RECOVERY for state in held)


@dataclass(frozen=True)
class WindowEvidence:
    """One window of a bundle as its verdicts read it: effective states only (§10.5)."""

    window_id: str
    states: Mapping[str, State]
    verdict: WindowVerdict
    ended: bool
    closed: bool
    superseded: bool
    min_size: int


@dataclass(frozen=True)
class BundleEvidence:
    verdict: BundleVerdict
    reasons: tuple[str, ...]


def bundle_verdict(windows: Sequence[WindowEvidence]) -> BundleEvidence:
    """§11.3, over **every** window ever declared for the bundle: a later PASS never hides an
    earlier window."""
    reasons: list[str] = []
    if not windows:
        return BundleEvidence(BundleVerdict.INCOMPLETE, ("NO_WINDOW",))
    if failed := [w.window_id for w in windows if w.verdict is WindowVerdict.FAIL]:
        return BundleEvidence(BundleVerdict.FAIL, tuple(f"WINDOW_FAILED:{w}" for w in failed))
    blocked = sorted(
        {
            f"{state.cause.value}:{window.window_id}:{run_id}"
            for window in windows
            for run_id, state in window.states.items()
            if state.blocking and state.cause is not None
        }
    )
    if blocked:
        return BundleEvidence(BundleVerdict.BLOCKED, tuple(blocked))
    for window in windows:
        if window.verdict is WindowVerdict.PASS and window.closed:
            continue
        if only_after_recovery(window.states.values()):
            if not (window.closed and window.superseded):
                reasons.append(f"RECOVERY_WINDOW_NOT_SUPERSEDED:{window.window_id}")
            continue
        if not window.ended and len(window.states) < window.min_size:
            reasons.append(f"WINDOW_OPEN_BELOW_K:{window.window_id}")
            continue
        reasons.append(f"WINDOW_NOT_PASSED:{window.window_id}")
    # A window's verdict is final only at its closeout, so only a closed PASS window counts.
    if not any(w.verdict is WindowVerdict.PASS and w.closed for w in windows):
        reasons.append("NO_CLOSED_PASS_WINDOW")
    if reasons:
        return BundleEvidence(BundleVerdict.INCOMPLETE, tuple(reasons))
    return BundleEvidence(BundleVerdict.PASS, ())


def permanently_blocked(windows: Sequence[WindowEvidence]) -> tuple[str, ...]:
    """Why no further window may ever be declared for this exact bundle: a permanent blocking
    cause in any of its runs, or a FAIL window. Empty when a new window may be declared."""
    found = [
        f"{state.cause.value}:{window.window_id}:{run_id}"
        for window in windows
        for run_id, state in window.states.items()
        if state.cause in PERMANENT and state.cause is not None
    ]
    found += [f"WINDOW_FAILED:{w.window_id}" for w in windows if w.verdict is WindowVerdict.FAIL]
    return tuple(sorted(found))
