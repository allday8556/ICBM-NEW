"""M5 SEARCH positive-only reconcile: the durable reconcile-check owner and the channel identity.

Revision ID: 0031_m5_registration_reconcile
Revises: 0030_g3_visual_acceptance
Create Date: 2026-09-30

ADR-0014 §28.2 and §28.4, authorized concretely by the architect resolution on Issue #89
(comment ``5904349289``, H-S2 option (a)). It creates one table with its partial unique index and
triggers, adds one nullable column to ``registration_intents`` and one to
``marketplace_registrations``, and adds triggers for those columns. It changes no existing row,
trigger, index or constraint.

**A. ``registration_reconcile_checks``** — one row per reconcile check of one Intent, identified
by ``(intent_id, seq)``:
- ``seq`` is the previous maximum plus one;
- a check opens in flight — no ``finished_at``, ``result``, ``candidate_count``,
  ``evidence_digest`` or ``next_due_at`` — and only for an ``UNKNOWN`` Intent;
- at most one check per Intent is in flight (a partial unique index);
- finishing it is the only mutation ever allowed; afterwards the row is immutable, and it is
  never deleted;
- the finished result agrees with its count: ``ZERO`` is 0, ``ONE_*`` is 1, ``MULTIPLE`` is at
  least 2, and an unavailable or failed lookup may leave the count unknown;
- ``evidence_digest`` is the SHA-256 of the sanitized canonical evidence, never of wire bytes;
- ``next_due_at`` never precedes ``finished_at``.

**B. ``marketplace_channel_product_id``** (for SmartStore the ``STOREFARM`` ``channelProductNo``;
``marketplace_product_id`` stays the ``originProductNo``):
- on an Intent it exists only with an applied outcome, is written together with it, and never
  changes afterwards;
- a registration copies its confirmed Intent's value and never changes it;
- no historical row is backfilled with a guessed identity.

**Downgrade fails closed.** It refuses while any reconcile check or any channel identity exists:
reconcile evidence and provider identities are never silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_m5_registration_reconcile"
down_revision: str | None = "0030_g3_visual_acceptance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKS = "registration_reconcile_checks"
INTENTS = "registration_intents"
REGISTRATIONS = "marketplace_registrations"
CHANNEL = "marketplace_channel_product_id"
IN_FLIGHT = "ux_registration_reconcile_checks_in_flight"

TRIGGERS: tuple[tuple[str, str], ...] = (
    (f"trg_{CHECKS}_append", CHECKS),
    (f"trg_{CHECKS}_finish_once", CHECKS),
    (f"trg_{CHECKS}_no_delete", CHECKS),
    (f"trg_{INTENTS}_channel_open", INTENTS),
    (f"trg_{INTENTS}_channel_identity", INTENTS),
    (f"trg_{REGISTRATIONS}_channel_copied", REGISTRATIONS),
    (f"trg_{REGISTRATIONS}_channel_immutable", REGISTRATIONS),
)


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{CHECKS}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _trigger(name: str, event: str, table: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} BEGIN {body} END")


def upgrade() -> None:
    op.create_table(
        CHECKS,
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("result", sa.String(length=20), nullable=True),
        sa.Column("candidate_count", sa.Integer(), nullable=True),
        sa.Column("evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("next_due_at", sa.DateTime(), nullable=True),
        _check("seq > 0", "seq_positive"),
        _check("\"trigger\" IN ('AUTO', 'OPERATOR', 'OPERATOR_CANDIDATE')", "trigger_valid"),
        _check(
            "result IS NULL OR result IN ('ZERO', 'ONE_VERIFIED', 'ONE_MISMATCH', 'MULTIPLE',"
            " 'LOOKUP_UNAVAILABLE', 'ERROR')",
            "result_valid",
        ),
        _check(
            "(finished_at IS NULL) = (result IS NULL)"
            " AND (finished_at IS NULL) = (evidence_digest IS NULL)",
            "finished_together",
        ),
        _check(
            "(finished_at IS NOT NULL OR candidate_count IS NULL)"
            " AND (result IS NOT 'ZERO' OR candidate_count IS 0)"
            " AND (result IS NOT 'ONE_VERIFIED' OR candidate_count IS 1)"
            " AND (result IS NOT 'ONE_MISMATCH' OR candidate_count IS 1)"
            " AND (result IS NOT 'MULTIPLE' OR (candidate_count IS NOT NULL AND candidate_count >= 2))"
            " AND (candidate_count IS NULL OR candidate_count >= 0)",
            "count_agrees_with_result",
        ),
        _check(f"evidence_digest IS NULL OR ({_hex64('evidence_digest')})", "evidence_digest_hex"),
        _check("finished_at IS NULL OR finished_at >= started_at", "finished_after_start"),
        _check(
            "next_due_at IS NULL OR (finished_at IS NOT NULL AND next_due_at >= finished_at)",
            "next_due_after_finish",
        ),
        sa.ForeignKeyConstraint(
            ["intent_id"],
            [f"{INTENTS}.intent_id"],
            name=op.f(f"fk_{CHECKS}_intent_id_{INTENTS}"),
        ),
        sa.PrimaryKeyConstraint("intent_id", "seq", name=op.f(f"pk_{CHECKS}")),
    )
    op.create_index(
        IN_FLIGHT, CHECKS, ["intent_id"], unique=True, sqlite_where=sa.text("finished_at IS NULL")
    )
    _trigger(
        f"trg_{CHECKS}_append",
        "INSERT",
        CHECKS,
        _raise(
            f"{CHECKS}: checks are appended in order",
            f"NEW.seq <> (SELECT COALESCE(MAX(seq), 0) + 1 FROM {CHECKS}"
            " WHERE intent_id = NEW.intent_id)",
        )
        + _raise(
            f"{CHECKS}: a check opens in flight",
            "NEW.finished_at IS NOT NULL OR NEW.result IS NOT NULL"
            " OR NEW.candidate_count IS NOT NULL OR NEW.evidence_digest IS NOT NULL"
            " OR NEW.next_due_at IS NOT NULL",
        )
        + _raise(
            f"{CHECKS}: only an UNKNOWN intent is reconciled",
            f"(SELECT state FROM {INTENTS} WHERE intent_id = NEW.intent_id) IS NOT 'UNKNOWN'",
        ),
    )
    _trigger(
        f"trg_{CHECKS}_finish_once",
        "UPDATE",
        CHECKS,
        _raise(
            f"{CHECKS}: a check is finished once and never changed",
            "NOT (OLD.finished_at IS NULL AND NEW.finished_at IS NOT NULL"
            " AND NEW.intent_id IS OLD.intent_id AND NEW.seq IS OLD.seq"
            ' AND NEW."trigger" IS OLD."trigger" AND NEW.started_at IS OLD.started_at)',
        ),
    )
    _trigger(
        f"trg_{CHECKS}_no_delete",
        "DELETE",
        CHECKS,
        _raise(f"{CHECKS} is never deleted", "1"),
    )

    op.add_column(INTENTS, sa.Column(CHANNEL, sa.String(length=64), nullable=True))
    op.add_column(REGISTRATIONS, sa.Column(CHANNEL, sa.String(length=64), nullable=True))
    _trigger(
        f"trg_{INTENTS}_channel_open",
        "INSERT",
        INTENTS,
        _raise(
            f"{INTENTS}: an intent opens without a channel identity",
            f"NEW.{CHANNEL} IS NOT NULL",
        ),
    )
    _trigger(
        f"trg_{INTENTS}_channel_identity",
        "UPDATE",
        INTENTS,
        _raise(
            f"{INTENTS}: a channel identity exists only with an applied outcome",
            f"NEW.{CHANNEL} IS NOT NULL"
            f" AND (NEW.remote_outcome IS NOT 'APPLIED_PROVEN' OR NEW.{CHANNEL} = '')",
        )
        + _raise(
            f"{INTENTS}: an applied channel identity never changes",
            f"OLD.remote_outcome = 'APPLIED_PROVEN' AND NEW.{CHANNEL} IS NOT OLD.{CHANNEL}",
        ),
    )
    _trigger(
        f"trg_{REGISTRATIONS}_channel_copied",
        "INSERT",
        REGISTRATIONS,
        _raise(
            f"{REGISTRATIONS}: the channel identity is copied from the confirmed intent",
            f"NEW.{CHANNEL} IS NOT (SELECT {CHANNEL} FROM {INTENTS}"
            " WHERE intent_id = NEW.intent_id)",
        ),
    )
    _trigger(
        f"trg_{REGISTRATIONS}_channel_immutable",
        "UPDATE",
        REGISTRATIONS,
        _raise(
            f"{REGISTRATIONS}: the channel identity never changes",
            f"NEW.{CHANNEL} IS NOT OLD.{CHANNEL}",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    checks = bind.execute(sa.text(f"SELECT COUNT(*) FROM {CHECKS}")).scalar_one()
    channels = sum(
        bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE {CHANNEL} IS NOT NULL")
        ).scalar_one()
        for table in (INTENTS, REGISTRATIONS)
    )
    if checks or channels:
        raise RuntimeError(
            f"{checks} reconcile check(s) and {channels} channel identit(ies) exist: reconcile"
            " evidence and provider identities are never silently destroyed"
        )
    for name, _table in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER {name}")
    op.drop_column(REGISTRATIONS, CHANNEL)
    op.drop_column(INTENTS, CHANNEL)
    op.drop_index(IN_FLIGHT, table_name=CHECKS)
    op.drop_table(CHECKS)
