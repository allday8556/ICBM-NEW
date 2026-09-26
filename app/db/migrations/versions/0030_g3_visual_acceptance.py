"""Gate 3 area 3: the reviewed populated visual acceptance record.

Revision ID: 0030_g3_visual_acceptance
Revises: 0029_g3_restore_retention
Create Date: 2026-09-26

Issue #89 Gate 3, area 3 of ADR-0018 §12, under ADR-0018 §9 (authorization 5843380581). It creates
one append-only table and its triggers, and touches no other table, row, trigger or index.

**What the database enforces.** ``visual_acceptances``: a record names the git commit it ran at
(40 hex), the running code digest and report digest (64 hex each), its schema head, harness
version and scenario, at least one target and one check, the reviewer and the review reference
that accepted it, and one JSON object of sanitized evidence. Triggers refuse any update and any
delete.

**Downgrade fails closed.** It refuses while the table holds a row: acceptance history is never
silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_g3_visual_acceptance"
down_revision: str | None = "0029_g3_restore_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "visual_acceptances"
INDEX = "ix_visual_acceptances_code_digest"


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{TABLE}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("acceptance_id", sa.String(length=36), nullable=False),
        sa.Column("code_sha", sa.String(length=40), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("schema_head", sa.String(length=64), nullable=False),
        sa.Column("report_digest", sa.String(length=64), nullable=False),
        sa.Column("harness_version", sa.String(length=64), nullable=False),
        sa.Column("scenario", sa.String(length=64), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("check_count", sa.Integer(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=False),
        sa.Column("authorization_ref", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        _check("length(code_sha) = 40 AND code_sha NOT GLOB '*[^0-9a-f]*'", "code_sha_hex"),
        _check(_hex64("code_digest"), "code_digest_hex"),
        _check(_hex64("report_digest"), "report_digest_hex"),
        _check(
            "json_valid(evidence_json) AND json_type(evidence_json) = 'object'",
            "evidence_is_object",
        ),
        _check("target_count > 0 AND check_count > 0", "checks_present"),
        _check(
            "schema_head <> '' AND harness_version <> '' AND scenario <> ''", "identity_present"
        ),
        _check("approved_by <> '' AND authorization_ref <> ''", "review_present"),
        _check("actor <> '' AND correlation_id <> ''", "actor_present"),
        sa.PrimaryKeyConstraint("acceptance_id", name=op.f(f"pk_{TABLE}")),
    )
    op.create_index(INDEX, TABLE, ["code_digest", "schema_head"])
    for name, event in (("no_update", "UPDATE"), ("no_delete", "DELETE")):
        op.execute(
            f"CREATE TRIGGER trg_{TABLE}_{name} BEFORE {event} ON {TABLE} BEGIN"
            f" SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
        )


def downgrade() -> None:
    count = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar_one()
    if count:
        raise RuntimeError(
            f"{TABLE} holds {count} row(s): visual acceptance history is never silently destroyed"
        )
    op.execute(f"DROP TRIGGER trg_{TABLE}_no_delete")
    op.execute(f"DROP TRIGGER trg_{TABLE}_no_update")
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
