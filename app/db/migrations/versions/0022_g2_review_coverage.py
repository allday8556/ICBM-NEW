"""Gate 2 G2-B: the review producers' coverage watermark.

Revision ID: 0022_g2_review_coverage
Revises: 0021_g2_review_items
Create Date: 2026-09-24

Issue #89 Gate 2, slice G2-B, under ADR-0016 §4 and §7 and the carry-forwards of the G2-A merge
comment `5806614407`. It adds exactly one table and touches no existing table, row, trigger or
index.

**What it holds.** One row per review producer:
- the watermark: when the producer's last *complete* full reconciliation ended, which process
  run completed it, and when that pass started;
- the newest known indexing failure still unrecovered, as a code of ours.

Whether a producer's coverage is current is derived from that row, the current process run and a
finite freshness bound. It is never stored.

**What the database enforces.**
- A watermark is complete or absent, and it never moves backwards.
- The count of completed passes never falls.
- A failure is a time and a code together, and the count of recorded failures never falls. A
  pass clears a failure only if that count has not moved since the pass began, never by comparing
  times.
- A row is never deleted.

**Downgrade fails closed.** It refuses while the table holds a row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_g2_review_coverage"
down_revision: str | None = "0021_g2_review_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COVERAGE = "review_coverage"


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{COVERAGE}_{name}"))


def _trigger(name: str, event: str, body: str) -> None:
    op.execute(
        f"CREATE TRIGGER trg_{COVERAGE}_{name} BEFORE {event} ON {COVERAGE} BEGIN {body} END"
    )


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    op.create_table(
        COVERAGE,
        sa.Column("producer", sa.String(length=64), nullable=False),
        sa.Column("process_run_id", sa.String(length=36), nullable=True),
        sa.Column("pass_started_at", sa.DateTime(), nullable=True),
        sa.Column("watermark_at", sa.DateTime(), nullable=True),
        sa.Column("full_passes", sa.Integer(), nullable=False),
        sa.Column("failure_at", sa.DateTime(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failures_recorded", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        _check("producer <> ''", "producer_present"),
        _check(
            "(watermark_at IS NULL) = (process_run_id IS NULL)"
            " AND (watermark_at IS NULL) = (pass_started_at IS NULL)",
            "watermark_complete",
        ),
        _check("pass_started_at IS NULL OR pass_started_at <= watermark_at", "pass_ordered"),
        _check("full_passes >= 0", "full_passes_counted"),
        _check("(failure_at IS NULL) = (failure_code IS NULL)", "failure_complete"),
        _check(
            "failures_recorded >= 0 AND (failures_recorded > 0 OR failure_at IS NULL)",
            "failures_counted",
        ),
        sa.PrimaryKeyConstraint("producer", name=op.f(f"pk_{COVERAGE}")),
    )
    _trigger(
        "watermark_forward",
        "UPDATE",
        _raise(
            f"{COVERAGE}: a watermark never moves backwards",
            "OLD.watermark_at IS NOT NULL AND (NEW.watermark_at IS NULL"
            " OR NEW.watermark_at < OLD.watermark_at)",
        ),
    )
    _trigger(
        "passes_forward",
        "UPDATE",
        _raise(f"{COVERAGE}: completed passes never fall", "NEW.full_passes < OLD.full_passes"),
    )
    _trigger(
        "failures_forward",
        "UPDATE",
        _raise(
            f"{COVERAGE}: recorded failures never fall",
            "NEW.failures_recorded < OLD.failures_recorded",
        ),
    )
    _trigger("no_delete", "DELETE", _raise(f"a {COVERAGE} row is never deleted", "1"))


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {COVERAGE}")).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot drop the review coverage owner: {held} row(s) in {COVERAGE} are held;"
            " coverage history is never silently destroyed"
        )
    op.drop_table(COVERAGE)
