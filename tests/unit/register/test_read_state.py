"""ADR-0014 §28.5 (M5-35, M5-36): the one server-side partition of the durable per-Intent state."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from app.platform.core.errors import AppError
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.model import IntentState, VerificationState
from app.stages.register.read_state import (
    READ_STATE_LABELS,
    UNCLASSIFIED,
    VERIFICATION_DEADLINE,
    IntentReadFacts,
    ReadState,
    classify,
    counts,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
WITHIN = NOW - VERIFICATION_DEADLINE
PAST = NOW - VERIFICATION_DEADLINE - timedelta(seconds=1)

# Every durable state the `_STATE_AGREES` CHECK of `registration_intents` admits, with the owner
# facts beside it, and the one label ADR-0014 §28.5 gives it.
VALID: list[tuple[IntentReadFacts, ReadState]] = [
    (
        IntentReadFacts(
            IntentState.CONFIRMED,
            RemoteOutcome.APPLIED_PROVEN,
            VerificationState.PASS,
            attempted=True,
        ),
        ReadState.REGISTERED,
    ),
    (
        IntentReadFacts(
            IntentState.UNKNOWN,
            RemoteOutcome.UNKNOWN,
            VerificationState.NOT_VERIFIED,
            attempted=True,
        ),
        ReadState.RECHECK_REQUIRED,
    ),
    (
        IntentReadFacts(
            IntentState.SENT,
            RemoteOutcome.APPLIED_PROVEN,
            VerificationState.MISMATCH,
            applied_at=NOW,
            attempted=True,
        ),
        ReadState.RECHECK_REQUIRED,
    ),
    (
        IntentReadFacts(
            IntentState.SENT,
            RemoteOutcome.APPLIED_PROVEN,
            VerificationState.NOT_VERIFIED,
            applied_at=WITHIN,
            attempted=True,
        ),
        ReadState.REGISTERING,
    ),
    (
        IntentReadFacts(
            IntentState.SENT,
            RemoteOutcome.APPLIED_PROVEN,
            VerificationState.NOT_VERIFIED,
            applied_at=PAST,
            attempted=True,
        ),
        ReadState.RECHECK_REQUIRED,
    ),
    (
        IntentReadFacts(
            IntentState.SENT,
            None,
            VerificationState.NOT_VERIFIED,
            attempt_in_flight=True,
            attempted=True,
        ),
        ReadState.REGISTERING,
    ),
    (
        IntentReadFacts(IntentState.PREPARED, None, VerificationState.NOT_VERIFIED),
        ReadState.REGISTERING,
    ),
    (
        IntentReadFacts(
            IntentState.PREPARED, None, VerificationState.NOT_VERIFIED, create_job_live=True
        ),
        ReadState.REGISTERING,
    ),
    (
        IntentReadFacts(
            IntentState.PREPARED, None, VerificationState.NOT_VERIFIED, create_job_dead=True
        ),
        ReadState.FAILED,
    ),
    (
        IntentReadFacts(
            IntentState.FAILED,
            RemoteOutcome.NOT_APPLIED_PROVEN,
            VerificationState.NOT_VERIFIED,
            create_job_live=True,
            attempted=True,
        ),
        ReadState.REGISTERING,
    ),
    (
        IntentReadFacts(
            IntentState.FAILED,
            RemoteOutcome.NOT_APPLIED_PROVEN,
            VerificationState.NOT_VERIFIED,
            attempted=True,
        ),
        ReadState.FAILED,
    ),
    (
        IntentReadFacts(
            IntentState.FAILED,
            RemoteOutcome.NOT_APPLIED_PROVEN,
            VerificationState.NOT_VERIFIED,
            create_job_dead=True,
            attempted=True,
        ),
        ReadState.FAILED,
    ),
]


@pytest.mark.parametrize(("facts", "expected"), VALID)
def test_every_valid_durable_state_has_exactly_its_one_label(
    facts: IntentReadFacts, expected: ReadState
) -> None:
    verdict = classify(facts, now=NOW)
    assert verdict.state is expected
    assert verdict.label == READ_STATE_LABELS[expected]
    assert verdict.reason_code.startswith("REGISTER_READ_")


def test_the_labels_are_exactly_the_four_of_adr_0014_28_5() -> None:
    assert [READ_STATE_LABELS[state] for state in ReadState] == [
        "등록중",
        "등록성공",
        "재확인필요",
        "등록실패",
    ]


def test_the_partition_is_total_and_disjoint_over_every_combination() -> None:
    """Every combination of the durable vocabularies is either exactly one label or refused —
    never two labels, never a default."""
    labelled = 0
    for state, outcome, verification, flight, applied, live, dead, attempted in itertools.product(
        IntentState,
        (None, *RemoteOutcome),
        VerificationState,
        (False, True),
        (None, WITHIN, PAST),
        (False, True),
        (False, True),
        (False, True),
    ):
        facts = IntentReadFacts(
            state, outcome, verification, flight, applied, live, dead, attempted
        )
        try:
            verdict = classify(facts, now=NOW)
        except AppError as refused:
            assert refused.code == UNCLASSIFIED
            continue
        labelled += 1
        assert verdict.state in ReadState
    assert labelled > 0


def test_an_unknown_or_ambiguous_outcome_is_never_a_failure() -> None:
    """M5-36: a timeout, a lost response or an unknown outcome is never 등록실패."""
    for state, outcome, verification, flight, applied, live, dead in itertools.product(
        (IntentState.UNKNOWN, IntentState.SENT),
        (None, RemoteOutcome.UNKNOWN, RemoteOutcome.APPLIED_PROVEN),
        VerificationState,
        (False, True),
        (None, WITHIN, PAST),
        (False, True),
        (False, True),
    ):
        facts = IntentReadFacts(state, outcome, verification, flight, applied, live, dead, True)
        try:
            verdict = classify(facts, now=NOW)
        except AppError:
            continue
        assert verdict.state is not ReadState.FAILED


def test_a_failure_with_an_automatic_retry_scheduled_is_not_shown_as_failed() -> None:
    retrying = IntentReadFacts(
        IntentState.FAILED,
        RemoteOutcome.NOT_APPLIED_PROVEN,
        VerificationState.NOT_VERIFIED,
        create_job_live=True,
        attempted=True,
    )
    assert classify(retrying, now=NOW).state is ReadState.REGISTERING


def test_the_verification_deadline_is_a_boundary_of_the_server_policy() -> None:
    applied = IntentReadFacts(
        IntentState.SENT,
        RemoteOutcome.APPLIED_PROVEN,
        VerificationState.NOT_VERIFIED,
        applied_at=NOW,
        attempted=True,
    )
    assert classify(applied, now=NOW + VERIFICATION_DEADLINE).state is ReadState.REGISTERING
    late = NOW + VERIFICATION_DEADLINE + timedelta(microseconds=1)
    assert classify(applied, now=late).state is ReadState.RECHECK_REQUIRED


@pytest.mark.parametrize(
    "facts",
    [
        # An applied CREATE whose applied time no owner recorded cannot be placed in time.
        IntentReadFacts(
            IntentState.SENT, RemoteOutcome.APPLIED_PROVEN, VerificationState.NOT_VERIFIED
        ),
        # SENT with no outcome and no Attempt open is not a state any owner leaves.
        IntentReadFacts(IntentState.SENT, None, VerificationState.NOT_VERIFIED),
        # A PREPARED Intent with an Attempt was sent; the store never writes it.
        IntentReadFacts(IntentState.PREPARED, None, VerificationState.NOT_VERIFIED, attempted=True),
        IntentReadFacts(
            IntentState.CONFIRMED, RemoteOutcome.APPLIED_PROVEN, VerificationState.NOT_VERIFIED
        ),
        IntentReadFacts(IntentState.FAILED, RemoteOutcome.UNKNOWN, VerificationState.NOT_VERIFIED),
    ],
)
def test_an_unmatched_durable_state_is_refused_never_labelled(facts: IntentReadFacts) -> None:
    with pytest.raises(AppError) as refused:
        classify(facts, now=NOW)
    assert refused.value.code == UNCLASSIFIED


def test_the_four_counts_always_sum_to_the_intents_counted() -> None:
    states = [expected for _facts, expected in VALID]
    found = counts(states)
    assert list(found) == list(ReadState)
    assert sum(found.values()) == len(states)
    assert counts([]) == dict.fromkeys(ReadState, 0)


def test_boundary_transitions_never_double_count_or_drop_an_intent() -> None:
    """Dispatch, outcome, deadline and reconcile each move one Intent from one label to one other;
    the batch total is unchanged across each step."""
    path = [
        IntentReadFacts(IntentState.PREPARED, None, VerificationState.NOT_VERIFIED),
        IntentReadFacts(
            IntentState.SENT,
            None,
            VerificationState.NOT_VERIFIED,
            attempt_in_flight=True,
            attempted=True,
        ),
        IntentReadFacts(
            IntentState.UNKNOWN,
            RemoteOutcome.UNKNOWN,
            VerificationState.NOT_VERIFIED,
            attempted=True,
        ),
        IntentReadFacts(
            IntentState.SENT,
            RemoteOutcome.APPLIED_PROVEN,
            VerificationState.NOT_VERIFIED,
            applied_at=PAST,
            attempted=True,
        ),
        IntentReadFacts(
            IntentState.CONFIRMED,
            RemoteOutcome.APPLIED_PROVEN,
            VerificationState.PASS,
            attempted=True,
        ),
    ]
    sibling = IntentReadFacts(IntentState.PREPARED, None, VerificationState.NOT_VERIFIED)
    for step in path:
        batch = counts([classify(step, now=NOW).state, classify(sibling, now=NOW).state])
        assert sum(batch.values()) == 2
