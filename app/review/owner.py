"""The durable ReviewItem owner (Gate 2 G2-A, ADR-0016 §2–§5, §8, §9).

It is the only production writer of ``review_items`` and ``review_item_events``. It owns the
review lifecycle and nothing else. It reads no owner truth itself: a registered producer derives
conditions from its owner, and no owner ever reads this module (G2-02).

**Derive and apply are one locked unit** (reviews ``5806206452``, ``5806614407``). Both a
reconciliation and a human resolution take the application write coordinator first, then let the
producer derive, then apply, and only then commit — so no owner write of this process can land
between what was derived and what is applied. A producer's derivation is read-only; one that tries
to write is refused by the coordinator (``DATABASE_WRITE_REENTRANT``), never left to hang, and the
whole unit rolls back.

**Reconciliation alone decides the state** (§4). Given the conditions a producer derives now
within a scope, one unit of work:

- the condition at source identity S, no row for S       → a new OPEN item;
- the condition at S, an OPEN row for S                   → nothing;
- the condition at S, a RESOLVED or SUPERSEDED row for S  → reopened, generation + 1;
- the same condition key at a new identity S′             → the S item SUPERSEDED, S′ OPEN;
- nothing for a condition key with an OPEN item           → RESOLVED, OWNER_CONDITION_CLEARED.

**A human resolution never overrides the owner** (§5). It re-derives the owner through the
item's producer and reconciles in the same unit of work: the item closes only if the owner no
longer derives the condition, and it stays OPEN, with the resolution recorded as history,
otherwise. A retry of the same resolution writes nothing; a stale, mismatched or conflicting one
writes nothing (§8).

Every transition is audited in the same unit of work: identifiers, states, keys and enums only,
never a note (§9).
"""

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database
from app.review.model import (
    REVIEW_REFERENCE_INVALID,
    ResolutionOutcome,
    ReviewBasis,
    ReviewCondition,
    ReviewConflictError,
    ReviewDisposition,
    ReviewEvent,
    ReviewKind,
    ReviewState,
    canonical_scope,
    evidence_reference,
    sanitized_note,
)
from app.review.models import ReviewItem, ReviewItemEvent

# The system actor of every reconciliation transition (§9).
SYSTEM_ACTOR: Final = "system:review"

REVIEW_ITEM_NOT_FOUND: Final = "REVIEW_ITEM_NOT_FOUND"
REVIEW_ITEM_SCOPE_MISMATCH: Final = "REVIEW_ITEM_SCOPE_MISMATCH"
REVIEW_ITEM_MOVED: Final = "REVIEW_ITEM_MOVED"
REVIEW_RESOLUTION_ALREADY_RECORDED: Final = "REVIEW_RESOLUTION_ALREADY_RECORDED"
REVIEW_PRODUCER_NOT_WIRED: Final = "REVIEW_PRODUCER_NOT_WIRED"
REVIEW_CONDITION_AMBIGUOUS: Final = "REVIEW_CONDITION_AMBIGUOUS"

_AUDIT_TYPE: Final = {
    ReviewEvent.OPENED: AuditEventType.REVIEW_ITEM_OPENED,
    ReviewEvent.REOPENED: AuditEventType.REVIEW_ITEM_REOPENED,
    ReviewEvent.SUPERSEDED: AuditEventType.REVIEW_ITEM_SUPERSEDED,
    ReviewEvent.RESOLVED: AuditEventType.REVIEW_ITEM_RESOLVED,
    ReviewEvent.RESOLUTION_RECORDED: AuditEventType.REVIEW_ITEM_RESOLUTION_RECORDED,
}


class ReviewProducer(Protocol):
    """One owner derivation that opens review work (§6)."""

    @property
    def name(self) -> str: ...

    def scopes(self) -> Sequence[Mapping[str, str]]:
        """Every canonical scope the owner holds truth for now: what a full pass visits. It
        writes nothing."""
        ...

    def derive(self, scope: Mapping[str, str]) -> Sequence[ReviewCondition]:
        """Every condition the owner derives **now** among items whose scope includes ``scope``,
        read from the owner's own truth. It writes nothing: it runs under the write
        coordinator, and a write attempt is refused as ``DATABASE_WRITE_REENTRANT``."""
        ...


@dataclass(frozen=True)
class ReviewItemRecord:
    review_item_id: str
    kind: ReviewKind
    producer: str
    scope: Mapping[str, str]
    subject: str
    reason_code: str
    source_identity: str
    condition_key: str
    review_key: str
    state: ReviewState
    generation: int
    opened_at: datetime
    changed_at: datetime


@dataclass(frozen=True)
class ReviewEventRecord:
    event_no: int
    generation: int
    event: ReviewEvent
    from_state: ReviewState | None
    to_state: ReviewState
    basis: ReviewBasis
    successor_item_id: str | None
    disposition: ReviewDisposition | None
    note: str | None
    evidence_reference: str | None
    actor: str
    correlation_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class ReconcileResult:
    """What one reconciliation did, by review item identity."""

    opened: tuple[str, ...] = ()
    reopened: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolutionResult:
    item: ReviewItemRecord
    outcome: ResolutionOutcome
    # True when this exact resolution was already recorded and nothing was written now.
    replayed: bool
    successor_item_id: str | None = None


@dataclass(frozen=True)
class _Human:
    """A human resolution being reconciled: it attributes the target item's closing (§5)."""

    review_item_id: str
    disposition: ReviewDisposition
    note: str | None
    evidence_reference: str | None
    actor: str


class ReviewItemStore:
    """The only production writer of the ReviewItem tables."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        *,
        producers: Iterable[ReviewProducer] = (),
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._producers = {producer.name: producer for producer in producers}

    @property
    def producers(self) -> tuple[str, ...]:
        """The producers wired to this owner."""
        return tuple(sorted(self._producers))

    def producer(self, name: str) -> ReviewProducer:
        found = self._producers.get(name)
        if found is None:
            raise ReviewConflictError(
                REVIEW_PRODUCER_NOT_WIRED,
                "no producer of that name is wired to the review owner",
                details={"producer": name},
            )
        return found

    def scopes(self, producer: str) -> tuple[dict[str, str], ...]:
        """Every distinct scope the producer's items hold, whatever their state."""
        with self._db.read() as session:
            raw = session.scalars(
                select(ReviewItem.scope_json).where(ReviewItem.producer == producer).distinct()
            ).all()
        return tuple(json.loads(value) for value in sorted(raw))

    # -------------------------------------------------------------- reads

    def item(self, review_item_id: str) -> ReviewItemRecord:
        with self._db.read() as session:
            row = session.get(ReviewItem, review_item_id)
            if row is None:
                raise NotFoundError(REVIEW_ITEM_NOT_FOUND, "no such review item")
            return _item_record(row)

    def items(
        self,
        *,
        kind: ReviewKind | None = None,
        state: ReviewState | None = None,
        producer: str | None = None,
        scope: Mapping[str, str] | None = None,
        limit: int = 200,
    ) -> tuple[ReviewItemRecord, ...]:
        """Items of one kind, state, producer and scope, newest change first. Only this scope's
        items are ever returned: one scope never renders as another's (§8)."""
        wanted = None if scope is None else canonical_scope(scope)
        with self._db.read() as session:
            query = select(ReviewItem).order_by(
                ReviewItem.changed_at.desc(), ReviewItem.review_item_id
            )
            if kind is not None:
                query = query.where(ReviewItem.kind == kind.value)
            if state is not None:
                query = query.where(ReviewItem.state == state.value)
            if producer is not None:
                query = query.where(ReviewItem.producer == producer)
            rows = [_item_record(row) for row in session.scalars(query)]
        matching = [row for row in rows if wanted is None or _within(row.scope, wanted)]
        return tuple(matching[: max(0, limit)])

    def history(self, review_item_id: str) -> tuple[ReviewEventRecord, ...]:
        with self._db.read() as session:
            if session.get(ReviewItem, review_item_id) is None:
                raise NotFoundError(REVIEW_ITEM_NOT_FOUND, "no such review item")
            return tuple(
                _event_record(row)
                for row in session.scalars(
                    select(ReviewItemEvent)
                    .where(ReviewItemEvent.review_item_id == review_item_id)
                    .order_by(ReviewItemEvent.event_no)
                )
            )

    # -------------------------------------------------------------- reconciliation (§4)

    def reconcile(
        self,
        producer: str,
        *,
        scope: Mapping[str, str] | None,
        correlation_id: str,
    ) -> ReconcileResult:
        """Make the producer's items within ``scope`` match what its owner derives now.

        The producer derives **inside** the write unit that applies it, so what is applied is
        what the owner holds at commit (reviews 5806206452, 5806614407). ``scope=None`` covers
        every item of the producer in one unit. Idempotent: the same owner truth again changes
        nothing, whatever reload, restart or retry came in between.
        """
        wanted = None if scope is None else canonical_scope(scope)
        with self._db.write() as session:
            source = self.producer(producer)
            derived = source.derive({} if wanted is None else wanted)
            conditions = _validated(producer, wanted, derived)
            return self._apply(session, producer, wanted, conditions, correlation_id, None)

    # -------------------------------------------------------------- human resolution (§5)

    def resolve(
        self,
        review_item_id: str,
        *,
        expected_scope: Mapping[str, str],
        expected_generation: int,
        disposition: ReviewDisposition,
        note: str | None,
        evidence: str | None,
        actor: str,
        correlation_id: str,
    ) -> ResolutionResult:
        """Record one human resolution of one item generation, reconciled against its owner.

        Everything decisive happens inside **one** write unit (review ``5806206452``): the
        application write coordinator is held from before the owner is re-derived until the
        item, its events and their audit commit. The stale, generation and replay checks read
        that same session, and no owner write of this process can pass the coordinator while
        the producer derives, so the resolution is never reconciled against owner truth that
        has already moved. The producer's derivation is read-only; it must not write.
        """
        if not isinstance(disposition, ReviewDisposition):
            raise InputValidationError(REVIEW_REFERENCE_INVALID, "disposition is server-owned")
        human = _Human(
            review_item_id, disposition, sanitized_note(note), evidence_reference(evidence), actor
        )
        shown = canonical_scope(expected_scope)
        with self._db.write() as session:
            row = session.get(ReviewItem, review_item_id)
            if row is None:
                raise NotFoundError(REVIEW_ITEM_NOT_FOUND, "no such review item")
            current = _item_record(row)
            replay = _check_resolvable(session, current, shown, expected_generation, human)
            if replay is not None:
                return replay
            producer = self._producers.get(current.producer)
            if producer is None:
                raise ReviewConflictError(
                    REVIEW_PRODUCER_NOT_WIRED,
                    "the item's producer is not wired, so its owner cannot be re-derived",
                    details={"producer": current.producer},
                )
            derived = _validated(current.producer, current.scope, producer.derive(current.scope))
            self._apply(session, current.producer, current.scope, derived, correlation_id, human)
            session.refresh(row)
            after = _item_record(row)
            if after.state is ReviewState.RESOLVED:
                outcome, successor = ResolutionOutcome.RESOLVED, None
            else:
                outcome = (
                    ResolutionOutcome.CONDITION_PERSISTS
                    if after.state is ReviewState.OPEN
                    else ResolutionOutcome.SUPERSEDED
                )
                successor = (
                    _successor(session, row) if outcome is ResolutionOutcome.SUPERSEDED else None
                )
                self._record_event(
                    session,
                    row,
                    event=ReviewEvent.RESOLUTION_RECORDED,
                    from_state=after.state,
                    basis=(
                        ReviewBasis.CONDITION_PERSISTS
                        if after.state is ReviewState.OPEN
                        else ReviewBasis.OWNER_SOURCE_MOVED
                    ),
                    correlation_id=correlation_id,
                    human=human,
                )
            return ResolutionResult(after, outcome, replayed=False, successor_item_id=successor)

    # -------------------------------------------------------------- the one lifecycle engine

    def _apply(
        self,
        session: Session,
        producer: str,
        scope: Mapping[str, str] | None,
        conditions: Mapping[str, ReviewCondition],
        correlation_id: str,
        human: _Human | None,
    ) -> ReconcileResult:
        rows = [
            row
            for row in session.scalars(select(ReviewItem).where(ReviewItem.producer == producer))
            if scope is None or _within(json.loads(row.scope_json), scope)
        ]
        by_review_key = {row.review_key: row for row in rows}
        open_by_condition = {
            row.condition_key: row for row in rows if row.state == ReviewState.OPEN.value
        }
        opened: list[str] = []
        reopened: list[str] = []
        superseded: list[str] = []
        resolved: list[str] = []
        unchanged: list[str] = []
        for condition in conditions.values():
            existing = by_review_key.get(condition.review_key)
            if existing is not None and existing.state == ReviewState.OPEN.value:
                unchanged.append(existing.review_item_id)
                continue
            # Not OPEN at this identity, so any OPEN item of the condition is at another one.
            previous = open_by_condition.get(condition.condition_key)
            if previous is not None:
                # The same condition moved to a new source identity. The old item leaves OPEN
                # first, so the one-OPEN-per-condition rule holds at every step (§3, §4); its
                # SUPERSEDED event is written once the successor exists and is OPEN.
                self._move(session, previous, ReviewState.SUPERSEDED)
            if existing is None:
                target = self._insert_open(session, condition, correlation_id)
                opened.append(target.review_item_id)
            else:
                target = existing
                from_state = ReviewState(target.state)
                self._move(session, target, ReviewState.OPEN, next_generation=True)
                self._record_event(
                    session,
                    target,
                    event=ReviewEvent.REOPENED,
                    from_state=from_state,
                    basis=ReviewBasis.OWNER_CONDITION_DERIVED,
                    correlation_id=correlation_id,
                )
                reopened.append(target.review_item_id)
            if previous is not None:
                self._record_event(
                    session,
                    previous,
                    event=ReviewEvent.SUPERSEDED,
                    from_state=ReviewState.OPEN,
                    basis=ReviewBasis.OWNER_SOURCE_MOVED,
                    correlation_id=correlation_id,
                    successor=target.review_item_id,
                )
                superseded.append(previous.review_item_id)
        derived_conditions = {c.condition_key for c in conditions.values()}
        for row in rows:
            if row.state == ReviewState.OPEN.value and row.condition_key not in derived_conditions:
                closer = human if human and human.review_item_id == row.review_item_id else None
                self._move(session, row, ReviewState.RESOLVED)
                self._record_event(
                    session,
                    row,
                    event=ReviewEvent.RESOLVED,
                    from_state=ReviewState.OPEN,
                    basis=(
                        ReviewBasis.HUMAN_RESOLUTION
                        if closer
                        else ReviewBasis.OWNER_CONDITION_CLEARED
                    ),
                    correlation_id=correlation_id,
                    human=closer,
                )
                resolved.append(row.review_item_id)
        return ReconcileResult(
            tuple(opened), tuple(reopened), tuple(superseded), tuple(resolved), tuple(unchanged)
        )

    def _insert_open(
        self, session: Session, condition: ReviewCondition, correlation_id: str
    ) -> ReviewItem:
        now = self._clock.now()
        row = ReviewItem(
            review_item_id=str(uuid.uuid4()),
            kind=condition.kind.value,
            producer=condition.producer,
            scope_json=condition.scope_json(),
            subject=condition.subject,
            reason_code=condition.reason_code,
            source_identity=condition.source_identity,
            condition_key=condition.condition_key,
            review_key=condition.review_key,
            state=ReviewState.OPEN.value,
            generation=1,
            opened_at=now,
            changed_at=now,
        )
        session.add(row)
        session.flush()
        self._record_event(
            session,
            row,
            event=ReviewEvent.OPENED,
            from_state=None,
            basis=ReviewBasis.OWNER_CONDITION_DERIVED,
            correlation_id=correlation_id,
        )
        return row

    def _move(
        self,
        session: Session,
        row: ReviewItem,
        to_state: ReviewState,
        *,
        next_generation: bool = False,
    ) -> None:
        """Move the row's lifecycle only. Its event is written by the caller, after it."""
        row.state = to_state.value
        if next_generation:
            row.generation += 1
        row.changed_at = self._clock.now()
        session.flush()

    def _record_event(
        self,
        session: Session,
        row: ReviewItem,
        *,
        event: ReviewEvent,
        from_state: ReviewState | None,
        basis: ReviewBasis,
        correlation_id: str,
        human: _Human | None = None,
        successor: str | None = None,
    ) -> None:
        number = 1 + int(
            session.scalar(
                select(func.coalesce(func.max(ReviewItemEvent.event_no), 0)).where(
                    ReviewItemEvent.review_item_id == row.review_item_id
                )
            )
            or 0
        )
        actor = SYSTEM_ACTOR if human is None else human.actor
        session.add(
            ReviewItemEvent(
                event_id=str(uuid.uuid4()),
                review_item_id=row.review_item_id,
                event_no=number,
                generation=row.generation,
                event=event.value,
                from_state=None if from_state is None else from_state.value,
                to_state=row.state,
                basis=basis.value,
                successor_item_id=successor,
                disposition=None if human is None else human.disposition.value,
                note=None if human is None else human.note,
                evidence_reference=None if human is None else human.evidence_reference,
                actor=actor,
                correlation_id=correlation_id,
                occurred_at=self._clock.now(),
            )
        )
        session.flush()
        self._audit.append(
            AuditEntry(
                event_type=_AUDIT_TYPE[event],
                action=f"REVIEW_ITEM_{event.value}",
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=row.review_item_id,
                before=None
                if from_state is None
                else {
                    "state": from_state.value,
                    "generation": row.generation - (event is ReviewEvent.REOPENED),
                },
                after={"state": row.state, "generation": row.generation, "event_no": number},
                details={
                    "kind": row.kind,
                    "producer": row.producer,
                    "review_key": row.review_key,
                    "basis": basis.value,
                    "successor_item_id": successor,
                    "disposition": None if human is None else human.disposition.value,
                },
                correlation_id=correlation_id,
            ),
            session=session,
        )


def _validated(
    producer: str, scope: Mapping[str, str] | None, derived: Iterable[ReviewCondition]
) -> dict[str, ReviewCondition]:
    """The derivation, keyed by condition key. Every condition is the producer's own and inside
    the reconciled scope, and one condition has one current source identity."""
    conditions: dict[str, ReviewCondition] = {}
    for condition in derived:
        if not isinstance(condition, ReviewCondition) or condition.producer != producer:
            raise InputValidationError(
                REVIEW_REFERENCE_INVALID, "a producer reconciles only its own conditions"
            )
        if scope is not None and not _within(condition.scope, scope):
            raise InputValidationError(
                REVIEW_REFERENCE_INVALID, "a condition lies outside the reconciled scope"
            )
        known = conditions.get(condition.condition_key)
        if known is not None and known.review_key != condition.review_key:
            raise InputValidationError(
                REVIEW_CONDITION_AMBIGUOUS,
                "one condition is derived at two source identities at once",
            )
        conditions[condition.condition_key] = condition
    return conditions


def _check_resolvable(
    session: Session,
    item: ReviewItemRecord,
    shown: Mapping[str, str],
    expected_generation: int,
    human: _Human,
) -> ResolutionResult | None:
    """Refuse a mismatched, stale or conflicting resolution; return a replay unchanged. It reads
    only the caller's locked session, so the check and the write decide on the same state."""
    if dict(item.scope) != dict(shown):
        raise ReviewConflictError(
            REVIEW_ITEM_SCOPE_MISMATCH, "the item belongs to another scope than the one shown"
        )
    row = session.scalars(
        select(ReviewItemEvent).where(
            ReviewItemEvent.review_item_id == item.review_item_id,
            ReviewItemEvent.generation == expected_generation,
            ReviewItemEvent.disposition.is_not(None),
        )
    ).one_or_none()
    if row is not None:
        recorded = _event_record(row)
        if (recorded.disposition, recorded.note, recorded.evidence_reference) != (
            human.disposition,
            human.note,
            human.evidence_reference,
        ):
            raise ReviewConflictError(
                REVIEW_RESOLUTION_ALREADY_RECORDED,
                "this item generation already has a different resolution",
                details={"generation": expected_generation},
            )
        item_row = session.get(ReviewItem, item.review_item_id)
        assert item_row is not None
        return ResolutionResult(
            item,
            _outcome_of(recorded),
            replayed=True,
            successor_item_id=_successor(session, item_row)
            if recorded.basis is ReviewBasis.OWNER_SOURCE_MOVED
            else None,
        )
    if item.generation != expected_generation or item.state is not ReviewState.OPEN:
        raise ReviewConflictError(
            REVIEW_ITEM_MOVED,
            "the item moved since it was shown; reload it before resolving",
            details={"state": item.state.value, "generation": item.generation},
        )
    return None


def _within(scope: Mapping[str, str], wanted: Mapping[str, str]) -> bool:
    """Whether an item's scope includes every identifier of ``wanted``."""
    return all(scope.get(key) == value for key, value in wanted.items())


def _successor(session: Session, row: ReviewItem) -> str | None:
    event = session.scalars(
        select(ReviewItemEvent)
        .where(
            ReviewItemEvent.review_item_id == row.review_item_id,
            ReviewItemEvent.event == ReviewEvent.SUPERSEDED.value,
        )
        .order_by(ReviewItemEvent.event_no.desc())
    ).first()
    return None if event is None else event.successor_item_id


def _outcome_of(recorded: ReviewEventRecord) -> ResolutionOutcome:
    if recorded.event is ReviewEvent.RESOLVED:
        return ResolutionOutcome.RESOLVED
    if recorded.basis is ReviewBasis.OWNER_SOURCE_MOVED:
        return ResolutionOutcome.SUPERSEDED
    return ResolutionOutcome.CONDITION_PERSISTS


def _item_record(row: ReviewItem) -> ReviewItemRecord:
    return ReviewItemRecord(
        review_item_id=row.review_item_id,
        kind=ReviewKind(row.kind),
        producer=row.producer,
        scope=json.loads(row.scope_json),
        subject=row.subject,
        reason_code=row.reason_code,
        source_identity=row.source_identity,
        condition_key=row.condition_key,
        review_key=row.review_key,
        state=ReviewState(row.state),
        generation=row.generation,
        opened_at=row.opened_at,
        changed_at=row.changed_at,
    )


def _event_record(row: ReviewItemEvent) -> ReviewEventRecord:
    return ReviewEventRecord(
        event_no=row.event_no,
        generation=row.generation,
        event=ReviewEvent(row.event),
        from_state=None if row.from_state is None else ReviewState(row.from_state),
        to_state=ReviewState(row.to_state),
        basis=ReviewBasis(row.basis),
        successor_item_id=row.successor_item_id,
        disposition=None if row.disposition is None else ReviewDisposition(row.disposition),
        note=row.note,
        evidence_reference=row.evidence_reference,
        actor=row.actor,
        correlation_id=row.correlation_id,
        occurred_at=row.occurred_at,
    )
