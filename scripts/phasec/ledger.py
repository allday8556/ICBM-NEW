"""The durable Phase C campaign ledger (Issue #110 C0; review `5313663701` B2 and B4).

One SQLite file, ``<campaign-root>/campaign.sqlite3``. It lives only in the campaign root: never
in, and never reaching, the live ICBM data root (the harness never opens that database). It follows
the accepted M3 campaign-ledger pattern:

* **Durable.** Every write is one ``BEGIN IMMEDIATE`` transaction committed with
  ``synchronous=FULL``, so a committed event survives a crash and two writers never interleave.
* **Single writer.** A whole harness command holds an exclusive OS lock on
  ``<campaign-root>/campaign.lock`` (``msvcrt.locking`` / ``fcntl.flock``); a second command on the
  same campaign fails ``CAMPAIGN_IN_USE`` before it reads or writes anything else.
* **Crash recovery.** A read opens the file normally with ``query_only`` on, so SQLite itself
  rolls back a hot journal a crashed write left behind; a read-only open could not, and would
  leave the campaign unreadable.
* **Append-only.** Triggers refuse every UPDATE and DELETE. Events are hash-chained as well.
* **Tamper and tail-loss evident.** Every read verifies that the stored schema is exactly the one
  this module creates (a dropped trigger is detected), that the event and reservation sequences
  run 1..n up to SQLite's own high-water mark (a removed tail is detected), the hash chain, and
  that every index table agrees with the events it indexes.
* **One identity per correlation.** ``correlations`` holds every correlation an event opens
  (creation, a stage grant, an intent) under a PRIMARY KEY, so a correlation is never reused;
  ``outcomes`` admits at most one outcome per intent, and only for an intent.
* **Typed stage grants.** A grant is the exact bytes of a typed JSON object
  (``scripts/phasec/grants.py``), stored as read and named by the SHA-256 of those bytes. Its
  authorization is an opaque GitHub anchor (``issuecomment-<id>`` / ``pullrequestreview-<id>``),
  used once; it is never compared by size. The ``grants`` table admits the stages strictly in
  order C0 → C4, once each.
* **Frozen ceilings that are enforced.** The per-stage ceilings are written once, at creation. A
  reservation is admitted only in the current stage and only while its class stays under its
  ceiling; a trigger refuses the rest, and every refusal is itself recorded. A reservation stays
  spent unless its command is proven not to have changed anything (``NOT_APPLIED``), which
  releases it for a safe retry.
* **Intent before effect, reconciled after a crash.** A command first commits an ``INTENDED``
  event with its reservations, then the harness reserves it in the data root, acts, and commits
  the outcome under the same correlation. An intent with no outcome is reconciled against owner
  truth: proven applied, proven not applied, or ambiguous — which puts the campaign on ``HOLD``,
  where it accepts no further intent.

The ledger holds no URL, cookie or secret: identifiers, digests, states, counts and times.
"""

import hashlib
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.phasec.ceilings import CEILINGS, STAGES

LEDGER = "campaign.sqlite3"
LOCK = "campaign.lock"
SCHEMA = "icbm-adaptive-phase-c-campaign/v3"
GENESIS = "0" * 64
CAMPAIGN_ID = re.compile(r"^phase-c-[a-z0-9][a-z0-9-]{2,40}$")
# An architect authorization, named as its GitHub anchor. It is an opaque identity: equal or not,
# never older or newer.
AUTHORIZATION = re.compile(r"^(issuecomment|pullrequestreview)-[1-9][0-9]{0,19}$")
CREATED = "CAMPAIGN_CREATED"
AUTHORIZED = "STAGE_AUTHORIZED"
INTENDED = "INTENDED"
HOLD = "CAMPAIGN_HOLD"
NOT_APPLIED = "NOT_APPLIED"
ACTION_REFUSED = "ACTION_REFUSED"
OPENING = (CREATED, AUTHORIZED, INTENDED)

_TABLES = (
    "campaign",
    "ceilings",
    "events",
    "correlations",
    "outcomes",
    "holds",
    "grants",
    "reservations",
    "refusals",
)
_SCHEMA = """
CREATE TABLE campaign (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    code_sha TEXT NOT NULL CHECK (length(code_sha) = 40),
    ceilings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE ceilings (
    stage TEXT NOT NULL,
    class TEXT NOT NULL,
    ceiling INTEGER NOT NULL CHECK (ceiling >= 0),
    PRIMARY KEY (stage, class)
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    correlation_id TEXT NOT NULL CHECK (length(trim(correlation_id)) > 0),
    payload_json TEXT NOT NULL,
    prev TEXT NOT NULL CHECK (length(prev) = 64),
    hash TEXT NOT NULL UNIQUE CHECK (length(hash) = 64)
);
CREATE TABLE correlations (
    correlation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('CAMPAIGN_CREATED', 'STAGE_AUTHORIZED', 'INTENDED')),
    event_seq INTEGER NOT NULL UNIQUE REFERENCES events (seq)
);
CREATE TABLE outcomes (
    correlation_id TEXT PRIMARY KEY REFERENCES correlations (correlation_id),
    applied INTEGER NOT NULL CHECK (applied IN (0, 1)),
    event_seq INTEGER NOT NULL UNIQUE REFERENCES events (seq)
);
CREATE TABLE holds (
    correlation_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL UNIQUE REFERENCES events (seq)
);
CREATE TABLE grants (
    stage_index INTEGER PRIMARY KEY,
    stage TEXT NOT NULL UNIQUE CHECK (stage = 'C' || stage_index),
    authorization TEXT NOT NULL UNIQUE,
    grant_digest TEXT NOT NULL CHECK (length(grant_digest) = 64),
    grant_text TEXT NOT NULL,
    event_seq INTEGER NOT NULL UNIQUE REFERENCES events (seq)
);
CREATE TABLE reservations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    stage TEXT NOT NULL,
    class TEXT NOT NULL,
    subject_digest TEXT NOT NULL CHECK (length(subject_digest) = 64),
    correlation_id TEXT NOT NULL REFERENCES correlations (correlation_id)
);
CREATE TABLE refusals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    stage TEXT,
    class TEXT NOT NULL,
    reason TEXT NOT NULL,
    correlation_id TEXT NOT NULL
);
CREATE VIEW current_stage AS SELECT stage FROM grants ORDER BY stage_index DESC LIMIT 1;
CREATE VIEW live_reservations AS
    SELECT r.* FROM reservations r
    WHERE NOT EXISTS (
        SELECT 1 FROM outcomes o WHERE o.correlation_id = r.correlation_id AND o.applied = 0
    );
CREATE TRIGGER campaign_once BEFORE INSERT ON campaign BEGIN
    SELECT RAISE(ABORT, 'CAMPAIGN_EXISTS') WHERE (SELECT COUNT(*) FROM events) > 0;
END;
CREATE TRIGGER ceilings_at_creation BEFORE INSERT ON ceilings BEGIN
    SELECT RAISE(ABORT, 'CEILINGS_FROZEN') WHERE (SELECT COUNT(*) FROM events) > 0;
END;
CREATE TRIGGER intents_one_at_a_time BEFORE INSERT ON correlations WHEN NEW.kind = 'INTENDED'
BEGIN
    SELECT RAISE(ABORT, 'CAMPAIGN_HOLD') WHERE EXISTS (SELECT 1 FROM holds);
    SELECT RAISE(ABORT, 'UNFINISHED_ACTION')
        WHERE EXISTS (
            SELECT 1 FROM correlations c
            WHERE c.kind = 'INTENDED'
                AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.correlation_id = c.correlation_id)
        );
END;
CREATE TRIGGER outcomes_answer_an_intent BEFORE INSERT ON outcomes BEGIN
    SELECT RAISE(ABORT, 'NOT_AN_INTENT')
        WHERE NOT EXISTS (
            SELECT 1 FROM correlations WHERE correlation_id = NEW.correlation_id
                AND kind = 'INTENDED'
        );
    SELECT RAISE(ABORT, 'HELD')
        WHERE EXISTS (SELECT 1 FROM holds WHERE correlation_id = NEW.correlation_id);
END;
CREATE TRIGGER holds_of_an_unanswered_command BEFORE INSERT ON holds BEGIN
    SELECT RAISE(ABORT, 'ANSWERED')
        WHERE EXISTS (SELECT 1 FROM outcomes WHERE correlation_id = NEW.correlation_id);
END;
CREATE TRIGGER grants_in_order BEFORE INSERT ON grants BEGIN
    SELECT RAISE(ABORT, 'STAGE_OUT_OF_ORDER')
        WHERE NEW.stage_index IS NOT (SELECT COALESCE(MAX(stage_index), -1) + 1 FROM grants);
END;
CREATE TRIGGER reservations_guard BEFORE INSERT ON reservations BEGIN
    SELECT RAISE(ABORT, 'STAGE_NOT_CURRENT')
        WHERE NEW.stage IS NOT (SELECT stage FROM current_stage);
    SELECT RAISE(ABORT, 'CLASS_NOT_BUDGETED')
        WHERE NOT EXISTS (SELECT 1 FROM ceilings WHERE stage = NEW.stage AND class = NEW.class);
    SELECT RAISE(ABORT, 'CEILING')
        WHERE (
            SELECT COUNT(*) FROM live_reservations
            WHERE stage = NEW.stage AND class = NEW.class
        ) >= (SELECT ceiling FROM ceilings WHERE stage = NEW.stage AND class = NEW.class);
END;
""" + "".join(
    f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;\n"
    for table in _TABLES
)


class LedgerRefused(RuntimeError):
    """The campaign ledger is absent, tampered with, or refuses this operation."""

    def __init__(self, message: str, code: str = "PHASE_C_LEDGER_REFUSED") -> None:
        super().__init__(message)
        self.code = code


class CampaignInUse(LedgerRefused):
    """Another harness command holds this campaign's writer lock."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def bytes_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _event_hash(event: Mapping[str, Any]) -> str:
    return sha256({key: value for key, value in event.items() if key != "hash"})


def now() -> str:
    return datetime.now(UTC).isoformat()


def _ceilings() -> dict[str, dict[str, int]]:
    return {stage: dict(values) for stage, values in CEILINGS.items()}


def parse_grant(raw: bytes) -> Any:
    """The JSON a grant's exact bytes hold; the same bytes are digested and recorded."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LedgerRefused(
            "a grant is one UTF-8 JSON object", code="PHASE_C_GRANT_REFUSED"
        ) from None


@dataclass(frozen=True)
class Grant:
    stage: str
    authorization: str
    digest: str
    grant: Mapping[str, Any]

    @property
    def scope(self) -> Mapping[str, Any]:
        scope: Mapping[str, Any] = self.grant["scope"]
        return scope


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    code_sha: str
    ceilings: Mapping[str, Mapping[str, int]]
    grants: Mapping[str, Grant]
    events: tuple[Mapping[str, Any], ...]

    @property
    def current_stage(self) -> str:
        return max(self.grants, key=STAGES.index)

    @property
    def authorized(self) -> dict[str, str]:
        return {stage: grant.authorization for stage, grant in self.grants.items()}

    def outcomes(self, kind: str) -> list[Mapping[str, Any]]:
        """The payloads of the recorded outcomes of one kind, in ledger order."""
        return [event["payload"] for event in self.events if event["kind"] == kind]

    def intent(self, correlation_id: str) -> Mapping[str, Any] | None:
        for event in self.events:
            if event["kind"] == INTENDED and event["correlation_id"] == correlation_id:
                payload: Mapping[str, Any] = event["payload"]
                return payload
        return None

    def intents(self) -> set[str]:
        return {e["correlation_id"] for e in self.events if e["kind"] == INTENDED}

    def held(self) -> list[Mapping[str, Any]]:
        """The commands this campaign holds as ambiguous (``HOLD``), with why."""
        return [
            {"correlation_id": e["correlation_id"], **e["payload"]}
            for e in self.events
            if e["kind"] == HOLD
        ]

    def unfinished(self) -> list[str]:
        """The correlations whose intent has neither an outcome nor a hold."""
        closed = {e["correlation_id"] for e in self.events if e["kind"] not in OPENING}
        return [
            e["correlation_id"]
            for e in self.events
            if e["kind"] == INTENDED and e["correlation_id"] not in closed
        ]


# ---------------------------------------------------------------- the OS writer lock


class _Lock:
    """An exclusive, non-blocking OS lock on an open handle, held for one harness command."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise CampaignInUse(
                "another harness command holds this campaign", code="CAMPAIGN_IN_USE"
            ) from None
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def _schema_of(db: sqlite3.Connection) -> set[tuple[str, str, str]]:
    rows = db.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {(str(t), str(n), " ".join(str(s).split())) for t, n, s in rows}


def _statements(script: str) -> list[str]:
    """The schema's statements, each complete (a trigger body holds its own semicolons)."""
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    return [s for s in statements if s]


def _expected_schema() -> set[tuple[str, str, str]]:
    with closing(sqlite3.connect(":memory:")) as db:
        for statement in _statements(_SCHEMA):
            db.execute(statement)
        return _schema_of(db)


def _reason(error: sqlite3.Error) -> str:
    text = str(error)
    if "correlations.correlation_id" in text:
        return "PHASE_C_CORRELATION_REUSED"
    if "grants.authorization" in text:
        return "PHASE_C_AUTHORIZATION_REUSED"
    return text.split(":")[-1].strip() or "REFUSED"


class CampaignLedger:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / LEDGER
        self.lock_path = root / LOCK
        self._held = False

    # -------------------------------------------------------------- plumbing

    @contextmanager
    def writer(self) -> Iterator[None]:
        """Hold this campaign's exclusive writer lock for one whole command."""
        if not self.root.is_dir():
            raise LedgerRefused("no campaign in this campaign root")
        lock = _Lock(self.lock_path)
        lock.acquire()
        self._held = True
        try:
            yield
        finally:
            self._held = False
            lock.release()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """A connection that writes nothing (``query_only``) yet can still roll back the hot journal
        of a crashed write; a file SQLite cannot read is refused, never a crash."""
        if not self.path.is_file():
            raise LedgerRefused("no campaign ledger in this campaign root")
        try:
            with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
                db.execute("PRAGMA query_only=ON")
                yield db
        except sqlite3.DatabaseError:
            raise LedgerRefused("the campaign ledger cannot be read as its own schema") from None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if not self._held:
            raise LedgerRefused("a campaign write needs the campaign's writer lock")
        try:
            with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
        except sqlite3.IntegrityError as refused:
            reason = _reason(refused)
            raise LedgerRefused(
                f"the campaign ledger refused this: {reason}", code=reason
            ) from None
        except sqlite3.DatabaseError:
            raise LedgerRefused("the campaign ledger cannot be written as its own schema") from None

    # -------------------------------------------------------------- verified reads

    @staticmethod
    def _events(db: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT seq, at, kind, actor, correlation_id, payload_json, prev, hash "
            "FROM events ORDER BY seq"
        ).fetchall()
        events = []
        previous = GENESIS
        for index, (seq, at, kind, actor, correlation, payload, prev, digest) in enumerate(
            rows, start=1
        ):
            event = {
                "seq": seq,
                "at": at,
                "kind": kind,
                "actor": actor,
                "correlation_id": correlation,
                "payload": json.loads(payload),
                "prev": prev,
                "hash": digest,
            }
            if seq != index or prev != previous or digest != _event_hash(event):
                raise LedgerRefused(f"campaign event {index} breaks the hash chain")
            events.append(event)
            previous = digest
        return events

    @staticmethod
    def _high_water(db: sqlite3.Connection, table: str) -> int:
        row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
        return 0 if row is None else int(row[0])

    @staticmethod
    def _indexes_agree(db: sqlite3.Connection, events: list[dict[str, Any]]) -> None:
        """Every index table holds exactly what the events it indexes say."""
        opened = {
            (e["correlation_id"], e["kind"], e["seq"]) for e in events if e["kind"] in OPENING
        }
        if set(db.execute("SELECT correlation_id, kind, event_seq FROM correlations")) != opened:
            raise LedgerRefused("the campaign's correlations do not match its events")
        answered = {
            (e["correlation_id"], 0 if e["kind"] in (NOT_APPLIED, ACTION_REFUSED) else 1, e["seq"])
            for e in events
            if e["kind"] not in (*OPENING, HOLD)
        }
        if set(db.execute("SELECT correlation_id, applied, event_seq FROM outcomes")) != answered:
            raise LedgerRefused("the campaign's outcomes do not match its events")
        held = {(e["correlation_id"], e["seq"]) for e in events if e["kind"] == HOLD}
        if set(db.execute("SELECT correlation_id, event_seq FROM holds")) != held:
            raise LedgerRefused("the campaign's holds do not match its events")

    def _verify(self, db: sqlite3.Connection) -> Campaign:
        if _schema_of(db) != _expected_schema():
            raise LedgerRefused("the campaign ledger's schema is not the one this harness writes")
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise LedgerRefused("the campaign ledger file is damaged")
        events = self._events(db)
        if len(events) != self._high_water(db, "events"):
            raise LedgerRefused("campaign events are missing from the end of the ledger")
        reservations = [r for (r,) in db.execute("SELECT seq FROM reservations ORDER BY seq")]
        if reservations != list(range(1, self._high_water(db, "reservations") + 1)):
            raise LedgerRefused("campaign reservations are missing from the ledger")
        if not events or events[0]["kind"] != CREATED:
            raise LedgerRefused("the campaign ledger does not start with CAMPAIGN_CREATED")
        self._indexes_agree(db, events)
        created = events[0]["payload"]
        row = db.execute(
            "SELECT schema, campaign_id, code_sha, ceilings_json FROM campaign"
        ).fetchone()
        stored = {
            (stage, name): value
            for stage, name, value in db.execute("SELECT stage, class, ceiling FROM ceilings")
        }
        frozen = {
            (stage, name): value
            for stage, values in _ceilings().items()
            for name, value in values.items()
        }
        if (
            row is None
            or row != (SCHEMA, created["campaign_id"], created["code_sha"], canonical(_ceilings()))
            or created.get("schema") != SCHEMA
            or created.get("ceilings") != _ceilings()
            or stored != frozen
        ):
            raise LedgerRefused("the campaign was created with other ceilings than this harness")
        by_seq = {event["seq"]: event for event in events}
        grants: dict[str, Grant] = {}
        for stage, authorization, digest, text, seq in db.execute(
            "SELECT stage, authorization, grant_digest, grant_text, event_seq "
            "FROM grants ORDER BY stage_index"
        ):
            grant = json.loads(text)
            event = by_seq.get(seq)
            if (
                bytes_digest(text.encode("utf-8")) != digest
                or event is None
                or event["kind"] != AUTHORIZED
                or event["payload"] != {"stage": stage, "grant_digest": digest, "grant": grant}
                or grant.get("authorization") != authorization
                or grant.get("stage") != stage
            ):
                raise LedgerRefused(f"the {stage} grant does not match its ledger event")
            grants[stage] = Grant(stage, authorization, digest, grant)
        authorizations = sum(event["kind"] == AUTHORIZED for event in events)
        if "C0" not in grants or authorizations != len(grants):
            raise LedgerRefused("the campaign's grants do not match its events")
        return Campaign(
            campaign_id=created["campaign_id"],
            code_sha=created["code_sha"],
            ceilings=created["ceilings"],
            grants=grants,
            events=tuple(events),
        )

    def campaign(self) -> Campaign:
        with self._read() as db:
            return self._verify(db)

    def events(self) -> tuple[Mapping[str, Any], ...]:
        return self.campaign().events

    def reservations(self, stage: str | None = None, *, live: bool = False) -> list[dict[str, Any]]:
        """The reservations, oldest first; ``live`` leaves out those NOT_APPLIED released."""
        table = "live_reservations" if live else "reservations"
        query = f"SELECT seq, stage, class, subject_digest, correlation_id FROM {table}"
        params: tuple[str, ...] = ()
        if stage is not None:
            query += " WHERE stage = ?"
            params = (stage,)
        with self._read() as db:
            self._verify(db)
            rows = db.execute(query + " ORDER BY seq", params).fetchall()
        return [
            {"seq": s, "stage": st, "class": c, "subject_digest": d, "correlation_id": k}
            for s, st, c, d, k in rows
        ]

    def counts(self, stage: str) -> dict[str, int]:
        """The live reservations of one stage per class: what its ceilings are measured against."""
        found: dict[str, int] = {}
        for reservation in self.reservations(stage, live=True):
            found[reservation["class"]] = found.get(reservation["class"], 0) + 1
        return {name: found.get(name, 0) for name in CEILINGS[stage]}

    def refusals(self) -> list[dict[str, Any]]:
        with self._read() as db:
            self._verify(db)
            rows = db.execute(
                "SELECT seq, stage, class, reason, correlation_id FROM refusals ORDER BY seq"
            ).fetchall()
        return [
            {"seq": s, "stage": st, "class": c, "reason": r, "correlation_id": k}
            for s, st, c, r, k in rows
        ]

    # -------------------------------------------------------------- writes

    @staticmethod
    def _append(
        db: sqlite3.Connection,
        kind: str,
        actor: str,
        correlation_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not actor.strip() or not correlation_id.strip():
            raise LedgerRefused("every campaign event names its actor and correlation")
        last = db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        event: dict[str, Any] = {
            "seq": 1 if last is None else int(last[0]) + 1,
            "at": now(),
            "kind": kind,
            "actor": actor,
            "correlation_id": correlation_id,
            "payload": dict(payload),
            "prev": GENESIS if last is None else str(last[1]),
        }
        event["hash"] = _event_hash(event)
        db.execute(
            "INSERT INTO events (seq, at, kind, actor, correlation_id, payload_json, prev, hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["seq"],
                event["at"],
                kind,
                actor,
                correlation_id,
                canonical(event["payload"]),
                event["prev"],
                event["hash"],
            ),
        )
        if kind in OPENING:
            db.execute(
                "INSERT INTO correlations VALUES (?, ?, ?)", (correlation_id, kind, event["seq"])
            )
        elif kind == HOLD:
            db.execute("INSERT INTO holds VALUES (?, ?)", (correlation_id, event["seq"]))
        else:
            applied = 0 if kind in (NOT_APPLIED, ACTION_REFUSED) else 1
            db.execute(
                "INSERT INTO outcomes VALUES (?, ?, ?)", (correlation_id, applied, event["seq"])
            )
        return event

    def _authorize(
        self, db: sqlite3.Connection, raw: bytes, actor: str, correlation_id: str
    ) -> dict[str, Any]:
        grant = parse_grant(raw)
        text = raw.decode("utf-8")
        stage = str(grant["stage"])
        digest = bytes_digest(raw)
        event = self._append(
            db,
            AUTHORIZED,
            actor,
            correlation_id,
            {"stage": stage, "grant_digest": digest, "grant": grant},
        )
        db.execute(
            "INSERT INTO grants VALUES (?, ?, ?, ?, ?, ?)",
            (STAGES.index(stage), stage, str(grant["authorization"]), digest, text, event["seq"]),
        )
        return event

    def create(self, *, campaign_id: str, code_sha: str, c0_grant: bytes, actor: str) -> None:
        if not CAMPAIGN_ID.fullmatch(campaign_id):
            raise LedgerRefused("a campaign id is phase-c-<lowercase words>")
        if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
            raise LedgerRefused("the campaign binds an exact 40-hex code SHA")
        if self.path.exists():
            raise LedgerRefused("this campaign root already holds a campaign")
        with self._transaction() as db:
            for statement in _statements(_SCHEMA):
                db.execute(statement)
            db.execute(
                "INSERT INTO campaign VALUES (1, ?, ?, ?, ?, ?)",
                (SCHEMA, campaign_id, code_sha, canonical(_ceilings()), now()),
            )
            for stage, values in _ceilings().items():
                for name, value in values.items():
                    db.execute("INSERT INTO ceilings VALUES (?, ?, ?)", (stage, name, value))
            self._append(
                db,
                CREATED,
                actor,
                f"{campaign_id}:created",
                {
                    "schema": SCHEMA,
                    "campaign_id": campaign_id,
                    "code_sha": code_sha,
                    "ceilings": _ceilings(),
                },
            )
            self._authorize(db, c0_grant, actor, f"{campaign_id}:stage:C0")

    def authorize(self, raw: bytes, *, actor: str, correlation_id: str) -> dict[str, Any]:
        """Record one stage grant, as the exact bytes ``grants.check_grant`` already admitted."""
        with self._transaction() as db:
            self._verify(db)
            return self._authorize(db, raw, actor, correlation_id)

    def intend(
        self,
        command: str,
        *,
        actor: str,
        correlation_id: str,
        payload: Mapping[str, Any],
        reservations: Sequence[tuple[str, str]] = (),
    ) -> dict[str, Any]:
        """Commit an action's intent and its reservations before any live-data effect.

        A reservation the ceilings refuse rolls the whole intent back; the refusal is then
        recorded on its own and nothing further happens."""
        refused: tuple[str, str, str] | None = None
        with self._transaction() as db:
            campaign = self._verify(db)
            stage = campaign.current_stage
            event = self._append(
                db, INTENDED, actor, correlation_id, {"command": command, "stage": stage, **payload}
            )
            db.execute("SAVEPOINT reservations")
            for name, subject in reservations:
                try:
                    db.execute(
                        "INSERT INTO reservations (at, stage, class, subject_digest, "
                        "correlation_id) VALUES (?, ?, ?, ?, ?)",
                        (now(), stage, name, subject, correlation_id),
                    )
                except sqlite3.IntegrityError as error:
                    refused = (stage, name, _reason(error))
                    break
            if refused is None:
                db.execute("RELEASE reservations")
                return event
            db.execute("ROLLBACK")  # the intent goes with its refused reservations
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO refusals (at, stage, class, reason, correlation_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (now(), *refused, correlation_id),
            )
        raise LedgerRefused(
            f"the campaign refused {refused[1]} in {refused[0]}: {refused[2]}", code=refused[2]
        )

    def record(
        self, kind: str, *, actor: str, correlation_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Commit the one outcome of an unfinished intent: its command's own kind when applied,
        ``NOT_APPLIED`` / ``ACTION_REFUSED`` when proven not to have changed anything."""
        if kind in (*OPENING, HOLD):
            raise LedgerRefused("an outcome is never an intent, a grant or a hold")
        with self._transaction() as db:
            campaign = self._verify(db)
            if correlation_id not in campaign.unfinished():
                raise LedgerRefused("an outcome answers one unfinished intent of this campaign")
            return self._append(db, kind, actor, correlation_id, payload)

    def hold(self, *, actor: str, correlation_id: str, reason: str) -> dict[str, Any]:
        """Put the campaign on HOLD for one command whose effect is ambiguous: no outcome is ever
        guessed for it, and the campaign accepts no further intent."""
        with self._transaction() as db:
            campaign = self._verify(db)
            if any(h["correlation_id"] == correlation_id for h in campaign.held()):
                return next(
                    dict(e)
                    for e in campaign.events
                    if e["kind"] == HOLD and e["correlation_id"] == correlation_id
                )
            return self._append(db, HOLD, actor, correlation_id, {"reason": reason})


def approval_phrase(campaign: Campaign, command: str, subject: str = "") -> str:
    """What the operator types, verbatim, before a command that can affect real evidence."""
    named = f"{command} {subject}" if subject else command
    return f"I APPROVE {named} FOR {campaign.campaign_id} AT {campaign.code_sha[:12]}"
