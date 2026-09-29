"""Adaptive Collector Phase C C0: the in-memory ValidationSample capture seam.

Revision ID: 0027_adaptive_capture_seam
Revises: 0026_g3_live_authority
Create Date: 2026-09-25

Issue #110 Phase C stage C0 (authorization `5826469852`, plan review `5312911203`, supplement
`5313045448`). It adds two nullable columns on ``collection_runs``, one unique index, two tables
and triggers; it rewrites no row and backfills nothing.

**The canonical run record.** ``capture_decision`` (``REQUESTED`` / ``OFF``) and
``capture_request_id`` are frozen at a run's genuinely first product-read reservation. The default
is ``OFF``. A run first read before this migration keeps NULL and is never captured. A request is
consumed by at most one run (unique index), and a frozen decision never changes.

**What it holds.**
- ``adaptive_capture_requests``: a Phase C campaign's request that the next ordinary collection of
  one target keep a capture candidate. It names a target digest, never a URL, and lapses within a
  bounded lifetime.
- ``adaptive_capture_candidates``: what a requested run's capture produced — the sanitized
  candidate (never the page body), or why there is none. At most one per run; it belongs to that
  run's request and that run's own revision.
- ``adaptive_phase_c_commands`` / ``adaptive_phase_c_command_results``: every Phase C harness
  command that changes this data root, reserved under its stable correlation before the change,
  and what it was proven to have done (``APPLIED``, ``RECOVERED`` or ``NOT_APPLIED``). A
  reservation without a result is an unresolved command; while one exists no campaign on this data
  root acts (review ``5313663701`` follow-up).

**What the database enforces.** Requests and candidates are never updated or deleted. A run's
capture decision has its shape, names a request of its own supplier, and is never changed once
frozen. A candidate belongs to a run frozen ``REQUESTED`` for exactly its request and names that
run's own revision.

**Downgrade fails closed.** It refuses while any request, candidate or harness command exists, or any run
froze a ``REQUESTED`` decision. An ``OFF`` decision carries no capture evidence; the step down keeps every
run row and drops only the new columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_adaptive_capture_seam"
down_revision: str | None = "0026_g3_live_authority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "collection_runs"
REQUESTS = "adaptive_capture_requests"
CANDIDATES = "adaptive_capture_candidates"
COMMANDS = "adaptive_phase_c_commands"
RESULTS = "adaptive_phase_c_command_results"
CREATED = (RESULTS, COMMANDS, CANDIDATES, REQUESTS)
RUN_COLUMNS = ("capture_decision", "capture_request_id")
RUN_INDEX = "ix_collection_runs_capture_request_id"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    quoted = message.replace("'", "''")  # a message is an SQL string literal
    return f"SELECT RAISE(ABORT, '{quoted}') WHERE {condition};"


def _immutable(table: str) -> None:
    _trigger(table, "no_update", "UPDATE", _raise(f"a {table} row is never updated", "1"))
    _trigger(table, "no_delete", "DELETE", _raise(f"a {table} row is never deleted", "1"))


def upgrade() -> None:
    op.create_table(
        REQUESTS,
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("target_digest", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        _check(REQUESTS, "campaign_id <> ''", "campaign_present"),
        _check(REQUESTS, "supplier_key <> ''", "supplier_present"),
        _check(REQUESTS, _hex64("target_digest"), "target_hex"),
        _check(REQUESTS, "requested_by <> ''", "author_present"),
        _check(REQUESTS, "correlation_id <> ''", "correlation_present"),
        _check(REQUESTS, "expires_at > requested_at", "expires_after_request"),
        sa.PrimaryKeyConstraint("request_id", name=op.f(f"pk_{REQUESTS}")),
    )
    op.create_index(
        "ix_adaptive_capture_requests_supplier_key_target_digest",
        REQUESTS,
        ["supplier_key", "target_digest"],
    )
    op.create_table(
        CANDIDATES,
        sa.Column("collection_run_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("structure_json", sa.Text(), nullable=True),
        sa.Column("excluded_json", sa.Text(), nullable=True),
        sa.Column("removals_json", sa.Text(), nullable=True),
        sa.Column("candidate_digest", sa.String(length=64), nullable=True),
        sa.Column("refusal", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        _check(CANDIDATES, "status IN ('CAPTURED', 'REFUSED')", "status_valid"),
        _check(
            CANDIDATES,
            "(status = 'CAPTURED' AND structure_json IS NOT NULL AND excluded_json IS NOT NULL"
            " AND removals_json IS NOT NULL AND candidate_digest IS NOT NULL AND refusal IS NULL)"
            " OR (status = 'REFUSED' AND structure_json IS NULL AND excluded_json IS NULL"
            " AND removals_json IS NULL AND candidate_digest IS NULL AND refusal IS NOT NULL"
            " AND refusal <> '')",
            "status_shape",
        ),
        _check(
            CANDIDATES,
            f"candidate_digest IS NULL OR ({_hex64('candidate_digest')})",
            "digest_hex",
        ),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            [f"{RUNS}.collection_run_id"],
            name=op.f(f"fk_{CANDIDATES}_collection_run_id_{RUNS}"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            [f"{REQUESTS}.request_id"],
            name=op.f(f"fk_{CANDIDATES}_request_id_{REQUESTS}"),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            ["product_facts_revisions.revision_id"],
            name=op.f(f"fk_{CANDIDATES}_revision_id_product_facts_revisions"),
        ),
        sa.PrimaryKeyConstraint("collection_run_id", name=op.f(f"pk_{CANDIDATES}")),
    )

    op.create_table(
        COMMANDS,
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("command", sa.String(length=24), nullable=False),
        sa.Column("reserved_at", sa.DateTime(), nullable=False),
        _check(COMMANDS, "correlation_id <> ''", "correlation_present"),
        _check(COMMANDS, "campaign_id <> ''", "campaign_present"),
        _check(COMMANDS, "command <> ''", "command_present"),
        sa.PrimaryKeyConstraint("correlation_id", name=op.f(f"pk_{COMMANDS}")),
    )
    op.create_index("ix_adaptive_phase_c_commands_campaign_id", COMMANDS, ["campaign_id"])
    op.create_table(
        RESULTS,
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=12), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=False),
        _check(RESULTS, "outcome IN ('APPLIED', 'RECOVERED', 'NOT_APPLIED')", "outcome_valid"),
        sa.ForeignKeyConstraint(
            ["correlation_id"],
            [f"{COMMANDS}.correlation_id"],
            name=op.f(f"fk_{RESULTS}_correlation_id_{COMMANDS}"),
        ),
        sa.PrimaryKeyConstraint("correlation_id", name=op.f(f"pk_{RESULTS}")),
    )

    # ------------------------------------------------------------ the canonical run record
    op.add_column(RUNS, sa.Column("capture_decision", sa.String(length=10), nullable=True))
    op.add_column(RUNS, sa.Column("capture_request_id", sa.String(length=36), nullable=True))
    op.create_index(RUN_INDEX, RUNS, ["capture_request_id"], unique=True)
    shape = _raise(
        f"{RUNS}: a capture decision is REQUESTED with a request of its own supplier, or OFF",
        "NOT ((NEW.capture_decision IS NULL AND NEW.capture_request_id IS NULL)"
        " OR (NEW.capture_decision = 'OFF' AND NEW.capture_request_id IS NULL)"
        " OR (NEW.capture_decision = 'REQUESTED' AND EXISTS (SELECT 1 FROM"
        f" {REQUESTS} q WHERE q.request_id = NEW.capture_request_id"
        " AND q.supplier_key = NEW.supplier_key)))",
    )
    _trigger(RUNS, "capture_shape_insert", "INSERT", shape)
    frozen = _raise(
        f"{RUNS}: a frozen capture decision is never changed",
        "OLD.capture_decision IS NOT NULL AND (NEW.capture_decision IS NOT OLD.capture_decision"
        " OR NEW.capture_request_id IS NOT OLD.capture_request_id)",
    )
    op.execute(
        f"CREATE TRIGGER trg_{RUNS}_capture_frozen BEFORE UPDATE ON {RUNS}"
        f" WHEN NEW.capture_decision IS NOT OLD.capture_decision"
        f" OR NEW.capture_request_id IS NOT OLD.capture_request_id"
        f" BEGIN {frozen} {shape} END"
    )

    # ------------------------------------------------------------ requests and candidates
    _immutable(REQUESTS)
    _immutable(CANDIDATES)
    _immutable(COMMANDS)
    _immutable(RESULTS)
    _trigger(
        CANDIDATES,
        "of_its_requested_run",
        "INSERT",
        _raise(
            f"{CANDIDATES}: a candidate belongs to a run frozen REQUESTED for exactly its request"
            " and names that run's own revision",
            f"NOT EXISTS (SELECT 1 FROM {RUNS} r, product_facts_revisions p"
            " WHERE r.collection_run_id = NEW.collection_run_id"
            " AND r.capture_decision = 'REQUESTED' AND r.capture_request_id = NEW.request_id"
            " AND r.supplier_key = NEW.supplier_key AND p.revision_id = NEW.revision_id"
            " AND p.collection_run_id = NEW.collection_run_id)",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in CREATED:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} holds {count} row(s): Adaptive capture evidence is never silently"
                " destroyed"
            )
    held = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {RUNS} WHERE capture_decision = 'REQUESTED'")
    ).scalar_one()
    if held:
        raise RuntimeError(
            f"{held} collection run(s) froze a REQUESTED capture: capture evidence is never"
            " silently destroyed"
        )
    # The tables go first: their triggers name the run columns dropped after them.
    for table in CREATED:
        op.drop_table(table)
    op.execute(f"DROP TRIGGER trg_{RUNS}_capture_frozen")
    op.execute(f"DROP TRIGGER trg_{RUNS}_capture_shape_insert")
    op.drop_index(RUN_INDEX, table_name=RUNS)
    for column in reversed(RUN_COLUMNS):
        op.execute(f"ALTER TABLE {RUNS} DROP COLUMN {column}")
