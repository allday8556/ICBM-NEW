"""The durable M3 reconnaissance ledger (ADR-0010 §4, §5; PR #55 rulings on Q1 and Q2).

One SQLite file per reconnaissance campaign. As the collection gateway's ``RequestBudget`` it is
the only thing that lets a reconnaissance request reach the supplier:

* every request is reserved — committed with ``synchronous=FULL`` — before any byte is sent;
* the reconnaissance caps are hard (ruling on Q1): product-detail reads 4, image requests 30,
  public policy reads 3. They are reconnaissance caps, not the M3 acceptance caps. Of the policy
  reads, at most one is a discovered policy read (``discovered:<path>``);
* requests are accepted only in an approved phase. Phase A covers policy and product reads,
  after the user's approval. Phase B covers image requests only, on the image hosts approved
  after phase A;
* phase A's observation is bound here, not in a file (PR #60 review 5214845204 §1): the exact
  observed image hosts, their digest and the digest of the sanitized findings are recorded in
  one transaction with the transition to the image-host STOP. Observations cannot be added
  after phase A, and a host can be approved only if it was recorded as observed;
* the same product is read at most once per 60 s (ruling on Q2), keyed by its canonical URL;
* every table is append-only. Triggers refuse UPDATE and DELETE and enforce the caps, the phase,
  the observation, the approved hosts, the discovered read and the interval a second time. No
  code path and no stray SQL statement can widen them.

The ledger holds no secret and no signed URL: request kinds, the canonical product URL, policy
paths, image hosts, statuses, times and digests.
"""

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from integrations.suppliers.collection import (
    DISCOVERED_POLICY_PREFIX,
    IMAGE_ROBOTS_PREFIX,
    ReadKind,
)
from integrations.suppliers.transport.collection import CollectionBudgetRefused

# Ruling on Q1: reconnaissance only. Every other kind of request is capped at 0.
CAPS: Mapping[ReadKind, int] = {
    ReadKind.PRODUCT_READ: 4,
    ReadKind.IMAGE_REQUEST: 30,
    ReadKind.POLICY_READ: 3,
}
SAME_PRODUCT_INTERVAL_S = 60.0
# The reservation guard's own version, kept in ``PRAGMA user_version``. A ledger created before a
# rule existed carries the older number until it is upgraded, and an approval that depends on a
# rule refuses a ledger that does not have it yet (Issue #52 ruling 5699776908).
GUARD_VERSION = 2
CAMPAIGN_ID = re.compile(r"^m3-recon-[a-z0-9][a-z0-9-]{1,40}$")
_TABLES = (
    "campaign",
    "events",
    "observed_hosts",
    "approved_hosts",
    "reservations",
    "completions",
)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def hosts_digest(hosts: Iterable[str]) -> str:
    return hashlib.sha256(json.dumps(sorted(set(hosts))).encode("utf-8")).hexdigest()


class Mode(StrEnum):
    REAL = "REAL"
    DRY = "DRY"


class State(StrEnum):
    INITIALIZED = "INITIALIZED"
    # The durable STOP after the zero-provider preflight; only an approved run leaves it.
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING_A = "RUNNING_A"
    # The provider reads are done and their captures are durable; only local work is left
    # (Issue #52 comment 5689874555 §4). No reservation is possible here, so phase A's network
    # collection can never resume from this state.
    FINALIZING_A = "FINALIZING_A"
    # Phase A recorded its observation; images wait for their explicit approval.
    AWAITING_IMAGE_HOST_APPROVAL = "AWAITING_IMAGE_HOST_APPROVAL"
    RUNNING_B = "RUNNING_B"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


TERMINAL = frozenset({State.COMPLETED, State.STOPPED, State.BUDGET_EXHAUSTED})
_TRANSITIONS: Mapping[State, frozenset[State]] = {
    State.INITIALIZED: frozenset({State.AWAITING_APPROVAL, State.STOPPED}),
    State.AWAITING_APPROVAL: frozenset({State.AWAITING_APPROVAL, State.RUNNING_A, State.STOPPED}),
    State.RUNNING_A: frozenset(
        {
            State.FINALIZING_A,
            State.AWAITING_IMAGE_HOST_APPROVAL,
            State.COMPLETED,
            State.STOPPED,
            State.BUDGET_EXHAUSTED,
        }
    ),
    # Local finalization only: the findings are written and bound, or the campaign stops. There
    # is no way back to RUNNING_A, so a refused finalization can never re-send provider requests.
    State.FINALIZING_A: frozenset({State.AWAITING_IMAGE_HOST_APPROVAL, State.STOPPED}),
    State.AWAITING_IMAGE_HOST_APPROVAL: frozenset(
        {State.RUNNING_B, State.COMPLETED, State.STOPPED}
    ),
    State.RUNNING_B: frozenset({State.COMPLETED, State.STOPPED, State.BUDGET_EXHAUSTED}),
}


def _cap_case() -> str:
    whens = " ".join(f"WHEN '{kind.value}' THEN {cap}" for kind, cap in CAPS.items())
    return f"CASE NEW.kind {whens} ELSE 0 END"


def reservations_guard() -> str:
    """The one statement that decides whether a request may be reserved.

    A ledger created today and a ledger upgraded to this version run the same text: it is written
    once here and installed by both paths, so the two can never drift apart.

    Phase B may reserve an image request, and exactly one robots preflight per approved image
    host. A generic policy read and a product read stay phase A's alone: the subject prefix, not
    the kind, is what opens the phase-B door, so nothing else widens with it.
    """
    robots = f"NEW.kind = 'POLICY_READ' AND NEW.subject LIKE '{IMAGE_ROBOTS_PREFIX}%'"
    robots_host = f"substr(NEW.subject, {len(IMAGE_ROBOTS_PREFIX) + 1})"
    return f"""CREATE TRIGGER trg_reservations_guard BEFORE INSERT ON reservations BEGIN
    SELECT RAISE(ABORT, 'CAP_REACHED')
        WHERE (SELECT COUNT(*) FROM reservations WHERE kind = NEW.kind) >= {_cap_case()};
    SELECT RAISE(ABORT, 'PHASE_CLOSED')
        WHERE (SELECT state FROM current_state) IS NOT (CASE
            WHEN NEW.kind = 'IMAGE_REQUEST' THEN 'RUNNING_B'
            WHEN {robots} THEN 'RUNNING_B'
            ELSE 'RUNNING_A' END);
    SELECT RAISE(ABORT, 'HOST_NOT_APPROVED')
        WHERE NEW.kind = 'IMAGE_REQUEST'
            AND NOT EXISTS (SELECT 1 FROM approved_hosts WHERE host = NEW.subject);
    SELECT RAISE(ABORT, 'HOST_NOT_APPROVED')
        WHERE {robots}
            AND NOT EXISTS (SELECT 1 FROM approved_hosts WHERE host = {robots_host});
    SELECT RAISE(ABORT, 'IMAGE_ROBOTS_ONCE')
        WHERE {robots}
            AND EXISTS (SELECT 1 FROM reservations WHERE subject = NEW.subject);
    SELECT RAISE(ABORT, 'DISCOVERED_POLICY_ONCE')
        WHERE NEW.kind = 'POLICY_READ' AND NEW.subject LIKE '{DISCOVERED_POLICY_PREFIX}%'
            AND EXISTS (
                SELECT 1 FROM reservations WHERE subject LIKE '{DISCOVERED_POLICY_PREFIX}%'
            );
    SELECT RAISE(ABORT, 'SAME_PRODUCT_INTERVAL')
        WHERE NEW.kind = 'PRODUCT_READ' AND EXISTS (
            SELECT 1 FROM reservations
            WHERE kind = 'PRODUCT_READ' AND subject = NEW.subject
                AND NEW.epoch - epoch < {SAME_PRODUCT_INTERVAL_S}
        );
END;"""


_SCHEMA = f"""
CREATE TABLE campaign (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    campaign_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('REAL', 'DRY')),
    product_url TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT,
    detail TEXT NOT NULL
);
CREATE TABLE observed_hosts (host TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE approved_hosts (host TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE reservations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    epoch REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ({", ".join(repr(k.value) for k in CAPS)})),
    subject TEXT NOT NULL CHECK (subject <> '')
);
CREATE TABLE completions (
    reservation INTEGER PRIMARY KEY REFERENCES reservations (seq),
    at TEXT NOT NULL,
    http_status INTEGER,
    outcome TEXT NOT NULL
);
CREATE VIEW current_state AS
    SELECT state FROM events WHERE state IS NOT NULL ORDER BY seq DESC LIMIT 1;
CREATE TRIGGER trg_observed_hosts_phase BEFORE INSERT ON observed_hosts BEGIN
    SELECT RAISE(ABORT, 'OBSERVATION_CLOSED')
        WHERE (SELECT state FROM current_state) NOT IN ('RUNNING_A', 'FINALIZING_A');
END;
CREATE TRIGGER trg_approved_hosts_observed BEFORE INSERT ON approved_hosts BEGIN
    SELECT RAISE(ABORT, 'APPROVAL_CLOSED')
        WHERE (SELECT state FROM current_state) IS NOT 'AWAITING_IMAGE_HOST_APPROVAL';
    SELECT RAISE(ABORT, 'HOST_NOT_OBSERVED')
        WHERE NOT EXISTS (SELECT 1 FROM observed_hosts WHERE host = NEW.host);
END;
{reservations_guard()}
""" + "".join(
    f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    for table in _TABLES
)


class LedgerError(RuntimeError):
    """The ledger refused an operation that would break the campaign's rules."""


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    mode: Mode
    product_url: str
    state: State


class Ledger:
    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise LedgerError("no reconnaissance ledger at this path")
        self.path = path

    @classmethod
    def create(cls, path: Path, *, campaign_id: str, mode: Mode, product_url: str) -> "Ledger":
        if not CAMPAIGN_ID.fullmatch(campaign_id):
            raise LedgerError("a reconnaissance campaign ID looks like m3-recon-01")
        if path.exists():
            raise LedgerError("a campaign ledger is never initialized twice")
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(_SCHEMA)
            db.execute(f"PRAGMA user_version = {GUARD_VERSION}")
            db.execute(
                "INSERT INTO campaign VALUES (1, ?, ?, ?, ?)",
                (campaign_id, mode.value, product_url, now()),
            )
            db.execute(
                "INSERT INTO events (at, kind, state, detail) VALUES (?, 'INITIALIZED', ?, '{}')",
                (now(), State.INITIALIZED.value),
            )
            db.commit()
        return cls(path)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
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

    def guard_version(self) -> int:
        """Which reservation guard this ledger carries."""
        with self._db() as db:
            (version,) = db.execute("PRAGMA user_version").fetchone()
        return int(version)

    def upgrade_guard(self) -> bool:
        """Install the current reservation guard on an older ledger; True when it did.

        Idempotent and versioned: a ledger already at this version is left alone, so running it
        twice changes nothing. The drop, the new guard, the version and the event that records it
        are one transaction, so a ledger can never be left carrying a guard its version denies.
        It adds no table, alters no CHECK and touches no recorded row.
        """
        with self._transaction() as db:
            (version,) = db.execute("PRAGMA user_version").fetchone()
            if int(version) >= GUARD_VERSION:
                return False
            db.execute("DROP TRIGGER IF EXISTS trg_reservations_guard")
            db.execute(reservations_guard())
            db.execute(f"PRAGMA user_version = {GUARD_VERSION}")
            self._append(
                db,
                "LEDGER_GUARD_UPGRADED",
                None,  # the campaign's own state is not touched by an upgrade
                {"from": int(version), "to": GUARD_VERSION},
            )
        return True

    # ---------------------------------------------------------------- campaign and events

    def campaign(self) -> Campaign:
        with self._db() as db:
            campaign_id, mode, product_url = db.execute(
                "SELECT campaign_id, mode, product_url FROM campaign"
            ).fetchone()
            (state,) = db.execute("SELECT state FROM current_state").fetchone()
        return Campaign(campaign_id, Mode(mode), product_url, State(state))

    def state(self) -> State:
        return self.campaign().state

    @staticmethod
    def _append(
        db: sqlite3.Connection, kind: str, state: State | None, detail: Mapping[str, Any]
    ) -> int:
        if state is not None:
            (current,) = db.execute("SELECT state FROM current_state").fetchone()
            if state not in _TRANSITIONS.get(State(current), frozenset()):
                raise LedgerError(f"no transition {current} -> {state}")
        cursor = db.execute(
            "INSERT INTO events (at, kind, state, detail) VALUES (?, ?, ?, ?)",
            (now(), kind, None if state is None else state.value, json.dumps(detail)),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def record(self, kind: str, *, state: State | None = None, **detail: Any) -> int:
        """Append one event; with ``state`` it is a transition the state machine allows."""
        with self._transaction() as db:
            return self._append(db, kind, state, detail)

    def last_event(self, kind: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT seq, at, detail FROM events WHERE kind = ? ORDER BY seq DESC LIMIT 1",
                (kind,),
            ).fetchone()
        if row is None:
            return None
        return {"seq": row[0], "at": row[1], "detail": json.loads(row[2])}

    def latest_seq(self) -> int:
        with self._db() as db:
            (seq,) = db.execute("SELECT MAX(seq) FROM events").fetchone()
        return int(seq)

    # ---------------------------------------------------------------- phase A observation

    def finish_phase_a(self, observed_hosts: Iterable[str], *, findings_digest: str) -> None:
        """Bind phase A's observation, in one transaction with the image-host STOP: the exact
        observed image hosts, their digest and the digest of the sanitized findings."""
        hosts = sorted(set(observed_hosts))
        try:
            with self._transaction() as db:
                db.executemany(
                    "INSERT INTO observed_hosts VALUES (?, ?)", [(host, now()) for host in hosts]
                )
                self._append(
                    db,
                    "PHASE_A_DONE",
                    State.AWAITING_IMAGE_HOST_APPROVAL,
                    {
                        "observed_hosts": hosts,
                        "observed_hosts_digest": hosts_digest(hosts),
                        "findings_digest": findings_digest,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"the observation was refused ({exc})") from None

    def observed_hosts(self) -> frozenset[str]:
        with self._db() as db:
            return frozenset(row[0] for row in db.execute("SELECT host FROM observed_hosts"))

    def offline_finalization_problems(self) -> list[str]:
        """Why phase A cannot be finished locally; empty when it can (comment 5689874555 §5).

        A campaign qualifies while its reads are done and nothing is bound yet: the FINALIZING_A
        shape a current run reaches on its own, and the RUNNING_A shape left by a run that
        crashed after its reads.
        """
        problems = []
        state = self.state()
        if state not in (State.RUNNING_A, State.FINALIZING_A):
            problems.append(f"the campaign is {state}, not phase A awaiting local finalization")
        if self.last_event("PHASE_A_DONE") is not None:
            problems.append("phase A is already bound")
        counts = self.counts()
        if counts[ReadKind.IMAGE_REQUEST.value]:
            problems.append("image requests exist; this finishes phase A only")
        if not counts[ReadKind.PRODUCT_READ.value]:
            problems.append("no product read was reserved")
        if any(row["outcome"] is None for row in self.reservations()):
            problems.append("a reservation has no completion")
        return problems

    def observation_problems(self) -> list[str]:
        """Why the recorded observation cannot back an image-host approval; empty if it can."""
        event = self.last_event("PHASE_A_DONE")
        if event is None:
            return ["phase A recorded no observation"]
        if hosts_digest(self.observed_hosts()) != event["detail"].get("observed_hosts_digest"):
            return ["the observed hosts do not match the digest recorded at the end of phase A"]
        return []

    # ---------------------------------------------------------------- image hosts

    def approve_hosts(self, hosts: Iterable[str]) -> None:
        chosen = sorted(set(hosts))
        if not chosen:
            raise LedgerError("phase B approves at least one image host")
        if self.state() is not State.AWAITING_IMAGE_HOST_APPROVAL:
            raise LedgerError("image hosts are approved only after phase A")
        try:
            with self._transaction() as db:
                db.executemany(
                    "INSERT INTO approved_hosts VALUES (?, ?)", [(host, now()) for host in chosen]
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"the image-host approval was refused ({exc})") from None

    def approved_hosts(self) -> frozenset[str]:
        with self._db() as db:
            return frozenset(row[0] for row in db.execute("SELECT host FROM approved_hosts"))

    # ---------------------------------------------------------------- requests

    def reserve(self, kind: ReadKind, subject: str, *, epoch: float | None = None) -> int:
        """Durably reserve one request before it is sent, or refuse it with nothing sent."""
        try:
            with self._transaction() as db:
                cursor = db.execute(
                    "INSERT INTO reservations (at, epoch, kind, subject) VALUES (?, ?, ?, ?)",
                    (now(), time.time() if epoch is None else epoch, kind.value, subject),
                )
        except sqlite3.IntegrityError as exc:
            raise CollectionBudgetRefused(
                "COLLECT_BUDGET_REFUSED",
                f"the reconnaissance ledger refused the request before send ({exc})",
            ) from None
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def complete(self, reservation: int, *, http_status: int | None, outcome: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO completions VALUES (?, ?, ?, ?)",
                (reservation, now(), http_status, outcome),
            )

    def counts(self) -> dict[str, int]:
        with self._db() as db:
            found = dict(
                db.execute("SELECT kind, COUNT(*) FROM reservations GROUP BY kind").fetchall()
            )
        return {kind.value: int(found.get(kind.value, 0)) for kind in CAPS}

    def reservations(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT r.seq, r.kind, r.subject, c.http_status, c.outcome FROM reservations r "
                "LEFT JOIN completions c ON c.reservation = r.seq ORDER BY r.seq"
            ).fetchall()
        return [
            {"seq": s, "kind": k, "subject": subject, "http_status": h, "outcome": o}
            for s, k, subject, h, o in rows
        ]


class LedgerBudget:
    """The gateway's ``RequestBudget``, backed by the ledger; remembers the last reservation so
    the run can record its completion."""

    def __init__(self, ledger: Ledger, *, clock: Callable[[], float] = time.time) -> None:
        self.ledger = ledger
        self.clock = clock
        self.last: int | None = None

    def reserve(self, kind: ReadKind, subject: str) -> None:
        self.last = None
        self.last = self.ledger.reserve(kind, subject, epoch=self.clock())
