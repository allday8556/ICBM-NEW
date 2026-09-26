"""The durable ReviewItem owner over a real migrated database (Gate 2 G2-A, ADR-0016).

What this proves, with a test producer standing in for an owner (production wires none in G2-A):
- the two keys are server-computed and deterministic, and only bounded owner identifiers are
  accepted as references (§2, §3, §9);
- one row per review key and one OPEN item per condition key, through repeats and a restart (§3);
- reconciliation alone moves the lifecycle: open, supersede on a new source identity, resolve when
  the owner no longer derives the condition, reopen the same row when it reappears (§4);
- a human resolution changes no owner fact, closes the item only if the owner no longer derives it,
  and otherwise leaves it OPEN with the resolution recorded; a retry writes nothing, and a stale,
  mismatched, conflicting or unwired one is refused with nothing written (§5, §8);
- every transition is audited, and the database itself refuses what the owner never does (§9).

No provider is contacted and no owner is read: the test producer holds its conditions in memory.
"""

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from alembic import command

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry
from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import AppError, InputValidationError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.db.database import create_sqlite_engine
from app.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.review.model import (
    ResolutionOutcome,
    ReviewBasis,
    ReviewCondition,
    ReviewConflictError,
    ReviewDisposition,
    ReviewEvent,
    ReviewKind,
    ReviewState,
)
from app.review.owner import SYSTEM_ACTOR, ReviewItemStore
from tests.product_support import raw
from tests.support import FakeClock

pytestmark = pytest.mark.integration

PRODUCER = "test.facts"
OPERATOR = "operator:local"
ITEMS, EVENTS = "review_items", "review_item_events"
REVIEW_TABLES = (ITEMS, EVENTS)


class FakeProducer:
    """An owner derivation held in memory: ``current`` is what the owner derives now."""

    def __init__(self, name: str = PRODUCER) -> None:
        self._name = name
        self.current: list[ReviewCondition] = []
        self.calls = 0
        # A well-behaved producer derives only inside the scope it is asked for.
        self.filtering = True

    @property
    def name(self) -> str:
        return self._name

    def scopes(self) -> Sequence[Mapping[str, str]]:
        return list(
            {json.dumps(dict(c.scope), sort_keys=True): c.scope for c in self.current}.values()
        )

    def truth_token(self) -> str:
        return json.dumps(sorted(c.review_key for c in self.current))

    def derive(self, scope: Mapping[str, str]) -> Sequence[ReviewCondition]:
        self.calls += 1
        if not self.filtering:
            return list(self.current)
        return [c for c in self.current if all(c.scope.get(k) == v for k, v in scope.items())]


def condition(
    product: str = "p-1",
    *,
    source: str = "rev-1",
    subject: str = "stock",
    reason: str = "SOURCE_FIELD_REVIEW_REQUIRED",
    kind: ReviewKind = ReviewKind.COLLECT_EVIDENCE,
    producer: str = PRODUCER,
) -> ReviewCondition:
    return ReviewCondition(
        kind=kind,
        producer=producer,
        scope={"supplier_key": "kmtong", "source_product_id": product},
        subject=subject,
        reason_code=reason,
        source_identity=source,
    )


def scope_of(product: str = "p-1") -> dict[str, str]:
    return {"supplier_key": "kmtong", "source_product_id": product}


@contextlib.contextmanager
def running(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    """One application process on the data directory: a restart is a second one."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config, ownership=lease, clock=clock, secret_store=MemorySecretStore()
        )
        try:
            yield built
        finally:
            built.db.dispose()


@pytest.fixture
def app(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    with running(config, clock) as built:
        yield built


@pytest.fixture
def producer() -> FakeProducer:
    return FakeProducer()


@pytest.fixture
def owner(app: Container, producer: FakeProducer) -> ReviewItemStore:
    """The owner as a producer slice will wire it: the same tables, one test producer."""
    return ReviewItemStore(app.db, app.clock, app.audit, producers=[producer])


def rows(config: AppConfig, table: str) -> list[dict[str, object]]:
    with contextlib.closing(raw(config)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(r) for r in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def review_audit(config: AppConfig) -> list[dict[str, object]]:
    with contextlib.closing(raw(config)) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in connection.execute(
                "SELECT * FROM audit_events WHERE event_type LIKE 'REVIEW_ITEM_%' ORDER BY seq"
            )
        ]


def snapshot(config: AppConfig) -> dict[str, int]:
    """Every table's row count: what a refused write must leave exactly as it was."""
    with contextlib.closing(raw(config)) as connection:
        names = [
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {n: connection.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}


def reconcile(
    owner: ReviewItemStore, derived: Sequence[ReviewCondition], scope: Mapping[str, str] | None
) -> object:
    """The owner now derives ``derived``; the store reconciles by deriving it itself."""
    source = owner.producer(PRODUCER)
    assert isinstance(source, FakeProducer)
    source.current = list(derived)
    return owner.reconcile(PRODUCER, scope=scope, correlation_id="cid-reconcile")


def resolve(
    owner: ReviewItemStore,
    item_id: str,
    *,
    scope: Mapping[str, str] | None = None,
    generation: int = 1,
    disposition: ReviewDisposition = ReviewDisposition.OWNER_ACTION_TAKEN,
    note: str | None = "재수집을 요청했습니다",
    evidence: str | None = None,
) -> object:
    return owner.resolve(
        item_id,
        expected_scope=scope or scope_of(),
        expected_generation=generation,
        disposition=disposition,
        note=note,
        evidence=evidence,
        actor=OPERATOR,
        correlation_id="cid-resolve",
    )


# ---------------------------------------------------------------- production wiring (G2-A)


def test_production_wires_exactly_the_gate2_producers(app: Container) -> None:
    # G2-B wires COLLECT / M3, G2-C M4 base readiness and REGISTER (execution and preparations),
    # and nothing else
    # (ADR-0016 §6). Their counts are G2-C's (tests/integration/test_g2c_review_counts.py).
    assert app.review_items.producers == (
        "collect.facts",
        "products.readiness",
        "register.execution",
        "register.preflight",
    )
    assert app.review_items.items() == ()


# ---------------------------------------------------------------- identity (§2, §3, §9)


def test_the_keys_are_server_computed_and_deterministic() -> None:
    one, same = condition(), condition()
    assert (one.condition_key, one.review_key) == (same.condition_key, same.review_key)
    # The scope's key order is not an identity.
    reordered = ReviewCondition(
        kind=one.kind,
        producer=one.producer,
        scope={"source_product_id": "p-1", "supplier_key": "kmtong"},
        subject=one.subject,
        reason_code=one.reason_code,
        source_identity=one.source_identity,
    )
    assert reordered.review_key == one.review_key
    moved = condition(source="rev-2")
    assert moved.condition_key == one.condition_key and moved.review_key != one.review_key
    for other in (
        condition(product="p-2"),
        condition(subject="price"),
        condition(reason="OTHER_REASON"),
        condition(kind=ReviewKind.STOCK),
        condition(producer="test.other"),
    ):
        assert other.condition_key != one.condition_key


@pytest.mark.parametrize(
    "change",
    [
        {"scope": {"supplier_key": "kmtong", "product_url": "p-1"}},  # not a canonical scope key
        {"scope": {}},
        {"subject": "https://supplier.example/p/1"},
        {"subject": "page text with spaces"},
        {"reason_code": "token=abc/def"},
        {"source_identity": ""},
        {"producer": "Bad Producer"},
    ],
)
def test_only_bounded_owner_identifiers_are_references(change: dict[str, object]) -> None:
    values: dict[str, object] = {
        "kind": ReviewKind.COLLECT_EVIDENCE,
        "producer": PRODUCER,
        "scope": scope_of(),
        "subject": "stock",
        "reason_code": "SOURCE_FIELD_REVIEW_REQUIRED",
        "source_identity": "rev-1",
    }
    values.update(change)
    with pytest.raises(InputValidationError):
        ReviewCondition(**values)  # type: ignore[arg-type]


# ---------------------------------------------------------------- lifecycle (§4)


def test_repeats_and_a_restart_never_multiply_open_items(
    config: AppConfig, clock: FakeClock
) -> None:
    with running(config, clock) as first_run:
        owner = ReviewItemStore(
            first_run.db, first_run.clock, first_run.audit, producers=[FakeProducer()]
        )
        first = reconcile(owner, [condition()], scope_of())
        again = reconcile(owner, [condition()], scope_of())
        full = reconcile(owner, [condition()], None)
        (item,) = owner.items()
    assert first.opened == (item.review_item_id,)  # type: ignore[attr-defined]
    assert again.unchanged == full.unchanged == (item.review_item_id,)  # type: ignore[attr-defined]
    with running(config, clock) as restarted:
        after = ReviewItemStore(
            restarted.db, restarted.clock, restarted.audit, producers=[FakeProducer()]
        )
        reconcile(after, [condition()], None)
        assert after.items() == (item,)
        assert [e.event for e in after.history(item.review_item_id)] == [ReviewEvent.OPENED]
    assert [r["event_type"] for r in review_audit(config)] == ["REVIEW_ITEM_OPENED"]


def test_a_new_source_identity_supersedes_the_old_item(owner: ReviewItemStore) -> None:
    reconcile(owner, [condition(source="rev-1")], scope_of())
    (old,) = owner.items()
    result = reconcile(owner, [condition(source="rev-2")], scope_of())
    new = owner.items(state=ReviewState.OPEN)[0]
    assert result.superseded == (old.review_item_id,)  # type: ignore[attr-defined]
    assert result.opened == (new.review_item_id,)  # type: ignore[attr-defined]
    superseded = owner.item(old.review_item_id)
    # Nothing is rewritten or merged: the old item keeps its own source identity.
    assert superseded.state is ReviewState.SUPERSEDED
    assert (superseded.source_identity, new.source_identity) == ("rev-1", "rev-2")
    assert superseded.condition_key == new.condition_key
    (_, event) = owner.history(old.review_item_id)
    assert (event.event, event.basis, event.successor_item_id) == (
        ReviewEvent.SUPERSEDED,
        ReviewBasis.OWNER_SOURCE_MOVED,
        new.review_item_id,
    )
    assert len(owner.items(state=ReviewState.OPEN)) == 1


def test_a_cleared_condition_is_resolved_by_the_system(owner: ReviewItemStore) -> None:
    reconcile(owner, [condition()], scope_of())
    (item,) = owner.items()
    result = reconcile(owner, [], scope_of())
    assert result.resolved == (item.review_item_id,)  # type: ignore[attr-defined]
    last = owner.history(item.review_item_id)[-1]
    assert (last.event, last.basis, last.actor, last.disposition) == (
        ReviewEvent.RESOLVED,
        ReviewBasis.OWNER_CONDITION_CLEARED,
        SYSTEM_ACTOR,
        None,
    )


def test_a_reappearing_review_key_reopens_its_own_row(owner: ReviewItemStore) -> None:
    reconcile(owner, [condition(source="rev-1")], scope_of())
    (first,) = owner.items()
    reconcile(owner, [], scope_of())
    reconcile(owner, [condition(source="rev-1")], scope_of())
    reopened = owner.item(first.review_item_id)
    assert (reopened.state, reopened.generation) == (ReviewState.OPEN, 2)
    assert len(owner.items()) == 1
    # A superseded identity that becomes current again reopens too, and supersedes its successor.
    reconcile(owner, [condition(source="rev-2")], scope_of())
    second = owner.items(state=ReviewState.OPEN)[0]
    reconcile(owner, [condition(source="rev-1")], scope_of())
    assert owner.item(first.review_item_id).state is ReviewState.OPEN
    assert owner.item(first.review_item_id).generation == 3
    assert owner.item(second.review_item_id).state is ReviewState.SUPERSEDED
    assert [e.event for e in owner.history(first.review_item_id)] == [
        ReviewEvent.OPENED,
        ReviewEvent.RESOLVED,
        ReviewEvent.REOPENED,
        ReviewEvent.SUPERSEDED,
        ReviewEvent.REOPENED,
    ]


def test_a_scoped_reconciliation_touches_only_its_scope(
    config: AppConfig, owner: ReviewItemStore, producer: FakeProducer
) -> None:
    reconcile(owner, [condition("p-1"), condition("p-2")], None)
    producer.current = [condition("p-2")]
    owner.reconcile(PRODUCER, scope=scope_of("p-1"), correlation_id="cid-scope")
    states = {i.scope["source_product_id"]: i.state for i in owner.items()}
    assert states == {"p-1": ReviewState.RESOLVED, "p-2": ReviewState.OPEN}
    assert [i.scope["source_product_id"] for i in owner.items(scope=scope_of("p-2"))] == ["p-2"]
    before = snapshot(config)
    # A producer that answers outside the scope it was asked for is refused, not trusted.
    producer.filtering = False
    with pytest.raises(InputValidationError):
        reconcile(owner, [condition("p-2")], scope_of("p-1"))
    producer.filtering = True
    with pytest.raises(InputValidationError):
        reconcile(owner, [condition(producer="test.other")], None)
    with pytest.raises(InputValidationError, match="two source identities"):
        reconcile(owner, [condition(source="rev-1"), condition(source="rev-2")], scope_of())
    with pytest.raises(ReviewConflictError, match="no producer"):
        owner.reconcile("test.unwired", scope=None, correlation_id="c")
    assert snapshot(config) == before


# ---------------------------------------------------------------- human resolution (§5, §8)


def test_a_resolution_while_the_owner_still_derives_leaves_the_item_open(
    config: AppConfig, owner: ReviewItemStore, producer: FakeProducer
) -> None:
    producer.current = [condition()]
    reconcile(owner, producer.current, scope_of())
    (item,) = owner.items()
    result = resolve(owner, item.review_item_id)
    assert result.outcome is ResolutionOutcome.CONDITION_PERSISTS  # type: ignore[attr-defined]
    after = owner.item(item.review_item_id)
    assert (after.state, after.generation) == (ReviewState.OPEN, 1)
    last = owner.history(item.review_item_id)[-1]
    assert (last.event, last.basis, last.actor, last.disposition, last.note) == (
        ReviewEvent.RESOLUTION_RECORDED,
        ReviewBasis.CONDITION_PERSISTS,
        OPERATOR,
        ReviewDisposition.OWNER_ACTION_TAKEN,
        "재수집을 요청했습니다",
    )
    assert len(owner.items(state=ReviewState.OPEN)) == 1
    # One derivation to open the item, and one inside the resolution's own locked unit.
    assert producer.calls == 2


def test_a_resolution_after_the_owner_changed_closes_it_as_the_human(
    owner: ReviewItemStore, producer: FakeProducer
) -> None:
    producer.current = [condition()]
    reconcile(owner, producer.current, scope_of())
    (item,) = owner.items()
    producer.current = []  # the owner's own command changed its truth; the queue did not
    result = resolve(owner, item.review_item_id, evidence="rev-2")
    assert result.outcome is ResolutionOutcome.RESOLVED  # type: ignore[attr-defined]
    last = owner.history(item.review_item_id)[-1]
    assert (last.event, last.basis, last.actor, last.evidence_reference) == (
        ReviewEvent.RESOLVED,
        ReviewBasis.HUMAN_RESOLUTION,
        OPERATOR,
        "rev-2",
    )


def test_a_resolution_when_the_source_moved_supersedes_and_records(
    owner: ReviewItemStore, producer: FakeProducer
) -> None:
    producer.current = [condition(source="rev-1")]
    reconcile(owner, producer.current, scope_of())
    (item,) = owner.items()
    producer.current = [condition(source="rev-2")]
    result = resolve(owner, item.review_item_id)
    assert result.outcome is ResolutionOutcome.SUPERSEDED  # type: ignore[attr-defined]
    successor = owner.items(state=ReviewState.OPEN)[0]
    assert result.successor_item_id == successor.review_item_id  # type: ignore[attr-defined]
    last = owner.history(item.review_item_id)[-1]
    assert (last.event, last.basis, last.disposition) == (
        ReviewEvent.RESOLUTION_RECORDED,
        ReviewBasis.OWNER_SOURCE_MOVED,
        ReviewDisposition.OWNER_ACTION_TAKEN,
    )


def test_a_retry_writes_nothing_and_a_different_resolution_is_refused(
    config: AppConfig, owner: ReviewItemStore, producer: FakeProducer
) -> None:
    producer.current = [condition()]
    reconcile(owner, producer.current, scope_of())
    (item,) = owner.items()
    first = resolve(owner, item.review_item_id)
    before = snapshot(config)
    replay = resolve(owner, item.review_item_id)
    assert replay.replayed is True  # type: ignore[attr-defined]
    assert replay.outcome is first.outcome  # type: ignore[attr-defined]
    assert snapshot(config) == before
    with pytest.raises(ReviewConflictError, match="different resolution"):
        resolve(owner, item.review_item_id, disposition=ReviewDisposition.NO_ACTION_TAKEN)
    assert snapshot(config) == before


def test_a_stale_mismatched_or_unwired_resolution_writes_nothing(
    config: AppConfig, app: Container, owner: ReviewItemStore, producer: FakeProducer
) -> None:
    producer.current = [condition("p-1"), condition("p-2")]
    reconcile(owner, producer.current, None)
    one = owner.items(scope=scope_of("p-1"))[0]
    before = snapshot(config)
    cases: list[tuple[dict[str, object], str]] = [
        # Product B's scope shown for product A's item.
        ({"scope": scope_of("p-2")}, "REVIEW_ITEM_SCOPE_MISMATCH"),
        # A generation the item never reached, or already left.
        ({"generation": 2}, "REVIEW_ITEM_MOVED"),
        ({"note": "see https://supplier.example/p/1"}, "REVIEW_NOTE_UNSAFE"),
        ({"note": "token=abcdef"}, "REVIEW_NOTE_UNSAFE"),
        ({"note": "x" * 501}, "REVIEW_NOTE_UNSAFE"),
        ({"evidence": "free text evidence"}, "REVIEW_REFERENCE_INVALID"),
        ({"disposition": "PASS"}, "REVIEW_REFERENCE_INVALID"),
    ]
    for change, code in cases:
        with pytest.raises(AppError) as refused:
            resolve(owner, one.review_item_id, **change)  # type: ignore[arg-type]
        assert refused.value.code == code, change
    # Production wires no producer, so it cannot re-derive the owner and refuses.
    with pytest.raises(ReviewConflictError) as unwired:
        resolve(app.review_items, one.review_item_id)
    assert unwired.value.code == "REVIEW_PRODUCER_NOT_WIRED"
    # A resolved item is no longer resolvable at its generation.
    producer.current = [condition("p-2")]
    reconcile(owner, producer.current, None)
    after_close = snapshot(config)
    with pytest.raises(ReviewConflictError) as moved:
        resolve(owner, one.review_item_id)
    assert moved.value.code == "REVIEW_ITEM_MOVED"
    assert snapshot(config) == after_close
    assert before[ITEMS] == after_close[ITEMS]


def test_an_owner_write_cannot_slip_between_derive_and_apply(
    config: AppConfig, app: Container, owner: ReviewItemStore, producer: FakeProducer
) -> None:
    """Review 5806206452: the owner is re-derived and the resolution applied in one locked unit.

    The owner has cleared the condition when the human resolves. While the producer derives, the
    owner's own command tries to put the condition back through the same write coordinator every
    owner write uses. It must wait: the resolution commits against the truth it derived, and the
    owner write lands after it — never between derive and apply."""
    producer.current = [condition()]
    reconcile(owner, producer.current, scope_of())
    (item,) = owner.items()
    producer.current = []
    owner_committed = threading.Event()

    def owner_write() -> None:
        with app.db.write() as session:
            producer.current = [condition()]
            app.audit.append(
                AuditEntry(
                    event_type=AuditEventType.PROTECTED_ACTION,
                    action="OWNER_WRITE",
                    actor="owner:test",
                    outcome=AuditOutcome.RECORDED,
                ),
                session=session,
            )
        owner_committed.set()

    writer = threading.Thread(target=owner_write)
    derive = producer.derive
    passed_during_derive: list[bool] = []

    def derive_while_the_owner_writes(scope: Mapping[str, str]) -> Sequence[ReviewCondition]:
        derived = derive(scope)
        writer.start()
        writer.join(timeout=0.5)  # every chance for the owner write to slip in now
        passed_during_derive.append(owner_committed.is_set())
        return derived

    producer.derive = derive_while_the_owner_writes  # type: ignore[method-assign]
    result = resolve(owner, item.review_item_id)
    writer.join(timeout=10)
    assert passed_during_derive == [False]
    assert owner_committed.is_set()
    assert result.outcome is ResolutionOutcome.RESOLVED  # type: ignore[attr-defined]
    with contextlib.closing(raw(config)) as connection:
        order = [
            r[0]
            for r in connection.execute(
                "SELECT action FROM audit_events"
                " WHERE action IN ('REVIEW_ITEM_RESOLVED', 'OWNER_WRITE') ORDER BY seq"
            )
        ]
    assert order == ["REVIEW_ITEM_RESOLVED", "OWNER_WRITE"]
    # The owner's later truth is not lost: the next reconciliation reopens the same item.
    producer.derive = derive  # type: ignore[method-assign]
    reconcile(owner, producer.current, scope_of())
    reopened = owner.item(item.review_item_id)
    assert (reopened.state, reopened.generation) == (ReviewState.OPEN, 2)


def test_a_resolution_changes_no_owner_table(
    config: AppConfig, owner: ReviewItemStore, producer: FakeProducer
) -> None:
    producer.current = [condition()]
    reconcile(owner, producer.current, scope_of())
    (item,) = owner.items()
    before = snapshot(config)
    resolve(owner, item.review_item_id)
    after = snapshot(config)
    changed = {table for table in after if after[table] != before[table]}
    assert changed == {EVENTS, "audit_events"}


# ---------------------------------------------------------------- audit (§9)


def test_every_transition_is_audited_with_references_only(
    config: AppConfig, owner: ReviewItemStore, producer: FakeProducer
) -> None:
    producer.current = [condition(source="rev-1")]
    reconcile(owner, producer.current, scope_of())
    (item,) = owner.items()
    resolve(owner, item.review_item_id, note="비공개 메모 내용")
    reconcile(owner, [condition(source="rev-2")], scope_of())
    reconcile(owner, [], scope_of())
    audited = review_audit(config)
    assert [a["event_type"] for a in audited] == [
        "REVIEW_ITEM_OPENED",
        "REVIEW_ITEM_RESOLUTION_RECORDED",
        "REVIEW_ITEM_OPENED",
        "REVIEW_ITEM_SUPERSEDED",
        "REVIEW_ITEM_RESOLVED",
    ]
    assert {a["actor"] for a in audited} == {SYSTEM_ACTOR, OPERATOR}
    assert all(a["correlation_id"] for a in audited)
    text = json.dumps(audited, ensure_ascii=False)
    assert "비공개 메모 내용" not in text  # a note lives in the event row, never in the audit log
    events = rows(config, EVENTS)
    assert [e["actor"] for e in events if e["disposition"]] == [OPERATOR]


# ---------------------------------------------------------------- the database refuses (§3, §9)


def test_the_database_refuses_what_the_owner_never_does(
    config: AppConfig, owner: ReviewItemStore
) -> None:
    reconcile(owner, [condition(source="rev-1")], scope_of())
    (item,) = rows(config, ITEMS)
    statements = {
        "identity references": f"UPDATE {ITEMS} SET source_identity = 'rev-9'",
        "generation only grows": f"UPDATE {ITEMS} SET generation = 5",
        f"{ITEMS} row is never deleted": f"DELETE FROM {ITEMS}",
        f"{EVENTS} row is never updated": f"UPDATE {EVENTS} SET actor = 'someone'",
        f"{EVENTS} row is never deleted": f"DELETE FROM {EVENTS}",
        "UNIQUE constraint failed: review_items.condition_key": (
            f"INSERT INTO {ITEMS} SELECT 'item-2', kind, producer, scope_json, subject,"
            " reason_code, 'rev-2', condition_key, '" + "a" * 64 + "', 'OPEN', 1,"
            f" opened_at, changed_at FROM {ITEMS}"
        ),
        "follows the one before it": (
            f"INSERT INTO {EVENTS} (event_id, review_item_id, event_no, generation, event,"
            " from_state, to_state, basis, disposition, actor, correlation_id, occurred_at)"
            f" VALUES ('e-9', '{item['review_item_id']}', 5, 1, 'RESOLUTION_RECORDED', 'OPEN',"
            " 'OPEN', 'CONDITION_PERSISTS', 'NO_ACTION_TAKEN', 'a', 'c', '2026-09-24 00:00:00')"
        ),
        "current state and generation": (
            f"INSERT INTO {EVENTS} (event_id, review_item_id, event_no, generation, event,"
            " from_state, to_state, basis, actor, correlation_id, occurred_at) VALUES"
            f" ('e-9', '{item['review_item_id']}', 2, 1, 'RESOLVED', 'OPEN', 'RESOLVED',"
            " 'OWNER_CONDITION_CLEARED', 'a', 'c', '2026-09-24 00:00:00')"
        ),
        "human_fields": (
            f"INSERT INTO {EVENTS} (event_id, review_item_id, event_no, generation, event,"
            " from_state, to_state, basis, disposition, actor, correlation_id, occurred_at)"
            f" VALUES ('e-9', '{item['review_item_id']}', 2, 1, 'RESOLUTION_RECORDED', 'OPEN',"
            " 'OPEN', 'CONDITION_PERSISTS', NULL, 'a', 'c', '2026-09-24 00:00:00')"
        ),
        "event_shape": (
            f"INSERT INTO {EVENTS} (event_id, review_item_id, event_no, generation, event,"
            " from_state, to_state, basis, actor, correlation_id, occurred_at) VALUES"
            f" ('e-9', '{item['review_item_id']}', 2, 1, 'OPENED', 'OPEN', 'OPEN',"
            " 'HUMAN_RESOLUTION', 'a', 'c', '2026-09-24 00:00:00')"
        ),
    }
    with contextlib.closing(raw(config)) as connection:
        for message, statement in statements.items():
            with pytest.raises(sqlite3.IntegrityError, match=message):
                connection.execute(statement)
    assert rows(config, ITEMS) == [item]


# ---------------------------------------------------------------- the migration (0021)


def test_0021_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}"
    upgrade_to_head(url)
    before = _tables(tmp_path / "icbm.db")
    command.downgrade(alembic_config(url), "0020_g1_registration_category_metadata")
    # 0021 owns these two; the coverage table 0022 adds and the Adaptive tables 0024 adds step
    # down with it.
    assert before - _tables(tmp_path / "icbm.db") == {
        *REVIEW_TABLES,
        "review_coverage",
        "adaptive_profile_revisions",
        "adaptive_profile_pins",
        "adaptive_profile_lint",
        "adaptive_profile_transitions",
        "adaptive_validation_samples",
        "adaptive_validation_runs",
        "adaptive_validation_run_samples",
        "adaptive_shadow_switch_entries",
        "adaptive_shadow_records",
        "adaptive_evidence_windows",
        "adaptive_evidence_window_events",
        "adaptive_shadow_ledger_events",
        "live_grants",
        "protected_write_brakes",
        "asset_upload_attempts",
        "adaptive_capture_requests",
        "adaptive_capture_candidates",
        "adaptive_phase_c_commands",
        "adaptive_phase_c_command_results",
    }
    command.upgrade(alembic_config(url), "head")
    assert _tables(tmp_path / "icbm.db") == before
    item = condition()
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        connection.execute(
            f"INSERT INTO {ITEMS} VALUES ('item-1', ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 1, ?, ?)",
            (
                item.kind.value,
                item.producer,
                item.scope_json(),
                item.subject,
                item.reason_code,
                item.source_identity,
                item.condition_key,
                item.review_key,
                "2026-09-24 00:00:00",
                "2026-09-24 00:00:00",
            ),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0020_g1_registration_category_metadata")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0021_g2_review_items"
    finally:
        engine.dispose()


def _tables(database: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
