"""The durable ledger of ``m3-accept-01`` (Issue #52 rulings 5711123764 §4, §7 and 5711187191).

One SQLite file per campaign, outside the repository and outside every ordinary ICBM data
directory. It is the only thing that lets a campaign request leave the machine:

* **Pre-send.** Every external request is reserved — committed with ``synchronous=FULL`` — before a
  byte of it can be sent, and only while its own pass is running. A refused reservation sends
  nothing and is itself recorded.
* **Frozen ceilings.** The manifest is written once, at arming, together with one ceiling row per
  request class. Triggers refuse an update or a delete of either, and a reservation over a
  per-pass or campaign ceiling is refused by a trigger as well as by the code, so neither a code
  path nor a stray statement can widen the budget. Retries of a job reserve against the same
  ceilings; nothing resets them.
* **One state at a time.** ``INITIALIZED → ARMED → APPROVED → PASS_A_RUNNING →
  WAITING_FOR_PACING → PASS_B_RUNNING → CLOSEOUT_READY → COMPLETED``, with ``HOLD`` and
  ``STOPPED`` terminal from anywhere. Waiting for the same-product interval is a state the operator
  moves out of, not a loop anything spins in.
* **Append-only.** Every table refuses UPDATE and DELETE. A restart reads exactly what was
  committed, including every reservation already spent.

The ledger holds no URL, no cookie and no secret: request classes, digests of subjects, identities
of runs and revisions, times and codes.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from integrations.suppliers.transport.collection import CollectionBudgetRefused
from scripts.m3accept.manifest import CAMPAIGN_ID, Manifest, RequestClass

_TABLES = ("manifest", "ceilings", "events", "reservations", "refusals", "submissions", "results")


class State(StrEnum):
    INITIALIZED = "INITIALIZED"
    ARMED = "ARMED"
    APPROVED = "APPROVED"
    PASS_A_RUNNING = "PASS_A_RUNNING"
    WAITING_FOR_PACING = "WAITING_FOR_PACING"
    PASS_B_RUNNING = "PASS_B_RUNNING"
    CLOSEOUT_READY = "CLOSEOUT_READY"
    COMPLETED = "COMPLETED"
    HOLD = "HOLD"
    STOPPED = "STOPPED"


TERMINAL = frozenset({State.COMPLETED, State.HOLD, State.STOPPED})
RUNNING = {"A": State.PASS_A_RUNNING, "B": State.PASS_B_RUNNING}


class LedgerError(RuntimeError):
    """The ledger refused an operation that would break the campaign's rules."""


class CampaignBudgetRefused(CollectionBudgetRefused):
    """The campaign ledger refused a request before anything was sent."""


_SCHEMA = f"""
CREATE TABLE manifest (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    campaign_id TEXT NOT NULL CHECK (campaign_id = '{CAMPAIGN_ID}'),
    mode TEXT NOT NULL CHECK (mode IN ('REAL', 'DRY')),
    code_sha TEXT NOT NULL CHECK (length(code_sha) = 40),
    target_digest TEXT NOT NULL CHECK (length(target_digest) = 64),
    budget_digest TEXT NOT NULL CHECK (length(budget_digest) = 64),
    manifest_digest TEXT NOT NULL CHECK (length(manifest_digest) = 64),
    manifest_json TEXT NOT NULL,
    armed_at TEXT NOT NULL
);
CREATE TABLE ceilings (
    class TEXT PRIMARY KEY,
    per_pass INTEGER NOT NULL CHECK (per_pass >= 0),
    campaign INTEGER NOT NULL CHECK (campaign >= per_pass)
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE reservations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    pass TEXT NOT NULL CHECK (pass IN ('A', 'B')),
    class TEXT NOT NULL,
    subject_digest TEXT NOT NULL CHECK (length(subject_digest) = 64)
);
CREATE TABLE refusals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    pass TEXT,
    class TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE submissions (
    pass TEXT PRIMARY KEY CHECK (pass IN ('A', 'B')),
    at TEXT NOT NULL,
    session_nonce TEXT NOT NULL,
    collection_run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL
);
CREATE TABLE results (
    pass TEXT PRIMARY KEY CHECK (pass IN ('A', 'B')),
    at TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('ACCEPTED', 'HOLD', 'STOPPED')),
    detail TEXT NOT NULL
);
CREATE VIEW current_state AS SELECT state FROM events ORDER BY seq DESC LIMIT 1;

CREATE TRIGGER trg_manifest_once BEFORE INSERT ON manifest BEGIN
    SELECT RAISE(ABORT, 'ARMING_CLOSED')
        WHERE (SELECT state FROM current_state) IS NOT 'INITIALIZED';
END;
CREATE TRIGGER trg_ceilings_at_arming BEFORE INSERT ON ceilings BEGIN
    SELECT RAISE(ABORT, 'ARMING_CLOSED')
        WHERE (SELECT state FROM current_state) IS NOT 'INITIALIZED';
END;
CREATE TRIGGER trg_reservations_guard BEFORE INSERT ON reservations BEGIN
    SELECT RAISE(ABORT, 'PASS_NOT_RUNNING')
        WHERE (SELECT state FROM current_state) IS NOT ('PASS_' || NEW.pass || '_RUNNING');
    SELECT RAISE(ABORT, 'CLASS_NOT_BUDGETED')
        WHERE NOT EXISTS (SELECT 1 FROM ceilings WHERE class = NEW.class);
    SELECT RAISE(ABORT, 'PASS_CEILING')
        WHERE (SELECT COUNT(*) FROM reservations WHERE class = NEW.class AND pass = NEW.pass)
            >= (SELECT per_pass FROM ceilings WHERE class = NEW.class);
    SELECT RAISE(ABORT, 'CAMPAIGN_CEILING')
        WHERE (SELECT COUNT(*) FROM reservations WHERE class = NEW.class)
            >= (SELECT campaign FROM ceilings WHERE class = NEW.class);
END;
""" + "".join(
    f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    for table in _TABLES
)


def now() -> str:
    return datetime.now(UTC).isoformat()


def subject_digest(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Submission:
    pass_id: str
    session_nonce: str
    collection_run_id: str
    job_id: str
    correlation_id: str


class CampaignLedger:
    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise LedgerError("no campaign ledger at this path")
        self.path = path

    @classmethod
    def create(cls, path: Path) -> "CampaignLedger":
        if path.exists():
            raise LedgerError("a campaign ledger is never initialized twice")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT INTO events (at, state, detail) VALUES (?, ?, '{}')",
                (now(), State.INITIALIZED.value),
            )
            db.commit()
        return cls(path)

    # ---------------------------------------------------------------- plumbing

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    @staticmethod
    def _state(db: sqlite3.Connection) -> State:
        (state,) = db.execute("SELECT state FROM current_state").fetchone()
        return State(state)

    @staticmethod
    def _event(db: sqlite3.Connection, state: State, detail: Mapping[str, Any]) -> None:
        db.execute(
            "INSERT INTO events (at, state, detail) VALUES (?, ?, ?)",
            (now(), state.value, json.dumps(dict(detail), sort_keys=True)),
        )

    def _require(self, db: sqlite3.Connection, *allowed: State) -> State:
        state = self._state(db)
        if state not in allowed:
            raise LedgerError(f"the campaign is {state.value}, not {'/'.join(allowed)}")
        return state

    # ---------------------------------------------------------------- reading

    def state(self) -> State:
        with self._db() as db:
            return self._state(db)

    def manifest(self) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT manifest_json FROM manifest").fetchone()
        return None if row is None else dict(json.loads(row[0]))

    def events(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT seq, at, state, detail FROM events ORDER BY seq").fetchall()
        return [
            {"seq": seq, "at": at, "state": state, "detail": json.loads(detail)}
            for seq, at, state, detail in rows
        ]

    def counts(self, pass_id: str | None = None) -> dict[str, int]:
        query = "SELECT class, COUNT(*) FROM reservations"
        params: tuple[str, ...] = ()
        if pass_id is not None:
            query += " WHERE pass = ?"
            params = (pass_id,)
        with self._db() as db:
            rows = db.execute(query + " GROUP BY class", params).fetchall()
        found = {name: int(count) for name, count in rows}
        return {request.value: found.get(request.value, 0) for request in RequestClass}

    def refusals(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT seq, pass, class, reason FROM refusals ORDER BY seq"
            ).fetchall()
        return [
            {"seq": seq, "pass": pass_id, "class": name, "reason": reason}
            for seq, pass_id, name, reason in rows
        ]

    def ceiling(self, request: RequestClass) -> tuple[int, int]:
        with self._db() as db:
            row = db.execute(
                "SELECT per_pass, campaign FROM ceilings WHERE class = ?", (request.value,)
            ).fetchone()
        return (0, 0) if row is None else (int(row[0]), int(row[1]))

    def submission(self, pass_id: str) -> Submission | None:
        with self._db() as db:
            row = db.execute(
                "SELECT pass, session_nonce, collection_run_id, job_id, correlation_id "
                "FROM submissions WHERE pass = ?",
                (pass_id,),
            ).fetchone()
        return None if row is None else Submission(*row)

    def result(self, pass_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT verdict, detail FROM results WHERE pass = ?", (pass_id,)
            ).fetchone()
        return None if row is None else {"verdict": row[0], "detail": json.loads(row[1])}

    # ---------------------------------------------------------------- arming and approval

    def arm(self, manifest: Manifest) -> None:
        """Write the manifest and its ceilings once. After this nothing in either can change."""
        if manifest.campaign_id != CAMPAIGN_ID:
            raise LedgerError(f"this ledger belongs to {CAMPAIGN_ID}")
        body = manifest.as_json()
        with self._transaction() as db:
            self._require(db, State.INITIALIZED)
            db.execute(
                "INSERT INTO manifest VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.campaign_id,
                    manifest.mode,
                    manifest.code_sha,
                    manifest.target_digest,
                    manifest.budget.digest(),
                    manifest.digest(),
                    json.dumps(body, sort_keys=True),
                    now(),
                ),
            )
            for request in RequestClass:
                ceiling = manifest.budget.ceiling(request)
                db.execute(
                    "INSERT INTO ceilings VALUES (?, ?, ?)",
                    (request.value, ceiling.per_pass, ceiling.campaign),
                )
            self._event(db, State.ARMED, {"manifest_digest": manifest.digest()})

    def approve(self, code_sha: str) -> None:
        """Record that the operator typed the approval for exactly this SHA. REAL only."""
        with self._transaction() as db:
            self._require(db, State.ARMED)
            (mode, armed_sha) = db.execute("SELECT mode, code_sha FROM manifest").fetchone()
            if mode != "REAL":
                raise LedgerError("a DRY campaign needs no approval and accepts none")
            if code_sha != armed_sha:
                raise LedgerError("the approval names a different SHA than the one armed")
            self._event(db, State.APPROVED, {"code_sha": code_sha})

    # ---------------------------------------------------------------- passes

    def begin_pass(self, pass_id: str, *, session_nonce: str) -> None:
        """Enter a pass. PASS-B needs the pacing wait and a session PASS-A did not run in."""
        with self._transaction() as db:
            (mode,) = db.execute("SELECT mode FROM manifest").fetchone()
            if pass_id == "A":
                self._require(db, State.APPROVED if mode == "REAL" else State.ARMED)
            elif pass_id == "B":
                self._require(db, State.WAITING_FOR_PACING)
                row = db.execute(
                    "SELECT session_nonce FROM submissions WHERE pass = 'A'"
                ).fetchone()
                if row is not None and row[0] == session_nonce:
                    raise LedgerError("PASS-B runs in a fresh session, never the one PASS-A used")
            else:
                raise LedgerError("a pass is A or B")
            self._event(db, RUNNING[pass_id], {"session_nonce": session_nonce})

    def record_submission(self, submission: Submission) -> None:
        with self._transaction() as db:
            self._require(db, RUNNING[submission.pass_id])
            db.execute(
                "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    submission.pass_id,
                    now(),
                    submission.session_nonce,
                    submission.collection_run_id,
                    submission.job_id,
                    submission.correlation_id,
                ),
            )

    def reserve(self, pass_id: str, request: RequestClass, subject: str) -> None:
        """Reserve one external request before it is sent, or refuse it and send nothing."""
        with self._transaction() as db:
            try:
                db.execute(
                    "INSERT INTO reservations (at, pass, class, subject_digest) "
                    "VALUES (?, ?, ?, ?)",
                    (now(), pass_id, request.value, subject_digest(subject)),
                )
            except sqlite3.IntegrityError as refused:
                reason = str(refused).split(":")[-1].strip() or "REFUSED"
                refusal = (now(), pass_id, request.value, reason)
            else:
                return
        # The refusal is recorded in its own transaction: the reservation above was rolled back.
        with self._transaction() as db:
            db.execute(
                "INSERT INTO refusals (at, pass, class, reason) VALUES (?, ?, ?, ?)", refusal
            )
        raise CampaignBudgetRefused(
            f"M3_ACCEPT_{refusal[3]}", f"the campaign refused {request.value} before send"
        )

    def refuse(self, pass_id: str | None, request: RequestClass, reason: str) -> None:
        """Record a request that was never going to be allowed, without reserving anything."""
        with self._transaction() as db:
            db.execute(
                "INSERT INTO refusals (at, pass, class, reason) VALUES (?, ?, ?, ?)",
                (now(), pass_id, request.value, reason),
            )

    def finish_pass(self, pass_id: str, *, verdict: str, detail: Mapping[str, Any]) -> State:
        """Record a pass's verdict and move to what follows it."""
        with self._transaction() as db:
            self._require(db, RUNNING[pass_id])
            db.execute(
                "INSERT INTO results VALUES (?, ?, ?, ?)",
                (pass_id, now(), verdict, json.dumps(dict(detail), sort_keys=True)),
            )
            if verdict == "HOLD":
                following = State.HOLD
            elif verdict == "STOPPED":
                following = State.STOPPED
            else:
                following = State.WAITING_FOR_PACING if pass_id == "A" else State.CLOSEOUT_READY
            self._event(db, following, {"pass": pass_id, "verdict": verdict})
            return following

    def complete(self, report_digest: str) -> None:
        with self._transaction() as db:
            self._require(db, State.CLOSEOUT_READY)
            self._event(db, State.COMPLETED, {"report_digest": report_digest})

    def stop(self, reason: str) -> None:
        with self._transaction() as db:
            if self._state(db) in TERMINAL:
                return
            self._event(db, State.STOPPED, {"reason": reason})
