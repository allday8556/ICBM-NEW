"""B-UX2 (ADR-0016 §1, ADR-0014 §28.5–§28.6): an applied CREATE whose verification is overdue is a
REGISTRATION_ERROR condition of the REGISTER execution producer, decided by the same deadline rule
as its 재확인필요 read state, with time-driven coverage. No provider is contacted.

- within the deadline (the boundary included) it is pending work: no item;
- one second past it the producer's truth token moves, so the count is never CURRENT while the
  item is missing; a pass then indexes ``verification`` / ``REGISTER_READ_VERIFICATION_OVERDUE``
  on the Intent's scope, sourced by the applied Attempt;
- once the owner moves the Intent on, reconciliation alone closes the item.
"""

from datetime import timedelta

import pytest

from app.capabilities.review.model import CountState, ReviewKind, ReviewState
from app.capabilities.review.register_producer import (
    REGISTER_PRODUCER,
    REGISTER_VERIFICATION_OVERDUE,
)
from app.config import AppConfig
from app.container import Container
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.read_state import (
    REASON_VERIFICATION_OVERDUE,
    VERIFICATION_DEADLINE,
)
from tests.integration.review.test_g2c_review_counts import (
    CID,
    MARKET,
    OPERATOR,
    counts,
    coverage_of,
    sent_intent,
)
from tests.support.jobs_support import FakeClock
from tests.support.register_support import establish

pytestmark = pytest.mark.integration


def overdue_items(container: Container) -> list[object]:
    return [
        item
        for item in container.review_items.items(producer=REGISTER_PRODUCER)
        if item.reason_code == REGISTER_VERIFICATION_OVERDUE
    ]


def test_an_overdue_verification_is_indexed_on_time_and_closed_by_reconciliation(
    container: Container, config: AppConfig, clock: FakeClock
) -> None:
    account = establish(container, config, MARKET, "uid-b-ux2-overdue")
    intent, attempt, draft_id = sent_intent(
        container, config, account, RemoteOutcome.APPLIED_PROVEN
    )
    container.review_reconciler.full_passes()
    assert overdue_items(container) == []
    # Exactly at the deadline it is still pending work: a pass there indexes nothing.
    clock.advance(VERIFICATION_DEADLINE.total_seconds())
    container.review_reconciler.full_passes()
    assert overdue_items(container) == []
    assert coverage_of(container, REGISTER_PRODUCER).current is True
    assert counts(container)[ReviewKind.REGISTRATION_ERROR].state is CountState.CURRENT
    # One second later the token moves: the count is not CURRENT while the item is missing.
    clock.advance(1)
    moved = coverage_of(container, REGISTER_PRODUCER)
    assert (moved.current, moved.reason) == (False, "REVIEW_OWNER_MOVED_SINCE_PASS")
    registration = counts(container)[ReviewKind.REGISTRATION_ERROR]
    assert (registration.state, registration.open) == (CountState.NOT_CURRENT, None)
    container.review_reconciler.full_passes()
    (item,) = overdue_items(container)
    assert item.kind is ReviewKind.REGISTRATION_ERROR  # type: ignore[attr-defined]
    assert item.subject == "verification"  # type: ignore[attr-defined]
    assert item.source_identity == attempt  # type: ignore[attr-defined]
    assert dict(item.scope) == {  # type: ignore[attr-defined]
        "draft_id": draft_id,
        "intent_id": intent,
        "marketplace_account_id": account,
        "marketplace_key": MARKET,
    }
    assert counts(container)[ReviewKind.REGISTRATION_ERROR].state is CountState.CURRENT
    # The same rule decides the read state: the same code, 재확인필요.
    (entry,) = [
        e for e in container.register.registration_status().entries if e.intent_id == intent
    ]
    assert entry.read_state is not None
    assert (entry.read_state.state.value, entry.read_state.reason_code) == (
        "RECHECK_REQUIRED",
        REASON_VERIFICATION_OVERDUE,
    )
    # The fix-only projection offers the read-only re-check, by the item's own identity.
    (row,) = [r for r in container.register_fixes.fixes().fixes if r.source == "REVIEW"]
    assert (row.code, row.intent_id, row.review_item_id) == (
        REGISTER_VERIFICATION_OVERDUE,
        intent,
        item.review_item_id,  # type: ignore[attr-defined]
    )
    assert (row.actionability, row.surface) == ("RECHECK", "REGISTER_ACTION")
    # The owner moves the Intent on (a read-back mismatch here): only reconciliation closes it.
    with container.registrations.transaction() as unit:
        unit.record_mismatch(
            intent,
            comparison_contract_version="c-1",
            normalizer_version="n-1",
            sanitized_comparison={"verdict": "MISMATCH", "field": "name"},
            actor=OPERATOR,
            correlation_id=CID,
        )
    container.review_reconciler.full_passes()
    closed = container.review_items.item(item.review_item_id)  # type: ignore[attr-defined]
    assert closed.state is ReviewState.RESOLVED
    assert {i.reason_code for i in container.review_items.items(producer=REGISTER_PRODUCER)} >= {
        "REGISTER_READBACK_MISMATCH"
    }


def test_the_deadline_rule_is_strictly_after_the_deadline() -> None:
    from datetime import UTC, datetime

    from app.stages.register.read_state import verification_overdue

    applied = datetime(2026, 10, 4, tzinfo=UTC)
    assert not verification_overdue(applied, now=applied + VERIFICATION_DEADLINE)
    assert verification_overdue(applied, now=applied + VERIFICATION_DEADLINE + timedelta(seconds=1))
