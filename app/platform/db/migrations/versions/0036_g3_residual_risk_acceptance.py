"""Gate 3: the durable proof of the user and architect residual-risk acceptance.

Revision ID: 0036_g3_residual_risk_acceptance
Revises: 0035_extension_list_queue
Create Date: 2026-10-02

ADR-0018 §6.1 and G3-30: opening any real canary needs an explicit user and architect acceptance of
the residual risk, recorded in GitHub. GitHub holds that decision. This migration adds only the
durable proof the existing send-time layer ``RESIDUAL_RISK_ACCEPTED`` reads. It creates one table
with its unique index and its triggers, changes no existing row, table, trigger, index or
constraint, and backfills nothing.

``residual_risk_acceptances`` — one row per recorded acceptance:
- the scope is the canonical account and the exact risk contract version, with the SHA-256 of
  the residual-risk statement that contract names;
- the evidence is the user's and the architect's GitHub comment, each by its numeric id and the
  SHA-256 of its body; the two are never the same comment;
- ``seq`` is the previous maximum plus one of the exact scope ``marketplace_key ×
  marketplace_account_id × risk_contract_version``, so no mutable pointer exists;
- a row is never updated and never deleted.

It authorizes nothing: it is one precondition among the stack's layers.

**Downgrade fails closed.** It refuses while any row exists: acceptance evidence is never silently
destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_g3_residual_risk_acceptance"
down_revision: str | None = "0035_extension_list_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECORDS = "residual_risk_acceptances"
ACCOUNTS = "marketplace_accounts"
UX_SCOPE = f"ux_{RECORDS}_scope_seq"
TRIGGERS: tuple[str, ...] = (
    f"trg_{RECORDS}_append",
    f"trg_{RECORDS}_no_update",
    f"trg_{RECORDS}_no_delete",
)


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{RECORDS}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _comment_id(column: str) -> str:
    return f"length({column}) BETWEEN 6 AND 20 AND {column} NOT GLOB '*[^0-9]*'"


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _trigger(name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {RECORDS} BEGIN {body} END")


def upgrade() -> None:
    op.create_table(
        RECORDS,
        sa.Column("acceptance_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("risk_contract_version", sa.String(length=64), nullable=False),
        sa.Column("risk_statement_digest", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("user_comment_id", sa.String(length=20), nullable=False),
        sa.Column("user_comment_digest", sa.String(length=64), nullable=False),
        sa.Column("architect_comment_id", sa.String(length=20), nullable=False),
        sa.Column("architect_comment_digest", sa.String(length=64), nullable=False),
        sa.Column("recorded_by", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        _check("seq >= 1", "seq_positive"),
        _check(_hex64("risk_statement_digest"), "risk_statement_digest_hex"),
        _check(_comment_id("user_comment_id"), "user_comment_id"),
        _check(_comment_id("architect_comment_id"), "architect_comment_id"),
        _check(_hex64("user_comment_digest"), "user_comment_digest_hex"),
        _check(_hex64("architect_comment_digest"), "architect_comment_digest_hex"),
        _check("user_comment_id <> architect_comment_id", "two_distinct_acceptances"),
        _check("risk_contract_version <> '' AND recorded_by <> ''", "identity_present"),
        sa.ForeignKeyConstraint(
            ["marketplace_key", "marketplace_account_id"],
            [f"{ACCOUNTS}.marketplace_key", f"{ACCOUNTS}.marketplace_account_id"],
            name=op.f(f"fk_{RECORDS}_marketplace_key_{ACCOUNTS}"),
        ),
        sa.PrimaryKeyConstraint("acceptance_id", name=op.f(f"pk_{RECORDS}")),
    )
    op.create_index(
        UX_SCOPE,
        RECORDS,
        ["marketplace_key", "marketplace_account_id", "risk_contract_version", "seq"],
        unique=True,
    )
    _trigger(
        f"trg_{RECORDS}_append",
        "INSERT",
        _raise(
            f"{RECORDS}: records are appended in order within their scope",
            f"NEW.seq <> (SELECT COALESCE(MAX(seq), 0) + 1 FROM {RECORDS}"
            " WHERE marketplace_key = NEW.marketplace_key"
            " AND marketplace_account_id = NEW.marketplace_account_id"
            " AND risk_contract_version = NEW.risk_contract_version)",
        ),
    )
    _trigger(f"trg_{RECORDS}_no_update", "UPDATE", _raise(f"{RECORDS} is append-only", "1"))
    _trigger(f"trg_{RECORDS}_no_delete", "DELETE", _raise(f"{RECORDS} is never deleted", "1"))


def downgrade() -> None:
    count = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {RECORDS}")).scalar_one()
    if count:
        raise RuntimeError(
            f"{count} residual-risk acceptance record(s) exist: acceptance evidence is never"
            " silently destroyed"
        )
    for name in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
    op.drop_index(UX_SCOPE, table_name=RECORDS)
    op.drop_table(RECORDS)
