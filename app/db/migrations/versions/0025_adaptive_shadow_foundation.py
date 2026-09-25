"""Adaptive Collector P3: the shadow foundation.

Revision ID: 0025_adaptive_shadow_foundation
Revises: 0024_adaptive_profile_validation
Create Date: 2026-09-25

Issue #110, production slice P3 (authorization `5824551569`), under ADR-0017 §10 and §11 and the
Phase C carry-forward `5818794101`. It adds five tables, five nullable columns on
``collection_runs`` and triggers; it rewrites no row and backfills nothing.

**The canonical run record (additive, nullable, no backfill).** ``collection_runs`` gains the
shadow decision a run freezes at its first product-read reservation — ``shadow_decision``
(``ENABLED`` / ``DISABLED``), ``shadow_switch_entry_id``, ``shadow_bundle_key`` and
``first_product_read_at`` — and ``settled_by_recovery``, which says the recovery path settled a
run from a revision an earlier attempt had appended (§11.2). A run that predates this migration
keeps NULL and is never shadow-eligible. Once frozen, the four frozen columns never change.

**What it holds.**
- ``adaptive_shadow_switch_entries``: the append-only per-supplier shadow switch. An ``ENABLE``
  names the exact EPR, bundle and validation freshness it enabled; a ``DISABLE`` names none. No
  supplier has an entry, so every supplier is off.
- ``adaptive_shadow_records``: the raw, non-canonical shadow comparison, at most one per run.
  Nothing canonical references it; it is pruned by its hard retention bounds.
- ``adaptive_shadow_ledger_events``: the append-only evidence ledger, per eligible run.
- ``adaptive_evidence_windows`` and ``adaptive_evidence_window_events``: evidence windows and
  their append-only ``DECLARED`` / ``ENDED`` / ``CLOSED`` / ``SUPERSEDED`` history.

**What the database enforces.**
- Switch entries, ledger events, windows and window events are never updated or deleted; each
  sequence follows the one before it. A raw shadow record is never updated.
- An EPR enters ``SHADOW`` only while its supplier's latest switch entry enables exactly that EPR,
  and leaves ``SHADOW`` for ``DRAFT`` only when it no longer does (a trigger on the P2 lifecycle).
- A ledger event after the first follows only an ``UNRESOLVED_MISMATCH`` state, in the same
  window. At most one window per supplier is open.
- A raw record belongs to a run frozen ``ENABLED`` for its supplier and bundle, and names only
  that run's own revision; a ledger event names exactly the window the run's frozen decision and
  first reservation make it eligible for.
- ``settled_by_recovery`` is carried only by a ``RECORDED`` run, is set explicitly in the update
  that settles it, and never changes afterwards.

**Downgrade fails closed.** It refuses while any of the new tables holds a row, or any run froze an
``ENABLED`` decision: shadow evidence is never silently destroyed. A ``DISABLED`` decision, and the
recovery marker of a run that was never shadow-eligible, carry no shadow evidence; the step down
drops those columns and keeps every run row, as earlier fail-closed downgrades keep theirs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_adaptive_shadow_foundation"
down_revision: str | None = "0024_adaptive_profile_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "collection_runs"
TRANSITIONS = "adaptive_profile_transitions"
REVISIONS = "adaptive_profile_revisions"
SWITCH = "adaptive_shadow_switch_entries"
RECORDS = "adaptive_shadow_records"
WINDOWS = "adaptive_evidence_windows"
WINDOW_EVENTS = "adaptive_evidence_window_events"
LEDGER = "adaptive_shadow_ledger_events"
CREATED = (LEDGER, WINDOW_EVENTS, WINDOWS, RECORDS, SWITCH)
RUN_COLUMNS = (
    "shadow_decision",
    "shadow_switch_entry_id",
    "shadow_bundle_key",
    "first_product_read_at",
    "settled_by_recovery",
)

SWITCH_ACTIONS = ("ENABLE", "DISABLE")
RUN_VERDICTS = (
    "MATCH",
    "MISMATCH",
    "IDENTITY_MISMATCH",
    "TEMPLATE_UNMATCHED",
    "TEMPLATE_AMBIGUOUS",
    "IMAGE_UNMATCHABLE",
    "SHADOW_FAILED",
)
SEVERITIES = ("CONFIDENT_DISAGREEMENT", "ADAPTIVE_OVERCONFIDENT", "ADAPTIVE_CONSERVATIVE")
EVENT_KINDS = ("OUTCOME_RECORDED", "RESOLUTION_RECORDED", "RAW_PRUNED_UNRESOLVED")
COUNTS_AS = ("SUCCESS", "FAILURE", "INCOMPLETE")
CAUSES = (
    "UNRESOLVED_MISMATCH",
    "PRUNED_BEFORE_RESOLUTION",
    "IMAGE_UNMATCHABLE",
    "SHADOW_MISSING",
    "SHADOW_MISSING_AFTER_RECOVERY",
)
RESOLUTIONS = ("CURRENT_CORRECT", "ADAPTIVE_CORRECT", "BOTH_WRONG", "SOURCE_AMBIGUOUS")
WINDOW_KINDS = ("DECLARED", "ENDED", "CLOSED", "SUPERSEDED")
MIN_WINDOW_SIZE = 3


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _bundle_key(column: str) -> str:
    return (
        f"json_valid({column}) AND json_type({column}) = 'array'"
        f" AND json_array_length({column}) = 4 AND json_extract({column}, '$[0]') = 'ADAPTIVE'"
    )


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _immutable(table: str, *, deletable: bool = False) -> None:
    _trigger(table, "no_update", "UPDATE", _raise(f"a {table} row is never updated", "1"))
    if not deletable:
        _trigger(table, "no_delete", "DELETE", _raise(f"a {table} row is never deleted", "1"))


def upgrade() -> None:
    # ------------------------------------------------------------ the canonical run record
    op.add_column(RUNS, sa.Column("shadow_decision", sa.String(length=8), nullable=True))
    op.add_column(RUNS, sa.Column("shadow_switch_entry_id", sa.String(length=36), nullable=True))
    op.add_column(RUNS, sa.Column("shadow_bundle_key", sa.Text(), nullable=True))
    op.add_column(RUNS, sa.Column("first_product_read_at", sa.DateTime(), nullable=True))
    op.add_column(RUNS, sa.Column("settled_by_recovery", sa.Boolean(), nullable=True))
    shape = _raise(
        f"{RUNS}: a frozen shadow decision is ENABLED with its entry and bundle, or DISABLED",
        "NOT ("
        "(NEW.shadow_decision IS NULL AND NEW.shadow_switch_entry_id IS NULL"
        " AND NEW.shadow_bundle_key IS NULL AND NEW.first_product_read_at IS NULL)"
        " OR (NEW.shadow_decision = 'DISABLED' AND NEW.shadow_switch_entry_id IS NULL"
        " AND NEW.shadow_bundle_key IS NULL AND NEW.first_product_read_at IS NOT NULL)"
        " OR (NEW.shadow_decision = 'ENABLED' AND NEW.shadow_switch_entry_id IS NOT NULL"
        f" AND NEW.shadow_bundle_key IS NOT NULL AND ({_bundle_key('NEW.shadow_bundle_key')})"
        " AND NEW.first_product_read_at IS NOT NULL))",
    )
    _trigger(RUNS, "shadow_shape_insert", "INSERT", shape)
    frozen = _raise(
        f"{RUNS}: a frozen shadow decision is never changed",
        "OLD.shadow_decision IS NOT NULL AND ("
        "NEW.shadow_decision IS NOT OLD.shadow_decision"
        " OR NEW.shadow_switch_entry_id IS NOT OLD.shadow_switch_entry_id"
        " OR NEW.shadow_bundle_key IS NOT OLD.shadow_bundle_key"
        " OR NEW.first_product_read_at IS NOT OLD.first_product_read_at)",
    )
    op.execute(
        f"CREATE TRIGGER trg_{RUNS}_shadow_frozen BEFORE UPDATE ON {RUNS}"
        f" BEGIN {frozen} {shape} END"
    )
    # The recovery marker decides the one non-blocking missing-shadow cause (§11.2), so it is a
    # structural invariant (review 5312254605 B2): only a RECORDED run carries it; a run settles
    # RECORDED with an explicit true or false in that same update; and a settled run's marker
    # never changes, so no later edit can turn SHADOW_MISSING into the recovery exception.
    only_recorded = _raise(
        f"{RUNS}: only a RECORDED run carries a recovery marker",
        "NEW.settled_by_recovery IS NOT NULL AND NEW.outcome <> 'RECORDED'",
    )
    _trigger(RUNS, "recovery_insert", "INSERT", only_recorded)
    settles = _raise(
        f"{RUNS}: a run settles RECORDED with an explicit recovery marker",
        "OLD.outcome = 'PENDING' AND NEW.outcome = 'RECORDED' AND NEW.settled_by_recovery IS NULL",
    )
    fixed = _raise(
        f"{RUNS}: a settled run keeps its recovery marker",
        "OLD.outcome <> 'PENDING' AND NEW.settled_by_recovery IS NOT OLD.settled_by_recovery",
    )
    op.execute(
        f"CREATE TRIGGER trg_{RUNS}_recovery_update BEFORE UPDATE ON {RUNS}"
        f" BEGIN {only_recorded} {settles} {fixed} END"
    )

    # ------------------------------------------------------------ the shadow switch
    op.create_table(
        SWITCH,
        sa.Column("entry_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("epr_digest", sa.String(length=64), nullable=True),
        sa.Column("bundle_key", sa.Text(), nullable=True),
        sa.Column("freshness_json", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check(SWITCH, _in("action", SWITCH_ACTIONS), "action_valid"),
        _check(SWITCH, "seq >= 1", "seq_valid"),
        _check(
            SWITCH,
            "(action = 'ENABLE' AND epr_digest IS NOT NULL AND bundle_key IS NOT NULL"
            " AND freshness_json IS NOT NULL)"
            " OR (action = 'DISABLE' AND epr_digest IS NULL AND bundle_key IS NULL"
            " AND freshness_json IS NULL)",
            "action_shape",
        ),
        _check(SWITCH, f"bundle_key IS NULL OR ({_bundle_key('bundle_key')})", "bundle_key_shape"),
        _check(
            SWITCH,
            "freshness_json IS NULL OR (json_valid(freshness_json)"
            " AND json_type(freshness_json) = 'array' AND json_array_length(freshness_json) = 7)",
            "freshness_shape",
        ),
        _check(SWITCH, "actor <> ''", "actor_present"),
        _check(SWITCH, "reason <> ''", "reason_present"),
        _check(SWITCH, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["epr_digest"],
            [f"{REVISIONS}.digest"],
            name=op.f(f"fk_{SWITCH}_epr_digest_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("entry_id", name=op.f(f"pk_{SWITCH}")),
        sa.UniqueConstraint("supplier_key", "seq", name=op.f(f"uq_{SWITCH}_supplier_key_seq")),
    )

    # ------------------------------------------------------------ raw shadow records
    op.create_table(
        RECORDS,
        sa.Column("collection_run_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=True),
        sa.Column("bundle_key", sa.Text(), nullable=False),
        sa.Column("run_verdict", sa.String(length=24), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=True),
        sa.Column("comparison_json", sa.Text(), nullable=False),
        sa.Column("record_digest", sa.String(length=64), nullable=False),
        sa.Column("process_run_id", sa.String(length=36), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check(RECORDS, _in("run_verdict", RUN_VERDICTS), "verdict_valid"),
        _check(RECORDS, f"severity IS NULL OR {_in('severity', SEVERITIES)}", "severity_valid"),
        _check(RECORDS, _bundle_key("bundle_key"), "bundle_key_shape"),
        _check(
            RECORDS,
            "json_valid(comparison_json) AND json_type(comparison_json) = 'object'",
            "comparison_object",
        ),
        _check(RECORDS, _hex64("record_digest"), "digest_hex"),
        _check(RECORDS, "process_run_id <> ''", "process_present"),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            [f"{RUNS}.collection_run_id"],
            name=op.f(f"fk_{RECORDS}_collection_run_id_{RUNS}"),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["product_facts_revisions.revision_id"],
            name=op.f(f"fk_{RECORDS}_revision_id_product_facts_revisions"),
        ),
        sa.PrimaryKeyConstraint("collection_run_id", name=op.f(f"pk_{RECORDS}")),
    )
    op.create_index(
        "ix_adaptive_shadow_records_supplier_key_recorded_at",
        RECORDS,
        ["supplier_key", "recorded_at"],
    )

    # ------------------------------------------------------------ evidence windows
    op.create_table(
        WINDOWS,
        sa.Column("window_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("bundle_key", sa.Text(), nullable=False),
        sa.Column("min_size", sa.Integer(), nullable=False),
        sa.Column("declared_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("declared_at", sa.DateTime(), nullable=False),
        _check(WINDOWS, _bundle_key("bundle_key"), "bundle_key_shape"),
        _check(WINDOWS, f"min_size >= {MIN_WINDOW_SIZE}", "min_size_valid"),
        _check(WINDOWS, "declared_by <> ''", "author_present"),
        _check(WINDOWS, "correlation_id <> ''", "correlation_present"),
        sa.PrimaryKeyConstraint("window_id", name=op.f(f"pk_{WINDOWS}")),
    )
    op.create_index("ix_adaptive_evidence_windows_supplier_key", WINDOWS, ["supplier_key"])
    op.create_table(
        WINDOW_EVENTS,
        sa.Column("window_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=12), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        _check(WINDOW_EVENTS, _in("kind", WINDOW_KINDS), "kind_valid"),
        _check(WINDOW_EVENTS, "(seq = 1) = (kind = 'DECLARED')", "declared_first"),
        _check(
            WINDOW_EVENTS,
            "detail_json IS NULL OR (json_valid(detail_json)"
            " AND json_type(detail_json) = 'object')",
            "detail_object",
        ),
        _check(WINDOW_EVENTS, "actor <> ''", "actor_present"),
        _check(WINDOW_EVENTS, "reason <> ''", "reason_present"),
        sa.ForeignKeyConstraint(
            ["window_id"],
            [f"{WINDOWS}.window_id"],
            name=op.f(f"fk_{WINDOW_EVENTS}_window_id_{WINDOWS}"),
        ),
        sa.PrimaryKeyConstraint("window_id", "seq", name=op.f(f"pk_{WINDOW_EVENTS}")),
    )

    # ------------------------------------------------------------ the evidence ledger
    op.create_table(
        LEDGER,
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("collection_run_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("window_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("count_as", sa.String(length=12), nullable=False),
        sa.Column("cause", sa.String(length=40), nullable=True),
        sa.Column("run_verdict", sa.String(length=24), nullable=True),
        sa.Column("revision_id", sa.String(length=36), nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=True),
        sa.Column("evidence_ref", sa.Text(), nullable=True),
        sa.Column("adaptive_failed_closed", sa.Boolean(), nullable=True),
        sa.Column("process_run_id", sa.String(length=36), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        _check(LEDGER, _in("kind", EVENT_KINDS), "kind_valid"),
        _check(LEDGER, _in("count_as", COUNTS_AS), "count_as_valid"),
        _check(LEDGER, f"cause IS NULL OR {_in('cause', CAUSES)}", "cause_valid"),
        _check(LEDGER, "(count_as = 'INCOMPLETE') = (cause IS NOT NULL)", "cause_iff_incomplete"),
        _check(
            LEDGER, f"run_verdict IS NULL OR {_in('run_verdict', RUN_VERDICTS)}", "verdict_valid"
        ),
        _check(
            LEDGER, f"resolution IS NULL OR {_in('resolution', RESOLUTIONS)}", "resolution_valid"
        ),
        _check(
            LEDGER,
            "(kind = 'OUTCOME_RECORDED' AND seq = 1 AND resolution IS NULL)"
            " OR (kind = 'RESOLUTION_RECORDED' AND seq > 1 AND resolution IS NOT NULL"
            " AND evidence_ref IS NOT NULL AND evidence_ref <> ''"
            " AND adaptive_failed_closed IS NOT NULL)"
            " OR (kind = 'RAW_PRUNED_UNRESOLVED' AND seq > 1 AND resolution IS NULL"
            " AND count_as = 'INCOMPLETE' AND cause = 'PRUNED_BEFORE_RESOLUTION')",
            "kind_shape",
        ),
        _check(LEDGER, "process_run_id <> ''", "process_present"),
        _check(LEDGER, "actor <> ''", "actor_present"),
        _check(LEDGER, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            [f"{RUNS}.collection_run_id"],
            name=op.f(f"fk_{LEDGER}_collection_run_id_{RUNS}"),
        ),
        sa.ForeignKeyConstraint(
            ["window_id"], [f"{WINDOWS}.window_id"], name=op.f(f"fk_{LEDGER}_window_id_{WINDOWS}")
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f(f"pk_{LEDGER}")),
        sa.UniqueConstraint(
            "collection_run_id", "seq", name=op.f(f"uq_{LEDGER}_collection_run_id_seq")
        ),
    )
    op.create_index("ix_adaptive_shadow_ledger_events_window_id", LEDGER, ["window_id"])
    _install_triggers()


def _install_triggers() -> None:
    # The switch: append-only, in order, and an ENABLE names an EPR of its own supplier.
    _immutable(SWITCH)
    _trigger(
        SWITCH,
        "follows",
        "INSERT",
        _raise(
            f"{SWITCH}: an entry follows the one before it",
            f"NEW.seq <> 1 + (SELECT COALESCE(MAX(seq), 0) FROM {SWITCH} s"
            " WHERE s.supplier_key = NEW.supplier_key)",
        ),
    )
    _trigger(
        SWITCH,
        "enables_an_own_epr",
        "INSERT",
        _raise(
            f"{SWITCH}: an ENABLE names an EPR of its own supplier",
            f"NEW.action = 'ENABLE' AND NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
            " WHERE r.digest = NEW.epr_digest AND r.kind = 'EXTRACTION_PROFILE'"
            " AND r.supplier_key = NEW.supplier_key"
            " AND json_extract(NEW.bundle_key, '$[3]') = NEW.epr_digest)",
        ),
    )
    # The P2 lifecycle: SHADOW is entered and left only as the switch says (P3 B1 closure).
    _trigger(
        TRANSITIONS,
        "shadow_follows_the_switch",
        "INSERT",
        _raise(
            f"{TRANSITIONS}: SHADOW is entered only under an ENABLE of this EPR, and left for"
            " DRAFT only when the switch no longer enables it",
            f"(NEW.to_state = 'SHADOW' AND {_latest_switch_of_epr()} IS NOT"
            " ('ENABLE:' || NEW.epr_digest))"
            " OR (NEW.from_state = 'SHADOW' AND NEW.to_state = 'DRAFT'"
            f" AND {_latest_switch_of_epr()} IS ('ENABLE:' || NEW.epr_digest))",
        ),
    )
    # Raw records: never updated; deleted only by their retention; bound to the canonical run's
    # frozen ENABLED decision, its supplier and bundle, and to that run's own revision (B3).
    _immutable(RECORDS, deletable=True)
    _trigger(
        RECORDS,
        "of_its_frozen_run",
        "INSERT",
        _raise(
            f"{RECORDS}: a raw record belongs to a run frozen ENABLED for its supplier and bundle",
            f"NOT EXISTS (SELECT 1 FROM {RUNS} r WHERE r.collection_run_id = NEW.collection_run_id"
            " AND r.supplier_key = NEW.supplier_key AND r.shadow_decision = 'ENABLED'"
            " AND r.shadow_bundle_key = NEW.bundle_key)"
            " OR (NEW.revision_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM"
            " product_facts_revisions p WHERE p.revision_id = NEW.revision_id"
            " AND p.collection_run_id = NEW.collection_run_id))",
        ),
    )
    # Windows: append-only, one open window per supplier, events in their own order.
    _immutable(WINDOWS)
    _trigger(
        WINDOWS,
        "one_open_per_supplier",
        "INSERT",
        _raise(
            f"{WINDOWS}: at most one window per supplier is open",
            f"EXISTS (SELECT 1 FROM {WINDOWS} w WHERE w.supplier_key = NEW.supplier_key"
            f" AND (SELECT e.kind FROM {WINDOW_EVENTS} e WHERE e.window_id = w.window_id"
            " ORDER BY e.seq DESC LIMIT 1) = 'DECLARED')",
        ),
    )
    _immutable(WINDOW_EVENTS)
    previous = (
        f"(SELECT e.kind FROM {WINDOW_EVENTS} e WHERE e.window_id = NEW.window_id"
        " AND e.seq = NEW.seq - 1)"
    )
    _trigger(
        WINDOW_EVENTS,
        "follows",
        "INSERT",
        _raise(
            f"{WINDOW_EVENTS}: a window moves DECLARED, ENDED, CLOSED, then SUPERSEDED at most",
            f"NEW.seq <> 1 + (SELECT COALESCE(MAX(seq), 0) FROM {WINDOW_EVENTS} e"
            " WHERE e.window_id = NEW.window_id)"
            f" OR (NEW.kind = 'ENDED' AND {previous} IS NOT 'DECLARED')"
            f" OR (NEW.kind = 'CLOSED' AND {previous} IS NOT 'ENDED')"
            f" OR (NEW.kind = 'SUPERSEDED' AND {previous} IS NOT 'CLOSED')",
        ),
    )
    # The ledger: append-only, in order, one window per run, and nothing after a terminal state.
    # A run's events name exactly the window its frozen decision makes it eligible for (B3).
    _immutable(LEDGER)
    _trigger(
        LEDGER,
        "in_its_frozen_window",
        "INSERT",
        _raise(
            f"{LEDGER}: an event names the window the run froze its eligibility for",
            f"NOT EXISTS (SELECT 1 FROM {RUNS} r, {WINDOWS} w"
            " WHERE r.collection_run_id = NEW.collection_run_id AND w.window_id = NEW.window_id"
            " AND r.supplier_key = w.supplier_key AND r.shadow_decision = 'ENABLED'"
            " AND r.shadow_bundle_key = w.bundle_key"
            " AND r.first_product_read_at >= w.declared_at"
            f" AND NOT EXISTS (SELECT 1 FROM {WINDOW_EVENTS} e WHERE e.window_id = w.window_id"
            " AND e.kind = 'ENDED' AND e.occurred_at <= r.first_product_read_at))",
        ),
    )
    before = (
        f"(SELECT l.count_as || ':' || COALESCE(l.cause, '') || ':' || l.window_id"
        f" FROM {LEDGER} l WHERE l.collection_run_id = NEW.collection_run_id"
        " AND l.seq = NEW.seq - 1)"
    )
    _trigger(
        LEDGER,
        "follows",
        "INSERT",
        _raise(
            f"{LEDGER}: an event follows the one before it, only an UNRESOLVED_MISMATCH, in the"
            " same window",
            f"NEW.seq <> 1 + (SELECT COALESCE(MAX(seq), 0) FROM {LEDGER} l"
            " WHERE l.collection_run_id = NEW.collection_run_id)"
            f" OR (NEW.seq > 1 AND {before} IS NOT"
            " ('INCOMPLETE:UNRESOLVED_MISMATCH:' || NEW.window_id))",
        ),
    )


def _latest_switch_of_epr() -> str:
    return (
        f"(SELECT s.action || ':' || COALESCE(s.epr_digest, '') FROM {SWITCH} s"
        f" WHERE s.supplier_key = (SELECT r.supplier_key FROM {REVISIONS} r"
        " WHERE r.digest = NEW.epr_digest) ORDER BY s.seq DESC LIMIT 1)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in CREATED:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} holds {count} row(s): Adaptive shadow evidence is never silently"
                " destroyed"
            )
    held = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {RUNS} WHERE shadow_decision = 'ENABLED'")
    ).scalar_one()
    if held:
        raise RuntimeError(
            f"{held} collection run(s) froze an ENABLED shadow decision: shadow evidence is never"
            " silently destroyed"
        )
    op.execute(f"DROP TRIGGER trg_{TRANSITIONS}_shadow_follows_the_switch")
    for table in CREATED:
        op.drop_table(table)
    op.execute(f"DROP TRIGGER trg_{RUNS}_recovery_update")
    op.execute(f"DROP TRIGGER trg_{RUNS}_recovery_insert")
    op.execute(f"DROP TRIGGER trg_{RUNS}_shadow_frozen")
    op.execute(f"DROP TRIGGER trg_{RUNS}_shadow_shape_insert")
    for column in reversed(RUN_COLUMNS):
        op.execute(f"ALTER TABLE {RUNS} DROP COLUMN {column}")
