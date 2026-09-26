"""Gate 3 area 2: the restore-drill record and the evidence-retention proof record.

Revision ID: 0028_g3_restore_retention
Revises: 0027_adaptive_capture_seam
Create Date: 2026-09-26

Issue #89 Gate 3, area 2 of ADR-0018 §12, under ADR-0018 §7 and §8. It creates two append-only
tables and their triggers, and touches no other table, row, trigger or index.

**What the database enforces.**
- ``restore_drills``: a drill names its stage, its target digest (hex) and its restore-root
  digest; a PASSED drill has no failure code, an ``ok`` integrity result and a backup digest; its
  evidence is one JSON object. Triggers refuse any update and any delete.
- ``retention_proofs``: a proof names its schema head and check digest; PASSED exactly when it has
  no failure code; its checks are one JSON object. Triggers refuse any update and any delete.

**Downgrade fails closed.** It refuses while either table holds a row: proof history is never
silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_g3_restore_retention"
down_revision: str | None = "0027_adaptive_capture_seam"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DRILLS = "restore_drills"
PROOFS = "retention_proofs"
CREATED = (DRILLS, PROOFS)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}');"


def upgrade() -> None:
    op.create_table(
        DRILLS,
        sa.Column("drill_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("unit_ref", sa.String(length=64), nullable=False),
        sa.Column("target_digest", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("schema_head", sa.String(length=64), nullable=False),
        sa.Column("integrity", sa.String(length=32), nullable=True),
        sa.Column("backup_digest", sa.String(length=64), nullable=True),
        sa.Column("restore_root_digest", sa.String(length=64), nullable=False),
        sa.Column("element_count", sa.Integer(), nullable=False),
        sa.Column("absent_count", sa.Integer(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        _check(DRILLS, "stage IN ('ASSET', 'CREATE')", "stage_valid"),
        _check(DRILLS, "verdict IN ('PASSED', 'FAILED')", "verdict_valid"),
        _check(DRILLS, _hex64("target_digest"), "target_digest_hex"),
        _check(DRILLS, "backup_digest IS NULL OR " + _hex64("backup_digest"), "backup_digest_hex"),
        _check(DRILLS, _hex64("restore_root_digest"), "restore_root_digest_hex"),
        _check(
            DRILLS,
            "(verdict = 'PASSED') = (failure_code IS NULL)"
            " AND (verdict <> 'PASSED' OR (integrity = 'ok' AND backup_digest IS NOT NULL))",
            "passed_is_complete",
        ),
        _check(
            DRILLS,
            "json_valid(evidence_json) AND json_type(evidence_json) = 'object'",
            "evidence_is_object",
        ),
        _check(DRILLS, "unit_ref <> '' AND schema_head <> ''", "identity_present"),
        _check(DRILLS, "actor <> '' AND correlation_id <> ''", "actor_present"),
        sa.PrimaryKeyConstraint("drill_id", name=op.f(f"pk_{DRILLS}")),
    )
    op.create_index("ix_restore_drills_stage_target_digest", DRILLS, ["stage", "target_digest"])
    op.create_table(
        PROOFS,
        sa.Column("proof_id", sa.String(length=36), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("schema_head", sa.String(length=64), nullable=False),
        sa.Column("check_digest", sa.String(length=64), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        _check(PROOFS, "verdict IN ('PASSED', 'FAILED')", "verdict_valid"),
        _check(PROOFS, _hex64("check_digest"), "check_digest_hex"),
        _check(PROOFS, "(verdict = 'PASSED') = (failure_code IS NULL)", "passed_is_clean"),
        _check(
            PROOFS,
            "json_valid(checks_json) AND json_type(checks_json) = 'object'",
            "checks_is_object",
        ),
        _check(PROOFS, "schema_head <> ''", "schema_head_present"),
        _check(PROOFS, "actor <> '' AND correlation_id <> ''", "actor_present"),
        sa.PrimaryKeyConstraint("proof_id", name=op.f(f"pk_{PROOFS}")),
    )
    for table in CREATED:
        _trigger(table, "no_update", "UPDATE", _raise(f"{table} is append-only"))
        _trigger(table, "no_delete", "DELETE", _raise(f"{table} is append-only"))


def downgrade() -> None:
    bind = op.get_bind()
    for table in CREATED:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"{table} holds {count} row(s): restore and retention proof history is never"
                " silently destroyed"
            )
    for table in CREATED:
        op.execute(f"DROP TRIGGER trg_{table}_no_delete")
        op.execute(f"DROP TRIGGER trg_{table}_no_update")
    op.drop_index("ix_restore_drills_stage_target_digest", table_name=DRILLS)
    for table in reversed(CREATED):
        op.drop_table(table)
