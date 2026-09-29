"""Adaptive Collector Phase C C1 PREP-0: durable accounting of actual Phase C sends.

Revision ID: 0028_phase_c_read_accounting
Revises: 0027_adaptive_capture_seam
Create Date: 2026-09-26

Issue #110 C1 PREP-0 (authorization `5841947773`). It adds three append-only tables. It changes
no existing table, rewrites no row and backfills nothing.

**What it holds.**
- ``adaptive_phase_c_read_budgets``: the frozen read ceilings of one campaign stage, one row per
  request class. ``CAMPAIGN`` bounds the whole stage; ``ATTEMPT`` bounds one collection attempt.
- ``adaptive_phase_c_reads``: each actual send of a Phase-C-accounted run, reserved before it was
  transmitted. It records the class, the run, the attempt and the digest of the subject. It never
  records a URL.
- ``adaptive_phase_c_read_refusals``: each send refused before transmission, and why.

**What the database enforces.** Nothing is ever updated or deleted. A read is admitted only when
all of the following hold:
- a ceiling exists for its campaign, stage and class;
- its run froze a ``REQUESTED`` capture of a request of that same campaign;
- the class's count stays under its ceiling, counted over the stage (``CAMPAIGN``) or over the
  run's attempt (``ATTEMPT``).

**Downgrade fails closed.** It refuses while any budget, read or refusal exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_phase_c_read_accounting"
down_revision: str | None = "0027_adaptive_capture_seam"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BUDGETS = "adaptive_phase_c_read_budgets"
READS = "adaptive_phase_c_reads"
REFUSALS = "adaptive_phase_c_read_refusals"
CREATED = (REFUSALS, READS, BUDGETS)
CLASSES = (
    "PRODUCT_READ",
    "IMAGE_REQUEST",
    "POLICY_READ",
    "CONNECT_CONTROL_READ",
    "CONNECT_PROTECTED_READ",
    "CONNECT_AUTHENTICATE",
)
CLASS_CHECK = "request_class IN (" + ", ".join(f"'{c}'" for c in CLASSES) + ")"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


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
        BUDGETS,
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=4), nullable=False),
        sa.Column("request_class", sa.String(length=24), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("ceiling", sa.Integer(), nullable=False),
        sa.Column("registered_at", sa.DateTime(), nullable=False),
        _check(BUDGETS, "campaign_id <> ''", "campaign_present"),
        _check(BUDGETS, CLASS_CHECK, "class_valid"),
        _check(BUDGETS, "scope IN ('CAMPAIGN', 'ATTEMPT')", "scope_valid"),
        _check(BUDGETS, "ceiling >= 0", "ceiling_valid"),
        sa.PrimaryKeyConstraint(
            "campaign_id", "stage", "request_class", name=op.f(f"pk_{BUDGETS}")
        ),
    )
    op.create_table(
        READS,
        sa.Column("read_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=4), nullable=False),
        sa.Column("request_class", sa.String(length=24), nullable=False),
        sa.Column("collection_run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("subject_digest", sa.String(length=64), nullable=False),
        sa.Column("reserved_at", sa.DateTime(), nullable=False),
        _check(READS, CLASS_CHECK, "class_valid"),
        _check(READS, "attempt_no >= 1", "attempt_valid"),
        _check(
            READS,
            "length(subject_digest) = 64 AND subject_digest NOT GLOB '*[^0-9a-f]*'",
            "subject_hex",
        ),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            ["collection_runs.collection_run_id"],
            name=op.f(f"fk_{READS}_collection_run_id_collection_runs"),
        ),
        sa.PrimaryKeyConstraint("read_id", name=op.f(f"pk_{READS}")),
    )
    op.create_index("ix_adaptive_phase_c_reads_campaign_id", READS, ["campaign_id"])
    op.create_table(
        REFUSALS,
        sa.Column("refusal_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=4), nullable=False),
        sa.Column("request_class", sa.String(length=24), nullable=False),
        sa.Column("collection_run_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("refused_at", sa.DateTime(), nullable=False),
        _check(REFUSALS, "reason <> ''", "reason_present"),
        sa.PrimaryKeyConstraint("refusal_id", name=op.f(f"pk_{REFUSALS}")),
    )
    op.create_index("ix_adaptive_phase_c_read_refusals_campaign_id", REFUSALS, ["campaign_id"])
    for table in CREATED:
        _immutable(table)
    budget = (
        f"(SELECT b.{{column}} FROM {BUDGETS} b WHERE b.campaign_id = NEW.campaign_id"
        " AND b.stage = NEW.stage AND b.request_class = NEW.request_class)"
    )
    _trigger(
        READS,
        "admitted",
        "INSERT",
        _raise(
            f"{READS}: a Phase C send is reserved only under its campaign's frozen ceiling",
            f"NOT EXISTS {budget.format(column='ceiling')}",
        )
        + _raise(
            f"{READS}: a Phase C send belongs to a run frozen REQUESTED for a request of its"
            " campaign",
            "NOT EXISTS (SELECT 1 FROM collection_runs r JOIN adaptive_capture_requests q"
            " ON q.request_id = r.capture_request_id"
            " WHERE r.collection_run_id = NEW.collection_run_id"
            " AND r.capture_decision = 'REQUESTED' AND q.campaign_id = NEW.campaign_id)",
        )
        + _raise(
            f"{READS}: PHASE_C_CEILING",
            f"{budget.format(column='scope')} = 'CAMPAIGN' AND (SELECT COUNT(*) FROM {READS} x"
            " WHERE x.campaign_id = NEW.campaign_id AND x.stage = NEW.stage"
            f" AND x.request_class = NEW.request_class) >= {budget.format(column='ceiling')}",
        )
        + _raise(
            f"{READS}: PHASE_C_CEILING",
            f"{budget.format(column='scope')} = 'ATTEMPT' AND (SELECT COUNT(*) FROM {READS} x"
            " WHERE x.campaign_id = NEW.campaign_id AND x.stage = NEW.stage"
            " AND x.request_class = NEW.request_class"
            " AND x.collection_run_id = NEW.collection_run_id"
            f" AND x.attempt_no = NEW.attempt_no) >= {budget.format(column='ceiling')}",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in CREATED:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} holds {count} row(s): Phase C send accounting is never silently destroyed"
            )
    for table in CREATED:
        op.drop_table(table)
