"""The shadow evidence owner: raw records, the ledger, windows, reconciliation and retention.

ADR-0017 §10.5 and §11, with the Phase C carry-forward `5818794101` closed in code:

- **Raw shadow records** are non-canonical, at most one per run, and hard-bounded: at most
  ``max_age`` old **and** at most ``max_per_supplier`` per supplier, whichever is reached first.
  The bounds are required and can never exceed 90 days and 5,000 records; there is no hold.
- **The ledger** is an append-only event stream per eligible run. Its effective state is the pure
  fold of ``evidence.fold``; nothing is ever updated, and no event follows a terminal state.
- **Windows** are declared for one supplier and one exact bundle. A run is in a window exactly when
  its canonical run froze an ENABLED decision for that bundle at a first reservation inside the
  window; the denominator is derived from the canonical run records, never from this store.
- **Reconciliation** appends the missing ``OUTCOME_RECORDED`` of every eligible run: at startup,
  and on demand before a window is ``ENDED`` or ``CLOSED`` (carry-forward item 3).
- **Exact bundles** (carry-forward item 1): a bundle with a permanent blocking cause or a ``FAIL``
  window is never given another window; only a content-different EPR starts fresh evidence.

It reads the canonical run records and job history through their models, read-only, and it
imports no canonical writer: no revision store, run store, pointer, review or audit owner.
"""

import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.collect.adaptive.canonical import canonical_json, digest
from app.collect.adaptive_shadow.evidence import (
    BundleEvidence,
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
from app.collect.adaptive_shadow.models import (
    MIN_WINDOW_SIZE,
    EvidenceWindow,
    EvidenceWindowEvent,
    LedgerEvent,
    ShadowRecord,
)
from app.collect.adaptive_shadow.switch import running_bundle
from app.collect.adaptive_store.gate import SupplierGate
from app.collect.adaptive_store.store import AdaptiveProfileStore
from app.collect.models import CollectionOutcome, CollectionRun, ProductFactsRevision
from app.core.clock import Clock
from app.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.db.database import Database

logger = logging.getLogger("icbm.collect.shadow")

ADAPTIVE_WINDOW_REFUSED = "ADAPTIVE_WINDOW_REFUSED"
ADAPTIVE_BUNDLE_BLOCKED = "ADAPTIVE_BUNDLE_BLOCKED"
ADAPTIVE_RESOLUTION_REFUSED = "ADAPTIVE_RESOLUTION_REFUSED"
ADAPTIVE_RETENTION_INVALID = "ADAPTIVE_RETENTION_INVALID"
ADAPTIVE_SHADOW_TAMPERED = "ADAPTIVE_SHADOW_TAMPERED"
ADAPTIVE_WINDOW_NOT_FOUND = "ADAPTIVE_WINDOW_NOT_FOUND"
ADAPTIVE_SHADOW_RECORD_NOT_FOUND = "ADAPTIVE_SHADOW_RECORD_NOT_FOUND"
ADAPTIVE_SHADOW_BINDING_REFUSED = "ADAPTIVE_SHADOW_BINDING_REFUSED"

RECORD_DIGEST_SCHEME = "icbm-shadow-record/v1"
# ADR-0017 §10.5: the hard raw bounds. A configured bound may be tighter, never looser.
RAW_MAX_AGE = timedelta(days=90)
RAW_MAX_PER_SUPPLIER = 5000
ELIGIBLE_OUTCOMES = (CollectionOutcome.RECORDED, CollectionOutcome.NO_REVISION)
SYSTEM = "system:shadow"


class ShadowEvidenceTampered(AppError):
    """Stored shadow evidence no legal history produces: nothing is read from it."""

    error_class = ErrorClass.FATAL


def _evidence_refused(code: str, message: str, **details: object) -> InputValidationError:
    return InputValidationError(code, message, details=details or None)


@dataclass(frozen=True)
class RawRetention:
    """The raw bounds, always explicit: a missing bound is a refusal, never a code default."""

    max_age: timedelta
    max_per_supplier: int

    def __post_init__(self) -> None:
        if not timedelta(0) < self.max_age <= RAW_MAX_AGE:
            raise _evidence_refused(
                ADAPTIVE_RETENTION_INVALID, "the raw age bound is at most 90 days"
            )
        if isinstance(self.max_per_supplier, bool) or not (
            0 < self.max_per_supplier <= RAW_MAX_PER_SUPPLIER
        ):
            raise _evidence_refused(
                ADAPTIVE_RETENTION_INVALID, "the raw count bound is at most 5,000"
            )


ADR_RETENTION = RawRetention(RAW_MAX_AGE, RAW_MAX_PER_SUPPLIER)


@dataclass(frozen=True)
class RawRecord:
    collection_run_id: str
    supplier_key: str
    revision_id: str | None
    bundle_key: str
    verdict: RunVerdict
    severity: str | None
    comparison: dict[str, object]
    process_run_id: str
    recorded_at: datetime


@dataclass(frozen=True)
class RetentionStatus:
    """Which raw bound a supplier reaches first (the carry-forward's operational note)."""

    supplier_key: str
    records: int
    count_headroom: int
    oldest_recorded_at: datetime | None
    age_deadline: datetime | None
    estimated_count_deadline: datetime | None
    nearer_bound: str | None  # "AGE" | "COUNT" | None when nothing is held


@dataclass(frozen=True)
class PruneReport:
    pruned: int
    unresolved_pruned: int


@dataclass(frozen=True)
class WindowRecord:
    window_id: str
    supplier_key: str
    bundle_key: str
    min_size: int
    declared_at: datetime
    ended_at: datetime | None
    closed: bool
    superseded: bool
    events: tuple[str, ...]
    closeout: dict[str, object] | None


def _record_digest(row: ShadowRecord) -> str:
    return digest(
        RECORD_DIGEST_SCHEME,
        {
            "run": row.collection_run_id,
            "supplier": row.supplier_key,
            "revision": row.revision_id,
            "bundle": row.bundle_key,
            "verdict": row.run_verdict,
            "severity": row.severity,
            "comparison": json.loads(row.comparison_json),
        },
    )


def _event(row: LedgerEvent) -> Event:
    try:
        return Event(
            seq=row.seq,
            kind=EventKind(row.kind),
            state=State(CountAs(row.count_as), None if row.cause is None else Cause(row.cause)),
            verdict=None if row.run_verdict is None else RunVerdict(row.run_verdict),
            resolution=None if row.resolution is None else Resolution(row.resolution),
            adaptive_failed_closed=row.adaptive_failed_closed,
            window_id=row.window_id,
            process_run_id=row.process_run_id,
        )
    except ValueError as error:
        raise ShadowEvidenceTampered(
            ADAPTIVE_SHADOW_TAMPERED, "a stored ledger event does not parse"
        ) from error


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return False
    if left.tzinfo is None:
        left = left.replace(tzinfo=right.tzinfo)
    if right.tzinfo is None:
        right = right.replace(tzinfo=left.tzinfo)
    return left == right


def _bound(session: Session, row: ShadowRecord) -> bool:
    """Whether a raw record belongs to its canonical run exactly as that run froze it: the same
    supplier, an ENABLED decision for the same bundle, and only that run's own revision."""
    run = session.get(CollectionRun, row.collection_run_id)
    if (
        run is None
        or run.supplier_key != row.supplier_key
        or run.shadow_decision != "ENABLED"
        or run.shadow_bundle_key != row.bundle_key
    ):
        return False
    if row.revision_id is None:
        return True
    revision = session.get(ProductFactsRevision, row.revision_id)
    return revision is not None and revision.collection_run_id == row.collection_run_id


def _fold(events: Sequence[Event]) -> State:
    try:
        return fold(events)
    except (LedgerInvalid, ValueError) as error:
        raise ShadowEvidenceTampered(
            ADAPTIVE_SHADOW_TAMPERED, "a stored ledger stream does not fold", details=None
        ) from error


class ShadowEvidenceStore:
    """The only writer of the raw shadow records, the ledger and the windows."""

    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        supplier_gate: SupplierGate,
        profiles: AdaptiveProfileStore,
        retention: RawRetention,
        process_run_id: str,
    ) -> None:
        if not isinstance(retention, RawRetention):
            raise _evidence_refused(
                ADAPTIVE_RETENTION_INVALID, "the raw retention bounds are required"
            )
        self._db = db
        self._clock = clock
        self._admits = supplier_gate
        self._profiles = profiles
        self._retention = retention
        self._process_run_id = process_run_id

    # ============================================================== the shadow step's write

    def record_outcome(
        self,
        *,
        collection_run_id: str,
        supplier_key: str,
        revision_id: str | None,
        bundle_key: str,
        first_product_read_at: datetime,
        verdict: RunVerdict,
        severity: str | None,
        comparison: dict[str, object],
        correlation_id: str,
    ) -> str | None:
        """The shadow's own write unit: its raw record and, when the run falls in a window, its
        ``OUTCOME_RECORDED``. Returns the window it counted in, if any. Never nested in a
        canonical unit (the collection has none open when the step runs).

        The canonical run is the authority (review 5312254605 B3): the write is refused unless the
        run froze ENABLED for this supplier and this exact bundle at this first reservation, and
        the revision is that run's own. The window is chosen from the run's frozen values alone.
        """
        with self._db.write() as session:
            run = session.get(CollectionRun, collection_run_id)
            if (
                run is None
                or run.supplier_key != supplier_key
                or run.shadow_decision != "ENABLED"
                or run.shadow_switch_entry_id is None
                or run.shadow_bundle_key != bundle_key
                or not _same_instant(run.first_product_read_at, first_product_read_at)
            ):
                raise _evidence_refused(
                    ADAPTIVE_SHADOW_BINDING_REFUSED,
                    "a shadow record binds exactly the run's frozen supplier, decision, bundle and"
                    " first reservation",
                )
            frozen_at = run.first_product_read_at
            assert frozen_at is not None
            row = ShadowRecord(
                collection_run_id=collection_run_id,
                supplier_key=supplier_key,
                revision_id=revision_id,
                bundle_key=bundle_key,
                run_verdict=verdict.value,
                severity=severity,
                comparison_json=canonical_json(comparison),
                record_digest="0" * 64,
                process_run_id=self._process_run_id,
                recorded_at=self._clock.now(),
            )
            row.record_digest = _record_digest(row)
            if not _bound(session, row):
                raise _evidence_refused(
                    ADAPTIVE_SHADOW_BINDING_REFUSED, "the revision is not the run's own"
                )
            session.add(row)
            session.flush()
            window = self._window_of(session, run.supplier_key, run.shadow_bundle_key, frozen_at)
            if window is None:
                return None
            self._append(
                session,
                collection_run_id,
                window.window_id,
                EventKind.OUTCOME_RECORDED,
                outcome_state(verdict),
                verdict=verdict,
                revision_id=revision_id,
                actor=SYSTEM,
                correlation_id=correlation_id,
            )
            return window.window_id

    def _append(
        self,
        session: Session,
        collection_run_id: str,
        window_id: str,
        kind: EventKind,
        state: State,
        *,
        actor: str,
        correlation_id: str,
        verdict: RunVerdict | None = None,
        revision_id: str | None = None,
        resolution: Resolution | None = None,
        evidence_ref: str | None = None,
        adaptive_failed_closed: bool | None = None,
    ) -> None:
        seq = 1 + (
            session.scalar(
                select(func.max(LedgerEvent.seq)).where(
                    LedgerEvent.collection_run_id == collection_run_id
                )
            )
            or 0
        )
        session.add(
            LedgerEvent(
                event_id=str(uuid.uuid4()),
                collection_run_id=collection_run_id,
                seq=seq,
                window_id=window_id,
                kind=kind.value,
                count_as=state.count_as.value,
                cause=None if state.cause is None else state.cause.value,
                run_verdict=None if verdict is None else verdict.value,
                revision_id=revision_id,
                resolution=None if resolution is None else resolution.value,
                evidence_ref=evidence_ref,
                adaptive_failed_closed=adaptive_failed_closed,
                process_run_id=self._process_run_id,
                actor=actor,
                correlation_id=correlation_id,
                occurred_at=self._clock.now(),
            )
        )
        session.flush()

    # ============================================================== reads

    def record(self, collection_run_id: str) -> RawRecord:
        with self._db.read() as session:
            row = session.get(ShadowRecord, collection_run_id)
            if row is None:
                raise NotFoundError(ADAPTIVE_SHADOW_RECORD_NOT_FOUND, "no raw shadow record")
            bound = _bound(session, row)
            session.expunge(row)
        if not bound:
            raise ShadowEvidenceTampered(
                ADAPTIVE_SHADOW_TAMPERED, "a raw shadow record no longer binds its canonical run"
            )
        if _record_digest(row) != row.record_digest:
            raise ShadowEvidenceTampered(
                ADAPTIVE_SHADOW_TAMPERED, "a raw shadow record does not recompute"
            )
        return RawRecord(
            collection_run_id=row.collection_run_id,
            supplier_key=row.supplier_key,
            revision_id=row.revision_id,
            bundle_key=row.bundle_key,
            verdict=RunVerdict(row.run_verdict),
            severity=row.severity,
            comparison=json.loads(row.comparison_json),
            process_run_id=row.process_run_id,
            recorded_at=row.recorded_at,
        )

    def events(self, collection_run_id: str) -> tuple[Event, ...]:
        with self._db.read() as session:
            return self._events(session, collection_run_id)

    def _events(self, session: Session, collection_run_id: str) -> tuple[Event, ...]:
        rows = session.scalars(
            select(LedgerEvent)
            .where(LedgerEvent.collection_run_id == collection_run_id)
            .order_by(LedgerEvent.seq)
        ).all()
        return tuple(_event(row) for row in rows)

    def _window_events(
        self, session: Session, run: CollectionRun, window_id: str
    ) -> tuple[Event, ...]:
        """The ledger of a run eligible for this window. Every event must name this exact window:
        a run's evidence is never counted in, or rebound to, another window (B3)."""
        events = self._events(session, run.collection_run_id)
        if any(event.window_id != window_id for event in events):
            raise ShadowEvidenceTampered(
                ADAPTIVE_SHADOW_TAMPERED,
                "a run's ledger names a window other than the one it froze its eligibility for",
                details={"collection_run_id": run.collection_run_id},
            )
        return events

    def state(self, collection_run_id: str) -> State | None:
        """The run's effective state, or ``None`` when it has no ledger event (yet)."""
        events = self.events(collection_run_id)
        return None if not events else _fold(events)

    # ============================================================== resolution

    def resolve(
        self,
        collection_run_id: str,
        resolution: Resolution,
        *,
        evidence_ref: str,
        adaptive_failed_closed: bool,
        actor: str,
        correlation_id: str,
    ) -> State:
        """Record a human resolution of an unresolved mismatch, from source evidence (§10.3). It
        never edits a canonical revision and is never a ReviewItem."""
        if not evidence_ref.strip():
            raise _evidence_refused(
                ADAPTIVE_RESOLUTION_REFUSED, "a resolution names the evidence it used"
            )
        with self._db.write() as session:
            events = self._events(session, collection_run_id)
            if not events:
                raise _evidence_refused(
                    ADAPTIVE_RESOLUTION_REFUSED, "this run has no recorded outcome"
                )
            state = _fold(events)
            if state.terminal:
                raise _evidence_refused(
                    ADAPTIVE_RESOLUTION_REFUSED,
                    "only an UNRESOLVED_MISMATCH is resolved; this run is already settled",
                    cause=None if state.cause is None else state.cause.value,
                )
            if session.get(ShadowRecord, collection_run_id) is None:
                raise _evidence_refused(
                    ADAPTIVE_RESOLUTION_REFUSED, "the raw record a resolution needs is gone"
                )
            verdict = events[0].verdict
            assert verdict is not None
            resolved = resolution_state(
                verdict, resolution, adaptive_failed_closed=adaptive_failed_closed
            )
            window_id = events[0].window_id
            assert window_id is not None
            self._append(
                session,
                collection_run_id,
                window_id,
                EventKind.RESOLUTION_RECORDED,
                resolved,
                resolution=resolution,
                evidence_ref=evidence_ref,
                adaptive_failed_closed=adaptive_failed_closed,
                actor=actor,
                correlation_id=correlation_id,
            )
        return resolved

    # ============================================================== retention

    def prune(self) -> PruneReport:
        """Apply both raw bounds, whichever is reached first, with no hold (§10.5). Pruning an
        unresolved run appends ``RAW_PRUNED_UNRESOLVED`` in the same unit; pruning a terminal
        run's raw record appends nothing. Never touches a canonical row."""
        now = self._clock.now()
        cutoff = now - self._retention.max_age
        pruned = unresolved = 0
        with self._db.write() as session:
            expired = set(
                session.scalars(
                    select(ShadowRecord.collection_run_id).where(
                        ShadowRecord.recorded_at <= cutoff  # at most 90 days: the boundary goes
                    )
                )
            )
            suppliers = session.scalars(select(ShadowRecord.supplier_key).distinct()).all()
            for supplier_key in suppliers:
                expired.update(
                    session.scalars(
                        select(ShadowRecord.collection_run_id)
                        .where(ShadowRecord.supplier_key == supplier_key)
                        .order_by(
                            ShadowRecord.recorded_at.desc(), ShadowRecord.collection_run_id.desc()
                        )
                        .offset(self._retention.max_per_supplier)
                    )
                )
            for run_id in sorted(expired):
                events = self._events(session, run_id)
                if events and not _fold(events).terminal:
                    window_id = events[0].window_id
                    assert window_id is not None
                    self._append(
                        session,
                        run_id,
                        window_id,
                        EventKind.RAW_PRUNED_UNRESOLVED,
                        State(CountAs.INCOMPLETE, Cause.PRUNED_BEFORE_RESOLUTION),
                        actor=SYSTEM,
                        correlation_id=f"prune:{now.isoformat()}",
                    )
                    unresolved += 1
                row = session.get(ShadowRecord, run_id)
                if row is not None:
                    session.delete(row)
                    pruned += 1
        return PruneReport(pruned, unresolved)

    def retention_status(self, supplier_key: str) -> RetentionStatus:
        now = self._clock.now()
        with self._db.read() as session:
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ShadowRecord)
                    .where(ShadowRecord.supplier_key == supplier_key)
                )
                or 0
            )
            oldest = session.scalar(
                select(func.min(ShadowRecord.recorded_at)).where(
                    ShadowRecord.supplier_key == supplier_key
                )
            )
        headroom = max(0, self._retention.max_per_supplier - count)
        if oldest is None:
            return RetentionStatus(supplier_key, 0, headroom, None, None, None, None)
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=now.tzinfo)
        age_deadline = oldest + self._retention.max_age
        span = max(now - oldest, timedelta(days=1))
        # At the rate the supplier accrued records so far, when the count bound would be reached.
        count_deadline = now if headroom == 0 else now + span * (headroom / count)
        nearer = "COUNT" if count_deadline < age_deadline else "AGE"
        return RetentionStatus(
            supplier_key, count, headroom, oldest, age_deadline, count_deadline, nearer
        )

    # ============================================================== windows

    def declare(
        self,
        supplier_key: str,
        bundle_key: str,
        *,
        actor: str,
        reason: str,
        correlation_id: str,
        min_size: int = MIN_WINDOW_SIZE,
        supersedes: str | None = None,
    ) -> str:
        """Declare an evidence window before its first eligible collection (§11.1).

        Refused for an unregistered supplier, a bundle this running engine cannot execute, a
        bundle whose EPR is not a stored EPR of that supplier, while another window of the
        supplier is open, and — carry-forward item 1 — for a bundle that already holds a
        permanent blocking cause or a FAIL window. ``supersedes`` names the one closed window of
        the same bundle whose only incompleteness is ``SHADOW_MISSING_AFTER_RECOVERY`` (§11.2);
        the supersession is recorded in this same unit, before any eligible collection.
        """
        if not self._admits(supplier_key):
            raise _evidence_refused(
                ADAPTIVE_WINDOW_REFUSED, "a window exists only for a registered supplier"
            )
        epr_digest = running_bundle(bundle_key)
        if epr_digest is None:
            raise _evidence_refused(
                ADAPTIVE_WINDOW_REFUSED, "the bundle is not one this engine executes"
            )
        record = self._profiles.record(epr_digest)
        if record.kind != "EXTRACTION_PROFILE" or record.supplier_key != supplier_key:
            raise _evidence_refused(
                ADAPTIVE_WINDOW_REFUSED, "the bundle names an EPR of this supplier"
            )
        if isinstance(min_size, bool) or min_size < MIN_WINDOW_SIZE:
            raise _evidence_refused(
                ADAPTIVE_WINDOW_REFUSED, f"a window holds at least {MIN_WINDOW_SIZE}"
            )
        evidence = self.bundle_windows(bundle_key)
        if blocked := permanently_blocked(evidence):
            raise _evidence_refused(
                ADAPTIVE_BUNDLE_BLOCKED,
                "this exact bundle can never pass; only a content-different EPR starts fresh"
                " evidence",
                causes=list(blocked),
            )
        superseded: WindowEvidence | None = None
        if supersedes is not None:
            superseded = next((w for w in evidence if w.window_id == supersedes), None)
            if (
                superseded is None
                or not superseded.closed
                or superseded.superseded
                or not only_after_recovery(superseded.states.values())
            ):
                raise _evidence_refused(
                    ADAPTIVE_WINDOW_REFUSED,
                    "only a closed window of this bundle whose incompleteness is all"
                    " SHADOW_MISSING_AFTER_RECOVERY is superseded",
                )
        now = self._clock.now()
        window_id = str(uuid.uuid4())
        with self._db.write() as session:
            if self._open_window(session, supplier_key) is not None:
                raise _evidence_refused(
                    ADAPTIVE_WINDOW_REFUSED, "another window of this supplier is open"
                )
            if superseded is not None:
                self._window_event(
                    session,
                    superseded.window_id,
                    "SUPERSEDED",
                    actor=actor,
                    reason="SHADOW_MISSING_AFTER_RECOVERY",
                    detail={
                        "superseded_by": window_id,
                        "reason": "SHADOW_MISSING_AFTER_RECOVERY",
                        "runs": sorted(superseded.states),
                    },
                )
            session.add(
                EvidenceWindow(
                    window_id=window_id,
                    supplier_key=supplier_key,
                    bundle_key=bundle_key,
                    min_size=min_size,
                    declared_by=actor,
                    correlation_id=correlation_id,
                    declared_at=now,
                )
            )
            session.flush()
            self._window_event(session, window_id, "DECLARED", actor=actor, reason=reason)
        return window_id

    def end(self, window_id: str, *, actor: str, reason: str) -> None:
        """Stop eligibility. Reconciles the window's missing outcomes first (item 3)."""
        window = self.window(window_id)
        if window.ended_at is not None:
            raise _evidence_refused(ADAPTIVE_WINDOW_REFUSED, "this window has already ended")
        self.reconcile(window_id)
        with self._db.write() as session:
            self._window_event(session, window_id, "ENDED", actor=actor, reason=reason)

    def close(self, window_id: str, *, actor: str, reason: str) -> dict[str, object]:
        """The final, immutable closeout (§11.1). Reconciles first; refused while any run of the
        window is still in flight or still ``UNRESOLVED_MISMATCH``."""
        window = self.window(window_id)
        if window.ended_at is None or window.closed:
            raise _evidence_refused(
                ADAPTIVE_WINDOW_REFUSED, "only an ended, unclosed window is closed"
            )
        self.reconcile(window_id)
        with self._db.write() as session:
            if self._in_flight(session, window):
                raise _evidence_refused(
                    ADAPTIVE_WINDOW_REFUSED, "a run of this window is still in flight"
                )
            runs = self._eligible(session, window)
            states: dict[str, tuple[State, int]] = {}
            for run in runs:
                events = self._window_events(session, run, window.window_id)
                if not events:
                    raise _evidence_refused(
                        ADAPTIVE_WINDOW_REFUSED, "a run of this window has no outcome"
                    )
                state = _fold(events)
                if not state.terminal:
                    raise _evidence_refused(
                        ADAPTIVE_WINDOW_REFUSED,
                        "a window holding an UNRESOLVED_MISMATCH can end but cannot close",
                    )
                states[run.collection_run_id] = (state, events[-1].seq)
            restart = self._restart_seen(session, states)
            verdict = window_verdict(
                (s for s, _ in states.values()), min_size=window.min_size, restart_seen=restart
            )
            closeout: dict[str, object] = {
                "verdict": verdict.value,
                "denominator": len(states),
                "min_size": window.min_size,
                "restart_seen": restart,
                "runs": [
                    {
                        "collection_run_id": run_id,
                        "count_as": state.count_as.value,
                        "cause": None if state.cause is None else state.cause.value,
                        "last_seq": last,
                    }
                    for run_id, (state, last) in sorted(states.items())
                ],
            }
            self._window_event(
                session, window_id, "CLOSED", actor=actor, reason=reason, detail=closeout
            )
        return closeout

    def _window_event(
        self,
        session: Session,
        window_id: str,
        kind: str,
        *,
        actor: str,
        reason: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        seq = 1 + (
            session.scalar(
                select(func.max(EvidenceWindowEvent.seq)).where(
                    EvidenceWindowEvent.window_id == window_id
                )
            )
            or 0
        )
        session.add(
            EvidenceWindowEvent(
                window_id=window_id,
                seq=seq,
                kind=kind,
                actor=actor,
                reason=reason,
                detail_json=None if detail is None else canonical_json(detail),
                occurred_at=self._clock.now(),
            )
        )
        session.flush()

    # -------------------------------------------------------------- window reads

    def window(self, window_id: str) -> WindowRecord:
        with self._db.read() as session:
            return self._window_record(session, window_id)

    def windows(self, supplier_key: str) -> tuple[WindowRecord, ...]:
        with self._db.read() as session:
            ids = session.scalars(
                select(EvidenceWindow.window_id)
                .where(EvidenceWindow.supplier_key == supplier_key)
                .order_by(EvidenceWindow.declared_at, EvidenceWindow.window_id)
            ).all()
            return tuple(self._window_record(session, window_id) for window_id in ids)

    def _window_record(self, session: Session, window_id: str) -> WindowRecord:
        row = session.get(EvidenceWindow, window_id)
        if row is None:
            raise NotFoundError(ADAPTIVE_WINDOW_NOT_FOUND, "no such evidence window")
        events = session.scalars(
            select(EvidenceWindowEvent)
            .where(EvidenceWindowEvent.window_id == window_id)
            .order_by(EvidenceWindowEvent.seq)
        ).all()
        kinds = tuple(event.kind for event in events)
        ended = next((e.occurred_at for e in events if e.kind == "ENDED"), None)
        closed = next((e for e in events if e.kind == "CLOSED"), None)
        return WindowRecord(
            window_id=row.window_id,
            supplier_key=row.supplier_key,
            bundle_key=row.bundle_key,
            min_size=row.min_size,
            declared_at=row.declared_at,
            ended_at=ended,
            closed=closed is not None,
            superseded="SUPERSEDED" in kinds,
            events=kinds,
            closeout=None
            if closed is None or closed.detail_json is None
            else json.loads(closed.detail_json),
        )

    def _open_window(self, session: Session, supplier_key: str) -> str | None:
        for window_id in session.scalars(
            select(EvidenceWindow.window_id).where(EvidenceWindow.supplier_key == supplier_key)
        ):
            last = session.scalar(
                select(EvidenceWindowEvent.kind)
                .where(EvidenceWindowEvent.window_id == window_id)
                .order_by(EvidenceWindowEvent.seq.desc())
                .limit(1)
            )
            if last == "DECLARED":
                return window_id
        return None

    def _window_of(
        self, session: Session, supplier_key: str, bundle_key: str, first_read_at: datetime
    ) -> WindowRecord | None:
        """The window a run with this frozen bundle and first reservation belongs to, if any."""
        for window_id in session.scalars(
            select(EvidenceWindow.window_id).where(
                EvidenceWindow.supplier_key == supplier_key,
                EvidenceWindow.bundle_key == bundle_key,
                EvidenceWindow.declared_at <= first_read_at,
            )
        ):
            window = self._window_record(session, window_id)
            if not window.closed and (window.ended_at is None or first_read_at < window.ended_at):
                return window
        return None

    def _members(self, session: Session, window: WindowRecord) -> list[CollectionRun]:
        """Every canonical run whose frozen decision puts it in this window (any outcome)."""
        clauses = [
            CollectionRun.supplier_key == window.supplier_key,
            CollectionRun.shadow_decision == "ENABLED",
            CollectionRun.shadow_bundle_key == window.bundle_key,
            CollectionRun.first_product_read_at >= window.declared_at,
        ]
        if window.ended_at is not None:
            clauses.append(CollectionRun.first_product_read_at < window.ended_at)
        return list(
            session.scalars(
                select(CollectionRun)
                .where(and_(*clauses))
                .order_by(CollectionRun.first_product_read_at, CollectionRun.collection_run_id)
            )
        )

    def _eligible(self, session: Session, window: WindowRecord) -> list[CollectionRun]:
        """§11.1: terminal ``RECORDED`` or ``NO_REVISION``, frozen ENABLED for this exact bundle,
        first reservation inside the window. Derived from the canonical runs alone."""
        return [
            run
            for run in self._members(session, window)
            if run.outcome in {o.value for o in ELIGIBLE_OUTCOMES}
        ]

    def _in_flight(self, session: Session, window: WindowRecord) -> bool:
        return any(
            run.outcome == CollectionOutcome.PENDING.value for run in self._members(session, window)
        )

    def _restart_seen(self, session: Session, runs: Iterable[str]) -> bool:
        """The fresh-session condition (CLAUDE.md §9): the window's recorded outcomes came from at
        least two application processes."""
        processes = set(
            session.scalars(
                select(LedgerEvent.process_run_id).where(
                    LedgerEvent.collection_run_id.in_(list(runs)),
                    LedgerEvent.kind == EventKind.OUTCOME_RECORDED.value,
                    LedgerEvent.run_verdict.is_not(None),
                )
            )
        )
        return len(processes) >= 2

    # -------------------------------------------------------------- reconciliation (§11.2, item 3)

    def reconcile(self, window_id: str | None = None) -> int:
        """Append the missing ``OUTCOME_RECORDED`` of every eligible run of the given window, or
        of every window that is not closed. Idempotent: a run that has one is left as it is. Each
        appended outcome is its own write unit. Never refetches anything."""
        with self._db.read() as session:
            if window_id is not None:
                windows = [self._window_record(session, window_id)]
            else:
                ids = session.scalars(select(EvidenceWindow.window_id)).all()
                windows = [self._window_record(session, w) for w in ids]
            missing: list[tuple[str, str, Cause]] = []
            for window in windows:
                if window.closed:
                    continue
                for run in self._eligible(session, window):
                    if self._window_events(session, run, window.window_id):
                        continue
                    cause = (
                        Cause.SHADOW_MISSING_AFTER_RECOVERY
                        if run.outcome == CollectionOutcome.RECORDED.value
                        and run.settled_by_recovery
                        else Cause.SHADOW_MISSING
                    )
                    missing.append((run.collection_run_id, window.window_id, cause))
        appended = 0
        for run_id, target, cause in missing:
            with self._db.write() as session:
                if self._events(session, run_id):
                    continue  # another reconciliation got there first
                self._append(
                    session,
                    run_id,
                    target,
                    EventKind.OUTCOME_RECORDED,
                    outcome_state(None, cause),
                    actor=SYSTEM,
                    correlation_id=f"reconcile:{run_id}",
                )
                appended += 1
        if appended:
            logger.info("collect.shadow_reconciled", extra={"outcomes": appended})
        return appended

    # -------------------------------------------------------------- verdicts

    def window_evidence(self, window_id: str) -> WindowEvidence:
        with self._db.read() as session:
            return self._window_evidence(session, self._window_record(session, window_id))

    def _window_evidence(self, session: Session, window: WindowRecord) -> WindowEvidence:
        states: dict[str, State] = {}
        for run in self._eligible(session, window):
            events = self._window_events(session, run, window.window_id)
            if events:
                states[run.collection_run_id] = _fold(events)
            else:
                # Not reconciled yet: read exactly what reconciliation would append.
                cause = (
                    Cause.SHADOW_MISSING_AFTER_RECOVERY
                    if run.outcome == CollectionOutcome.RECORDED.value and run.settled_by_recovery
                    else Cause.SHADOW_MISSING
                )
                states[run.collection_run_id] = outcome_state(None, cause)
        if window.closeout is not None:
            recorded = {
                entry["collection_run_id"]: State(
                    CountAs(entry["count_as"]),
                    None if entry["cause"] is None else Cause(entry["cause"]),
                )
                for entry in window.closeout["runs"]  # type: ignore[attr-defined]
            }
            if recorded != states:
                raise ShadowEvidenceTampered(
                    ADAPTIVE_SHADOW_TAMPERED, "a closeout disagrees with its runs' ledger"
                )
            verdict = WindowVerdict(str(window.closeout["verdict"]))
        else:
            verdict = window_verdict(
                states.values(),
                min_size=window.min_size,
                restart_seen=self._restart_seen(session, states),
            )
        return WindowEvidence(
            window_id=window.window_id,
            states=states,
            verdict=verdict,
            ended=window.ended_at is not None,
            closed=window.closed,
            superseded=window.superseded,
            min_size=window.min_size,
        )

    def bundle_windows(self, bundle_key: str) -> tuple[WindowEvidence, ...]:
        """Every window ever declared for this exact bundle, superseded ones included."""
        with self._db.read() as session:
            ids = session.scalars(
                select(EvidenceWindow.window_id)
                .where(EvidenceWindow.bundle_key == bundle_key)
                .order_by(EvidenceWindow.declared_at, EvidenceWindow.window_id)
            ).all()
            return tuple(
                self._window_evidence(session, self._window_record(session, window_id))
                for window_id in ids
            )

    def bundle(self, bundle_key: str) -> BundleEvidence:
        return bundle_verdict(self.bundle_windows(bundle_key))

    # -------------------------------------------------------------- startup

    def on_startup(self) -> tuple[int, PruneReport]:
        """The startup pass: reconcile every open window's missing outcomes, then apply the raw
        bounds. Both are idempotent."""
        return self.reconcile(), self.prune()
