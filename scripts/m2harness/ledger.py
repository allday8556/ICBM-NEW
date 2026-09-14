"""The durable M2 campaign ledger (Issue #46 §2.1; docs/acceptance/M2.md §5.4, §6).

One SQLite file per campaign, shared by the baseline and the crash data directory and by every
process of the campaign. It is the only thing that lets a SmartStore request reach a transport:

* a request is checked and durably reserved (committed with ``synchronous=FULL``) before any
  transport receives it. The response only completes the reservation and never consumes budget;
* a request that would exceed its endpoint's hard cap (token 8, seller account 6, anything else
  0) is refused before send, and the campaign becomes ``BUDGET_EXHAUSTED``;
* a request is accepted only while an approved run is ``RUNNING`` and one of the requesting
  process's phases is open, and only as an unused step of that phase's frozen plan, so nothing
  exploratory can be sent;
* the crash boundary has its own sub-cap of two attempts, T4a and T4b. There is no third;
* every table is append-only. Triggers refuse DELETE and any rewrite of a reservation, and they
  enforce the caps, the approved-run requirement and the crash sub-cap a second time, so neither
  a code path nor a stray SQL statement can reset or exceed the budget of a campaign ID.

The ledger holds no secret: endpoint ids, step labels, HTTP status codes, times and digests.
"""

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from integrations.marketplaces.smartstore.registry import EndpointId

TOKEN = EndpointId.SMARTSTORE_AUTH_TOKEN.value
SELLER = EndpointId.SMARTSTORE_SELLER_ACCOUNT.value
UNRECOGNIZED = "UNRECOGNIZED"
# M2.md §6: independent hard caps, never a pool. Every other endpoint is capped at 0.
CAPS: Mapping[str, int] = {TOKEN: 8, SELLER: 6}
# M2.md §5.4: T4a plus at most one T4b. Unused token capacity never buys a third attempt.
CRASH_ATTEMPT_CAP = 2
SCHEMA_VERSION = 1
CAMPAIGN_ID = re.compile(r"^m2-[a-z0-9][a-z0-9-]{2,40}$")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Mode(StrEnum):
    REAL = "REAL"
    DRY = "DRY"


class State(StrEnum):
    INITIALIZED = "INITIALIZED"
    # Issue #46 comment 5669037896: the durable stop after the zero-provider preflight. Only an
    # approved run leaves it; nothing falls through from preflight into T1.
    AWAITING_REAL_PROVIDER_APPROVAL = "AWAITING_REAL_PROVIDER_APPROVAL"
    RUNNING = "RUNNING"
    STOPPED_STEP_FAILED = "STOPPED_STEP_FAILED"
    STOPPED_INTERRUPTED = "STOPPED_INTERRUPTED"
    COMPLETED = "COMPLETED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    STOPPED_FOR_CONTRACT_REVIEW = "STOPPED_FOR_CONTRACT_REVIEW"
    STOPPED_OPERATOR_DECLINED = "STOPPED_OPERATOR_DECLINED"
    FAILED = "FAILED"


TERMINAL = frozenset(
    {
        State.COMPLETED,
        State.BUDGET_EXHAUSTED,
        State.STOPPED_FOR_CONTRACT_REVIEW,
        State.STOPPED_OPERATOR_DECLINED,
        State.FAILED,
    }
)
# A stopped run may continue only through a new, explicitly approved invocation.
RESUMABLE = frozenset({State.STOPPED_STEP_FAILED, State.STOPPED_INTERRUPTED})
_TRANSITIONS: Mapping[State, frozenset[State]] = {
    State.INITIALIZED: frozenset({State.AWAITING_REAL_PROVIDER_APPROVAL}),
    State.AWAITING_REAL_PROVIDER_APPROVAL: frozenset(
        {State.AWAITING_REAL_PROVIDER_APPROVAL, State.RUNNING}
    ),
    State.RUNNING: TERMINAL | RESUMABLE,
    State.STOPPED_STEP_FAILED: frozenset({State.RUNNING}),
    State.STOPPED_INTERRUPTED: frozenset({State.RUNNING}),
}


class Phase(StrEnum):
    BASELINE_CONNECT = "BASELINE_CONNECT"
    BASELINE_BIND = "BASELINE_BIND"
    BASELINE_RESTART = "BASELINE_RESTART"
    CRASH_T4A = "CRASH_T4A"
    CRASH_T4B = "CRASH_T4B"
    CRASH_RECOVERY = "CRASH_RECOVERY"


# M2.md §6.1, frozen: the steps each phase may send, in order. A step may be skipped (a committed
# session is reused), never repeated or reordered within one phase attempt.
PLANS: Mapping[Phase, tuple[tuple[str, str], ...]] = {
    Phase.BASELINE_CONNECT: (("T1", TOKEN), ("A1", SELLER)),
    Phase.BASELINE_BIND: (("A1b", SELLER),),
    Phase.BASELINE_RESTART: (("A2", SELLER),),
    Phase.CRASH_T4A: (("T4a", TOKEN),),
    Phase.CRASH_T4B: (("T4b", TOKEN),),
    Phase.CRASH_RECOVERY: (("T5", TOKEN), ("A3", SELLER)),
}
LABELS = tuple(label for plan in PLANS.values() for label, _ in plan)
# The crash-boundary attempt each crash phase is. There is no phase for a third attempt.
CRASH_ATTEMPTS: Mapping[Phase, tuple[int, str]] = {
    Phase.CRASH_T4A: (1, "T4a"),
    Phase.CRASH_T4B: (2, "T4b"),
}
PHASE_ROLES: Mapping[Phase, str] = {
    Phase.BASELINE_CONNECT: "baseline",
    Phase.BASELINE_BIND: "baseline",
    Phase.BASELINE_RESTART: "baseline",
    Phase.CRASH_T4A: "crash",
    Phase.CRASH_T4B: "crash",
    Phase.CRASH_RECOVERY: "crash",
}


class Refusal(StrEnum):
    NOT_RUNNING = "NOT_RUNNING"
    FORBIDDEN_TARGET = "FORBIDDEN_TARGET"
    NO_OPEN_PHASE = "NO_OPEN_PHASE"
    CAP_REACHED = "CAP_REACHED"
    UNPLANNED_REQUEST = "UNPLANNED_REQUEST"
    CRASH_SUB_BUDGET_EXHAUSTED = "CRASH_SUB_BUDGET_EXHAUSTED"


# The next request would exceed an endpoint cap (0 for anything but the two adopted endpoints).
BUDGET_REFUSALS = frozenset({Refusal.FORBIDDEN_TARGET, Refusal.CAP_REACHED})


class CrashVerdict(StrEnum):
    BOUNDARY_EXERCISED = "BOUNDARY_EXERCISED"
    BOUNDARY_NOT_EXERCISED = "BOUNDARY_NOT_EXERCISED"
    COMMITTED_BEFORE_CRASH = "COMMITTED_BEFORE_CRASH"
    CANDIDATE_LEAKED = "CANDIDATE_LEAKED"


_TERMINAL_SQL = ", ".join(f"'{state.value}'" for state in sorted(TERMINAL))
_CRASH_PHASES_SQL = ", ".join(f"'{phase.value}'" for phase in CRASH_ATTEMPTS)
_NOT_EXERCISED = CrashVerdict.BOUNDARY_NOT_EXERCISED.value

_SCHEMA = f"""
CREATE TABLE meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    campaign_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('REAL', 'DRY')),
    nonce TEXT NOT NULL,
    created_at TEXT NOT NULL,
    token_cap INTEGER NOT NULL CHECK (token_cap = {CAPS[TOKEN]}),
    seller_cap INTEGER NOT NULL CHECK (seller_cap = {CAPS[SELLER]}),
    crash_attempt_cap INTEGER NOT NULL CHECK (crash_attempt_cap = {CRASH_ATTEMPT_CAP}),
    state TEXT NOT NULL,
    outcome TEXT
);
CREATE TRIGGER meta_append_only BEFORE DELETE ON meta
BEGIN SELECT RAISE(ABORT, 'the campaign ledger is append-only'); END;
CREATE TRIGGER meta_identity_fixed
BEFORE UPDATE OF singleton, schema_version, campaign_id, mode, nonce, created_at, token_cap,
    seller_cap, crash_attempt_cap ON meta
BEGIN SELECT RAISE(ABORT, 'the campaign identity and its caps are immutable'); END;
CREATE TRIGGER meta_terminal_final BEFORE UPDATE OF state, outcome ON meta
WHEN OLD.state IN ({_TERMINAL_SQL})
BEGIN SELECT RAISE(ABORT, 'a terminal campaign state is final'); END;

CREATE TABLE requests (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id TEXT NOT NULL CHECK (endpoint_id IN ('{TOKEN}', '{SELLER}')),
    label TEXT NOT NULL,
    phase TEXT NOT NULL,
    phase_attempt INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    reserved_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'RESERVED'
        CHECK (outcome IN ('RESERVED', 'RESPONDED', 'NO_RESPONSE')),
    http_status INTEGER,
    latency_ms REAL,
    finished_at TEXT
);
CREATE TRIGGER requests_append_only BEFORE DELETE ON requests
BEGIN SELECT RAISE(ABORT, 'spent budget is never erased'); END;
CREATE TRIGGER requests_reservation_fixed
BEFORE UPDATE OF seq, endpoint_id, label, phase, phase_attempt, pid, reserved_at ON requests
BEGIN SELECT RAISE(ABORT, 'a reservation is immutable'); END;
CREATE TRIGGER requests_outcome_once
BEFORE UPDATE OF outcome, http_status, latency_ms, finished_at ON requests
WHEN OLD.outcome <> 'RESERVED'
BEGIN SELECT RAISE(ABORT, 'a request outcome is recorded once'); END;
CREATE TRIGGER requests_need_a_run BEFORE INSERT ON requests
WHEN (SELECT state FROM meta) <> 'RUNNING'
BEGIN SELECT RAISE(ABORT, 'no approved run is in progress'); END;
CREATE TRIGGER requests_token_cap BEFORE INSERT ON requests
WHEN NEW.endpoint_id = '{TOKEN}'
    AND (SELECT count(*) FROM requests WHERE endpoint_id = '{TOKEN}')
        >= (SELECT token_cap FROM meta)
BEGIN SELECT RAISE(ABORT, 'the token cap is reached'); END;
CREATE TRIGGER requests_seller_cap BEFORE INSERT ON requests
WHEN NEW.endpoint_id = '{SELLER}'
    AND (SELECT count(*) FROM requests WHERE endpoint_id = '{SELLER}')
        >= (SELECT seller_cap FROM meta)
BEGIN SELECT RAISE(ABORT, 'the seller-account cap is reached'); END;
CREATE TRIGGER requests_crash_attempt_single BEFORE INSERT ON requests
WHEN NEW.phase IN ({_CRASH_PHASES_SQL})
    AND (NEW.endpoint_id <> '{TOKEN}'
        OR (SELECT count(*) FROM requests WHERE phase = NEW.phase) > 0)
BEGIN SELECT RAISE(ABORT, 'a crash-boundary attempt is exactly one token request'); END;

CREATE TABLE refusals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    pid INTEGER NOT NULL,
    endpoint_id TEXT NOT NULL,
    phase TEXT,
    reason TEXT NOT NULL
);
CREATE TRIGGER refusals_append_only BEFORE DELETE ON refusals
BEGIN SELECT RAISE(ABORT, 'a refusal is never erased'); END;
CREATE TRIGGER refusals_fixed BEFORE UPDATE ON refusals
BEGIN SELECT RAISE(ABORT, 'a refusal is immutable'); END;

CREATE TABLE phases (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    phase TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    result TEXT,
    UNIQUE (phase, attempt)
);
CREATE TRIGGER phases_append_only BEFORE DELETE ON phases
BEGIN SELECT RAISE(ABORT, 'a phase is never erased'); END;
CREATE TRIGGER phases_fixed BEFORE UPDATE OF seq, phase, attempt, opened_at ON phases
BEGIN SELECT RAISE(ABORT, 'a phase record is immutable'); END;
CREATE TRIGGER phases_close_once BEFORE UPDATE OF closed_at, result ON phases
WHEN OLD.closed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a phase closes once'); END;
CREATE TRIGGER phases_crash_once BEFORE INSERT ON phases
WHEN NEW.phase IN ({_CRASH_PHASES_SQL}) AND NEW.attempt <> 1
BEGIN SELECT RAISE(ABORT, 'a crash-boundary phase runs once'); END;

CREATE TABLE crash_attempts (
    attempt INTEGER PRIMARY KEY CHECK (attempt BETWEEN 1 AND {CRASH_ATTEMPT_CAP}),
    label TEXT NOT NULL UNIQUE
        CHECK ((attempt = 1 AND label = 'T4a') OR (attempt = 2 AND label = 'T4b')),
    opened_at TEXT NOT NULL,
    verdict TEXT,
    detail TEXT
);
CREATE TRIGGER crash_attempts_append_only BEFORE DELETE ON crash_attempts
BEGIN SELECT RAISE(ABORT, 'a crash attempt is never erased'); END;
CREATE TRIGGER crash_attempts_fixed BEFORE UPDATE OF attempt, label, opened_at ON crash_attempts
BEGIN SELECT RAISE(ABORT, 'a crash attempt is immutable'); END;
CREATE TRIGGER crash_attempts_verdict_once BEFORE UPDATE OF verdict, detail ON crash_attempts
WHEN OLD.verdict IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'a crash verdict is recorded once'); END;
CREATE TRIGGER crash_retry_only_after_a_miss BEFORE INSERT ON crash_attempts
WHEN NEW.attempt = 2
    AND COALESCE((SELECT verdict FROM crash_attempts WHERE attempt = 1), '') <> '{_NOT_EXERCISED}'
BEGIN SELECT RAISE(ABORT, 'T4b is allowed only after T4a missed the boundary'); END;

CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TRIGGER events_append_only BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'an event is never erased'); END;
CREATE TRIGGER events_fixed BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'an event is immutable'); END;
"""

EXPECTED_TRIGGERS = frozenset(re.findall(r"CREATE TRIGGER (\w+)", _SCHEMA))
_TABLES = frozenset({"requests", "refusals", "phases", "crash_attempts", "events"})


class LedgerError(RuntimeError):
    """The ledger is missing, unreadable or altered, or was asked for something it forbids."""


class ReservationRefused(Exception):
    """The request was refused before any transport received it; the refusal is recorded."""

    def __init__(self, reason: Refusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    mode: Mode
    nonce: str
    created_at: str
    state: State
    outcome: str | None


@dataclass(frozen=True)
class Reservation:
    seq: int
    endpoint_id: str
    label: str
    phase: Phase
    attempt: int


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    # Durable at COMMIT: a reservation is on disk before the request reaches a transport.
    db.execute("PRAGMA synchronous=FULL")
    return db


def _event(db: sqlite3.Connection, kind: str, detail: Mapping[str, object]) -> None:
    db.execute(
        "INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)",
        (now(), kind, json.dumps(dict(detail), sort_keys=True)),
    )


def _meta(db: sqlite3.Connection) -> sqlite3.Row:
    row = db.execute("SELECT * FROM meta").fetchone()
    if row is None:
        raise LedgerError("the campaign ledger has no campaign")
    return row  # type: ignore[no-any-return]


def _set_state(db: sqlite3.Connection, target: State, *, reason: str) -> None:
    current = State(_meta(db)["state"])
    if target not in _TRANSITIONS.get(current, frozenset()):
        raise LedgerError(f"the campaign cannot move from {current} to {target}")
    outcome = target.value if target in TERMINAL else None
    db.execute("UPDATE meta SET state = ?, outcome = ?", (target.value, outcome))
    _event(db, "STATE", {"from": current.value, "to": target.value, "reason": reason})


def _next_label(db: sqlite3.Connection, phase: Phase, attempt: int, endpoint_id: str) -> str | None:
    plan = PLANS[phase]
    used = {
        row["label"]
        for row in db.execute(
            "SELECT label FROM requests WHERE phase = ? AND phase_attempt = ?",
            (phase.value, attempt),
        )
    }
    position = max((i + 1 for i, (label, _) in enumerate(plan) if label in used), default=0)
    return next((label for label, endpoint in plan[position:] if endpoint == endpoint_id), None)


class Ledger:
    """The campaign ledger at one path. Every method runs its own short transaction, so the
    harness and the application processes of the campaign can share the file safely."""

    def __init__(self, path: Path) -> None:
        self.path = path

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, path: Path, *, campaign_id: str, mode: Mode, nonce: str) -> "Ledger":
        if not CAMPAIGN_ID.fullmatch(campaign_id):
            raise LedgerError("a campaign id is m2- followed by lowercase letters, digits, hyphens")
        if path.exists():
            raise LedgerError("a ledger already exists here: a campaign's budget is never reset")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(_connect(path)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.executescript(_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO meta VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    SCHEMA_VERSION,
                    campaign_id,
                    mode.value,
                    nonce,
                    now(),
                    CAPS[TOKEN],
                    CAPS[SELLER],
                    CRASH_ATTEMPT_CAP,
                    State.INITIALIZED.value,
                ),
            )
            _event(db, "CAMPAIGN_CREATED", {"campaign_id": campaign_id, "mode": mode.value})
            db.execute("COMMIT")
        return cls.open(path)

    @classmethod
    def open(cls, path: Path) -> "Ledger":
        if not path.is_file():
            raise LedgerError("there is no campaign ledger at this path")
        ledger = cls(path)
        ledger.check()
        return ledger

    def check(self) -> None:
        """Refuse a ledger that is corrupt, altered or over its caps."""
        try:
            with closing(_connect(self.path)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise LedgerError("the campaign ledger fails its integrity check")
                triggers = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type = ?", ("trigger",)
                    )
                }
                if triggers != EXPECTED_TRIGGERS:
                    raise LedgerError("the campaign ledger's guards were altered")
                meta = _meta(db)
                caps = (meta["token_cap"], meta["seller_cap"], meta["crash_attempt_cap"])
                if meta["schema_version"] != SCHEMA_VERSION or caps != (
                    CAPS[TOKEN],
                    CAPS[SELLER],
                    CRASH_ATTEMPT_CAP,
                ):
                    raise LedgerError("the campaign ledger has an unexpected schema or caps")
                for endpoint, cap in CAPS.items():
                    used = db.execute(
                        "SELECT count(*) FROM requests WHERE endpoint_id = ?", (endpoint,)
                    ).fetchone()[0]
                    if used > cap:
                        raise LedgerError("the campaign ledger records more requests than a cap")
        except sqlite3.DatabaseError as exc:
            raise LedgerError("the campaign ledger is unreadable") from exc

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with closing(_connect(self.path)) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except sqlite3.IntegrityError as exc:
                db.execute("ROLLBACK")
                raise LedgerError(str(exc)) from exc
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # ------------------------------------------------------------------ the budget gate

    def reserve(self, endpoint_id: str, *, phases: frozenset[Phase], pid: int) -> Reservation:
        """Check and durably reserve one request before any transport receives it.

        ``phases`` are the phases the requesting process was started for. On refusal the refusal
        is committed first, then ``ReservationRefused`` is raised; nothing was sent.
        """
        at = now()
        reservation: Reservation | None = None
        refusal: Refusal | None = None
        with self._tx() as db:
            state = State(_meta(db)["state"])
            open_rows = db.execute(
                "SELECT phase, attempt FROM phases WHERE closed_at IS NULL"
            ).fetchall()
            open_phase = Phase(open_rows[0]["phase"]) if len(open_rows) == 1 else None
            attempt = int(open_rows[0]["attempt"]) if len(open_rows) == 1 else 0
            label: str | None = None
            if state is not State.RUNNING:
                refusal = Refusal.NOT_RUNNING
            elif endpoint_id not in CAPS:
                refusal = Refusal.FORBIDDEN_TARGET
            elif open_phase is None or open_phase not in phases:
                refusal = Refusal.NO_OPEN_PHASE
            elif (
                db.execute(
                    "SELECT count(*) FROM requests WHERE endpoint_id = ?", (endpoint_id,)
                ).fetchone()[0]
                >= CAPS[endpoint_id]
            ):
                refusal = Refusal.CAP_REACHED
            else:
                label = _next_label(db, open_phase, attempt, endpoint_id)
                if label is None:
                    refusal = Refusal.UNPLANNED_REQUEST
            if refusal is None:
                assert open_phase is not None and label is not None
                cursor = db.execute(
                    "INSERT INTO requests (endpoint_id, label, phase, phase_attempt, pid,"
                    " reserved_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (endpoint_id, label, open_phase.value, attempt, pid, at),
                )
                assert cursor.lastrowid is not None
                reservation = Reservation(cursor.lastrowid, endpoint_id, label, open_phase, attempt)
            else:
                db.execute(
                    "INSERT INTO refusals (at, pid, endpoint_id, phase, reason)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (at, pid, endpoint_id, open_phase.value if open_phase else None, refusal),
                )
                if refusal in BUDGET_REFUSALS and state is State.RUNNING:
                    _set_state(db, State.BUDGET_EXHAUSTED, reason=refusal.value)
        if refusal is not None:
            raise ReservationRefused(refusal)
        assert reservation is not None
        return reservation

    def complete(self, seq: int, *, http_status: int | None, latency_ms: float) -> None:
        """Record what became of a reserved request: a response's status, or no response."""
        outcome = "RESPONDED" if http_status is not None else "NO_RESPONSE"
        with self._tx() as db:
            db.execute(
                "UPDATE requests SET outcome = ?, http_status = ?, latency_ms = ?, finished_at = ?"
                " WHERE seq = ? AND outcome = 'RESERVED'",
                (outcome, http_status, round(latency_ms, 1), now(), seq),
            )

    # ------------------------------------------------------------------ the campaign state

    def mark_preflight_passed(self, digest: str, head: str) -> None:
        """Enter the approval STOP. Only a campaign that has sent nothing can reach it."""
        with self._tx() as db:
            state = State(_meta(db)["state"])
            if state not in (State.INITIALIZED, State.AWAITING_REAL_PROVIDER_APPROVAL):
                raise LedgerError(f"preflight cannot pass in state {state}")
            if db.execute("SELECT count(*) FROM requests").fetchone()[0]:
                raise LedgerError("preflight passes only while no provider request was sent")
            _event(db, "PREFLIGHT_PASSED", {"digest": digest, "head": head})
            _set_state(db, State.AWAITING_REAL_PROVIDER_APPROVAL, reason="PREFLIGHT_PASSED")

    def issue_approval(self, digest: str, *, approved_sha: str) -> None:
        with self._tx() as db:
            meta = _meta(db)
            state = State(meta["state"])
            if Mode(meta["mode"]) is not Mode.REAL:
                raise LedgerError("only a REAL campaign takes real-provider approval")
            if state is not State.AWAITING_REAL_PROVIDER_APPROVAL and state not in RESUMABLE:
                raise LedgerError(f"no approval can be issued in state {state}")
            _event(db, "APPROVAL_ISSUED", {"digest": digest, "approved_sha": approved_sha})

    def begin_real_run(self, approval_digest: str) -> None:
        """Consume the current approval and start a run. The approval must be the ledger's latest
        event: one issued before anything else happened (a new preflight, another approval, an
        earlier run) is stale, and a consumed one never counts twice."""
        with self._tx() as db:
            meta = _meta(db)
            state = State(meta["state"])
            if Mode(meta["mode"]) is not Mode.REAL:
                raise LedgerError("only a REAL campaign runs against the provider")
            if state is not State.AWAITING_REAL_PROVIDER_APPROVAL and state not in RESUMABLE:
                raise LedgerError(f"no run can start in state {state}")
            latest = db.execute(
                "SELECT kind, detail FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if latest["kind"] != "APPROVAL_ISSUED" or (
                json.loads(latest["detail"]).get("digest") != approval_digest
            ):
                raise LedgerError("no current approval: it is missing, stale or already used")
            _event(db, "APPROVAL_CONSUMED", {"digest": approval_digest})
            _set_state(db, State.RUNNING, reason="REAL_PROVIDER_APPROVED")

    def begin_dry_run(self) -> None:
        """Start a DRY rehearsal. A REAL ledger never takes this path."""
        with self._tx() as db:
            meta = _meta(db)
            state = State(meta["state"])
            if Mode(meta["mode"]) is not Mode.DRY:
                raise LedgerError("a REAL campaign starts only through real-provider approval")
            if state is not State.AWAITING_REAL_PROVIDER_APPROVAL and state not in RESUMABLE:
                raise LedgerError(f"no run can start in state {state}")
            _set_state(db, State.RUNNING, reason="DRY_REHEARSAL")

    def open_phase(self, phase: Phase) -> int:
        """Open the next attempt of ``phase``; a crash phase also takes its crash attempt."""
        with self._tx() as db:
            if State(_meta(db)["state"]) is not State.RUNNING:
                raise LedgerError("a phase opens only inside an approved run")
            if db.execute("SELECT count(*) FROM phases WHERE closed_at IS NULL").fetchone()[0]:
                raise LedgerError("another phase is still open")
            if phase in CRASH_ATTEMPTS:
                number, label = CRASH_ATTEMPTS[phase]
                db.execute(
                    "INSERT INTO crash_attempts (attempt, label, opened_at) VALUES (?, ?, ?)",
                    (number, label, now()),
                )
            attempt = (
                1
                + db.execute(
                    "SELECT COALESCE(MAX(attempt), 0) FROM phases WHERE phase = ?", (phase.value,)
                ).fetchone()[0]
            )
            db.execute(
                "INSERT INTO phases (phase, attempt, opened_at) VALUES (?, ?, ?)",
                (phase.value, attempt, now()),
            )
            return int(attempt)

    def close_phase(self, phase: Phase, attempt: int, result: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE phases SET closed_at = ?, result = ?"
                " WHERE phase = ? AND attempt = ? AND closed_at IS NULL",
                (now(), result, phase.value, attempt),
            )

    def record_crash_verdict(
        self, crash_attempt: int, verdict: CrashVerdict, reasons: Sequence[str]
    ) -> None:
        """Record one attempt's verdict. A second miss exhausts the crash sub-budget."""
        with self._tx() as db:
            db.execute(
                "UPDATE crash_attempts SET verdict = ?, detail = ? WHERE attempt = ?",
                (verdict.value, json.dumps(list(reasons)), crash_attempt),
            )
            exhausted = (
                verdict is CrashVerdict.BOUNDARY_NOT_EXERCISED
                and crash_attempt == CRASH_ATTEMPT_CAP
            )
            if exhausted and State(_meta(db)["state"]) is State.RUNNING:
                db.execute(
                    "INSERT INTO refusals (at, pid, endpoint_id, phase, reason)"
                    " VALUES (?, 0, ?, NULL, ?)",
                    (now(), TOKEN, Refusal.CRASH_SUB_BUDGET_EXHAUSTED.value),
                )
                _set_state(db, State.BUDGET_EXHAUSTED, reason="CRASH_SUB_BUDGET_EXHAUSTED")

    def finish(self, target: State, *, reason: str) -> State:
        """End the run in ``target`` unless the ledger already ended it (budget exhaustion), and
        close any phase left open so no process can send under it afterwards."""
        with self._tx() as db:
            current = State(_meta(db)["state"])
            db.execute(
                "UPDATE phases SET closed_at = ?, result = 'INTERRUPTED' WHERE closed_at IS NULL",
                (now(),),
            )
            if current is not State.RUNNING:
                return current
            _set_state(db, target, reason=reason)
            return target

    def interrupt_if_abandoned(self) -> bool:
        """A run whose harness process died is stopped (the caller holds the campaign lease)."""
        if self.campaign().state is not State.RUNNING:
            return False
        self.finish(State.STOPPED_INTERRUPTED, reason="PREVIOUS_RUN_ABANDONED")
        return True

    def record_event(self, kind: str, detail: Mapping[str, object]) -> None:
        with self._tx() as db:
            _event(db, kind, detail)

    # ------------------------------------------------------------------ reads

    def campaign(self) -> Campaign:
        with closing(_connect(self.path)) as db:
            meta = _meta(db)
            return Campaign(
                campaign_id=meta["campaign_id"],
                mode=Mode(meta["mode"]),
                nonce=meta["nonce"],
                created_at=meta["created_at"],
                state=State(meta["state"]),
                outcome=meta["outcome"],
            )

    def counts(self) -> dict[str, int]:
        with closing(_connect(self.path)) as db:
            return {
                endpoint: int(
                    db.execute(
                        "SELECT count(*) FROM requests WHERE endpoint_id = ?", (endpoint,)
                    ).fetchone()[0]
                )
                for endpoint in CAPS
            }

    def rows(self, table: str) -> list[dict[str, Any]]:
        if table not in _TABLES:
            raise ValueError(table)
        with closing(_connect(self.path)) as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]

    def requests_for(self, phase: Phase, attempt: int) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows("requests")
            if row["phase"] == phase.value and row["phase_attempt"] == attempt
        ]

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.rows("events")
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return [row for row in rows if kind is None or row["kind"] == kind]

    def last_event(self, kind: str) -> dict[str, Any] | None:
        found = self.events(kind)
        return found[-1] if found else None

    def phase_passed(self, phase: Phase) -> bool:
        return any(
            row["phase"] == phase.value and row["result"] == "PASS" for row in self.rows("phases")
        )
