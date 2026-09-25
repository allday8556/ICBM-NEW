"""The Phase C evidence rules, as pure functions (ADR-0017 §10.5, §11; carry-forward 5818794101)."""

import pytest

from app.collect.adaptive_shadow.evidence import (
    FAILURE,
    PRUNED,
    SUCCESS,
    UNRESOLVED,
    BundleVerdict,
    Cause,
    CountAs,
    Event,
    EventKind,
    LedgerInvalid,
    Resolution,
    RunVerdict,
    State,
    WindowEvidence,
    WindowVerdict,
    bundle_verdict,
    fold,
    only_after_recovery,
    outcome_state,
    permanently_blocked,
    resolution_state,
    window_verdict,
)

W = "w-1"


def outcome(verdict: RunVerdict | None, cause: Cause | None = None) -> Event:
    return Event(1, EventKind.OUTCOME_RECORDED, outcome_state(verdict, cause), verdict, window_id=W)


def resolution(seq: int, verdict: RunVerdict, answer: Resolution, closed: bool = False) -> Event:
    state = resolution_state(verdict, answer, adaptive_failed_closed=closed)
    return Event(seq, EventKind.RESOLUTION_RECORDED, state, None, answer, closed, window_id=W)


def pruned(seq: int) -> Event:
    return Event(seq, EventKind.RAW_PRUNED_UNRESOLVED, PRUNED, window_id=W)


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (RunVerdict.MATCH, SUCCESS),
        (RunVerdict.SHADOW_FAILED, FAILURE),
        (RunVerdict.IMAGE_UNMATCHABLE, State(CountAs.INCOMPLETE, Cause.IMAGE_UNMATCHABLE)),
        (RunVerdict.MISMATCH, UNRESOLVED),
        (RunVerdict.IDENTITY_MISMATCH, UNRESOLVED),
        (RunVerdict.TEMPLATE_UNMATCHED, UNRESOLVED),
        (RunVerdict.TEMPLATE_AMBIGUOUS, UNRESOLVED),
    ],
)
def test_what_a_recorded_outcome_counts_as(verdict: RunVerdict, expected: State) -> None:
    assert outcome_state(verdict) == expected


def test_a_missing_outcome_names_one_of_the_two_missing_causes() -> None:
    assert outcome_state(None, Cause.SHADOW_MISSING).cause is Cause.SHADOW_MISSING
    assert outcome_state(None, Cause.SHADOW_MISSING_AFTER_RECOVERY).terminal
    for cause in (None, Cause.UNRESOLVED_MISMATCH, Cause.IMAGE_UNMATCHABLE):
        with pytest.raises(ValueError):
            outcome_state(None, cause)


@pytest.mark.parametrize(
    ("verdict", "answer", "closed", "expected"),
    [
        (RunVerdict.MISMATCH, Resolution.ADAPTIVE_CORRECT, False, SUCCESS),
        (RunVerdict.MISMATCH, Resolution.CURRENT_CORRECT, False, FAILURE),
        (RunVerdict.MISMATCH, Resolution.BOTH_WRONG, False, FAILURE),
        (RunVerdict.MISMATCH, Resolution.SOURCE_AMBIGUOUS, True, SUCCESS),
        (RunVerdict.MISMATCH, Resolution.SOURCE_AMBIGUOUS, False, FAILURE),
        (RunVerdict.IDENTITY_MISMATCH, Resolution.ADAPTIVE_CORRECT, False, SUCCESS),
        (RunVerdict.TEMPLATE_UNMATCHED, Resolution.SOURCE_AMBIGUOUS, False, SUCCESS),
        (RunVerdict.TEMPLATE_UNMATCHED, Resolution.ADAPTIVE_CORRECT, True, FAILURE),
        (RunVerdict.TEMPLATE_AMBIGUOUS, Resolution.CURRENT_CORRECT, False, FAILURE),
    ],
)
def test_what_a_resolution_yields(
    verdict: RunVerdict, answer: Resolution, closed: bool, expected: State
) -> None:
    assert resolution_state(verdict, answer, adaptive_failed_closed=closed) == expected


@pytest.mark.parametrize(
    "verdict", [RunVerdict.MATCH, RunVerdict.SHADOW_FAILED, RunVerdict.IMAGE_UNMATCHABLE]
)
def test_a_settled_outcome_is_never_resolved(verdict: RunVerdict) -> None:
    with pytest.raises(ValueError):
        resolution_state(verdict, Resolution.ADAPTIVE_CORRECT, adaptive_failed_closed=True)


def test_the_fold_starts_at_the_outcome_and_only_an_unresolved_state_moves() -> None:
    assert fold([outcome(RunVerdict.MATCH)]) == SUCCESS
    assert fold([outcome(RunVerdict.MISMATCH)]) == UNRESOLVED
    assert fold([outcome(RunVerdict.MISMATCH), pruned(2)]) == PRUNED
    resolved = [
        outcome(RunVerdict.MISMATCH),
        resolution(2, RunVerdict.MISMATCH, Resolution.ADAPTIVE_CORRECT),
    ]
    assert fold(resolved) == SUCCESS


@pytest.mark.parametrize(
    "events",
    [
        [],  # a run in the ledger starts with its outcome
        [pruned(1)],
        [outcome(RunVerdict.MATCH), pruned(2)],  # nothing after a terminal state
        [
            outcome(RunVerdict.MISMATCH),
            pruned(2),
            resolution(3, RunVerdict.MISMATCH, Resolution.ADAPTIVE_CORRECT),  # after a prune
        ],
        [outcome(RunVerdict.MISMATCH), pruned(3)],  # a gap
        [outcome(RunVerdict.MISMATCH), Event(2, EventKind.OUTCOME_RECORDED, UNRESOLVED)],
        [Event(1, EventKind.OUTCOME_RECORDED, SUCCESS, RunVerdict.MISMATCH, window_id=W)],
        [
            outcome(RunVerdict.MISMATCH),
            Event(2, EventKind.RAW_PRUNED_UNRESOLVED, PRUNED, window_id="w-other"),
        ],
    ],
)
def test_a_stream_no_legal_history_produces_is_refused(events: list[Event]) -> None:
    with pytest.raises(LedgerInvalid):
        fold(events)


def test_a_window_passes_only_with_every_success_k_runs_and_a_restart() -> None:
    assert window_verdict([SUCCESS] * 3, min_size=3, restart_seen=True) is WindowVerdict.PASS
    assert window_verdict([SUCCESS] * 3, min_size=3, restart_seen=False) is WindowVerdict.INCOMPLETE
    assert window_verdict([SUCCESS] * 2, min_size=3, restart_seen=True) is WindowVerdict.INCOMPLETE
    assert window_verdict([SUCCESS, UNRESOLVED, SUCCESS], min_size=3, restart_seen=True) is (
        WindowVerdict.INCOMPLETE
    )
    assert window_verdict([SUCCESS, FAILURE], min_size=1, restart_seen=True) is WindowVerdict.FAIL


def _window(
    window_id: str,
    states: dict[str, State],
    verdict: WindowVerdict,
    *,
    closed: bool = True,
    superseded: bool = False,
) -> WindowEvidence:
    return WindowEvidence(window_id, states, verdict, closed, closed, superseded, 3)


AFTER = State(CountAs.INCOMPLETE, Cause.SHADOW_MISSING_AFTER_RECOVERY)
PASSED = _window("w-pass", {"r1": SUCCESS, "r2": SUCCESS, "r3": SUCCESS}, WindowVerdict.PASS)


def test_a_bundle_passes_only_with_a_closed_pass_window_and_nothing_else_against_it() -> None:
    assert bundle_verdict([PASSED]).verdict is BundleVerdict.PASS
    assert bundle_verdict([]).verdict is BundleVerdict.INCOMPLETE
    open_pass = _window("w-open", PASSED.states, WindowVerdict.PASS, closed=False)
    assert bundle_verdict([open_pass]).reasons == (
        "WINDOW_NOT_PASSED:w-open",
        "NO_CLOSED_PASS_WINDOW",
    )


@pytest.mark.parametrize(
    ("earlier", "expected"),
    [
        (_window("w-0", {"r0": UNRESOLVED}, WindowVerdict.INCOMPLETE, closed=False), "BLOCKED"),
        (_window("w-0", {"r0": PRUNED}, WindowVerdict.INCOMPLETE), "BLOCKED"),
        (
            _window(
                "w-0",
                {"r0": State(CountAs.INCOMPLETE, Cause.IMAGE_UNMATCHABLE)},
                WindowVerdict.INCOMPLETE,
            ),
            "BLOCKED",
        ),
        (
            _window(
                "w-0",
                {"r0": State(CountAs.INCOMPLETE, Cause.SHADOW_MISSING)},
                WindowVerdict.INCOMPLETE,
            ),
            "BLOCKED",
        ),
        (_window("w-0", {"r0": FAILURE}, WindowVerdict.FAIL), "FAIL"),
        (_window("w-0", {"r0": AFTER}, WindowVerdict.INCOMPLETE), "INCOMPLETE"),
    ],
)
def test_a_later_pass_window_never_hides_an_earlier_one(
    earlier: WindowEvidence, expected: str
) -> None:
    assert bundle_verdict([earlier, PASSED]).verdict is BundleVerdict(expected)


def test_a_recovery_only_window_counts_once_it_is_closed_and_superseded() -> None:
    recovered = _window("w-0", {"r0": AFTER, "r1": SUCCESS}, WindowVerdict.INCOMPLETE)
    assert only_after_recovery(recovered.states.values())
    assert bundle_verdict([recovered, PASSED]).reasons == ("RECOVERY_WINDOW_NOT_SUPERSEDED:w-0",)
    superseded = _window("w-0", recovered.states, WindowVerdict.INCOMPLETE, superseded=True)
    assert bundle_verdict([superseded, PASSED]).verdict is BundleVerdict.PASS
    assert not only_after_recovery([AFTER, UNRESOLVED])
    assert not only_after_recovery([SUCCESS])


def test_the_permanent_causes_and_a_failed_window_block_any_further_window() -> None:
    for cause in (Cause.PRUNED_BEFORE_RESOLUTION, Cause.IMAGE_UNMATCHABLE, Cause.SHADOW_MISSING):
        window = _window("w-0", {"r0": State(CountAs.INCOMPLETE, cause)}, WindowVerdict.INCOMPLETE)
        assert permanently_blocked([window]) == (f"{cause.value}:w-0:r0",)
    failed = _window("w-0", {"r0": FAILURE}, WindowVerdict.FAIL)
    assert permanently_blocked([failed]) == ("WINDOW_FAILED:w-0",)
    # The recoverable ones never block a fresh window of the same bundle.
    for state in (UNRESOLVED, AFTER):
        window = _window("w-0", {"r0": state}, WindowVerdict.INCOMPLETE)
        assert permanently_blocked([window]) == ()
