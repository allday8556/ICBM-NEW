"""ADR-0018 §3.5: the DELETE stage — a DELETE grant and the durable deletion-attempt owner.

Revision ID: 0037_g3_delete_stage
Revises: 0036_g3_residual_risk_acceptance
Create Date: 2026-10-03

**``live_grants`` admits a third stage.** A DELETE grant binds the exact confirmed registration
through its Intent and Snapshot, names no CREATE idempotency key or attempt number and no ASSET
binding, and has a budget of exactly 1. SQLite cannot alter a CHECK in place, so the table is
rebuilt — exactly as 0005 did for its CHECK — from its own stored definition, with only the stage
list and the stage binding widened and the one new budget check added. Every row, index and
trigger is kept: they are read back from ``sqlite_master`` before the old table is dropped and
recreated unchanged. ``asset_upload_attempts`` keeps a foreign key to the table, so the rebuild runs
with ``PRAGMA defer_foreign_keys`` (honoured inside the transaction), and the migration refuses to
finish if ``PRAGMA foreign_key_check`` reports anything for it or a table that refers to it.

**``registration_deletions``** records every attempt to delete one confirmed registration,
append-only: opened ``STARTED`` (its grant spent in the same unit, before any byte is sent),
ended exactly once, and afterwards only its read-back verification may be recorded — forward
only. A partial unique index keeps at most one open deletion per registration: in flight, applied,
or unknown without a read-back that shows the listing still there. So an unknown deletion is
never resent.

**Downgrade fails closed.** It refuses while any DELETE grant or deletion attempt exists: grant
and deletion history is never silently destroyed.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_g3_delete_stage"
down_revision: str | None = "0036_g3_residual_risk_acceptance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANTS = "live_grants"
DELETIONS = "registration_deletions"
REGISTRATIONS = "marketplace_registrations"
REBUILT = f"{GRANTS}_0037"

# Frozen with this revision.
_ASSET_BOUND = (
    "preparation_revision_id IS NOT NULL AND candidate_fingerprint IS NOT NULL"
    " AND artifact_set_json IS NOT NULL AND artifact_set_digest IS NOT NULL"
    " AND asset_profile IS NOT NULL AND asset_profile <> ''"
    " AND registration_snapshot_id IS NULL AND intent_id IS NULL"
    " AND idempotency_key IS NULL AND create_attempt_no IS NULL"
)
_CREATE_BOUND = (
    "registration_snapshot_id IS NOT NULL AND intent_id IS NOT NULL"
    " AND idempotency_key IS NOT NULL AND idempotency_key <> ''"
    " AND create_attempt_no IS NOT NULL AND create_attempt_no >= 1"
    " AND preparation_revision_id IS NULL AND candidate_fingerprint IS NULL"
    " AND artifact_set_json IS NULL AND artifact_set_digest IS NULL AND asset_profile IS NULL"
)
_DELETE_BOUND = (
    "registration_snapshot_id IS NOT NULL AND intent_id IS NOT NULL"
    " AND idempotency_key IS NULL AND create_attempt_no IS NULL"
    " AND preparation_revision_id IS NULL AND candidate_fingerprint IS NULL"
    " AND artifact_set_json IS NULL AND artifact_set_digest IS NULL AND asset_profile IS NULL"
)

_STAGES_0026 = "stage IN ('ASSET', 'CREATE')"
_STAGES_0037 = "stage IN ('ASSET', 'CREATE', 'DELETE')"
_BINDING_0026 = f"(stage = 'ASSET' AND {_ASSET_BOUND}) OR (stage = 'CREATE' AND {_CREATE_BOUND})"
_BINDING_0037 = _BINDING_0026 + f" OR (stage = 'DELETE' AND {_DELETE_BOUND})"
_DELETE_BUDGET = (
    f"CONSTRAINT ck_{GRANTS}_delete_budget_is_one CHECK (stage <> 'DELETE' OR budget_max = 1)"
)
_CREATE_BUDGET = (
    f"CONSTRAINT ck_{GRANTS}_create_budget_is_one CHECK (stage <> 'CREATE' OR budget_max = 1)"
)

# A deletion that blocks another (ADR-0018 §3.5): at most one per registration.
_OPEN = (
    "state IN ('STARTED', 'APPLIED_PROVEN')"
    " OR (state = 'UNKNOWN' AND (verification IS NULL OR verification <> 'STILL_PRESENT'))"
)
_DELETION_FROZEN = (
    "deletion_id",
    "registration_id",
    "intent_id",
    "marketplace_key",
    "marketplace_account_id",
    "marketplace_product_id",
    "grant_id",
    "attempt_no",
    "actor",
    "correlation_id",
    "started_at",
)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _changed(columns: Sequence[str]) -> str:
    return " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns)


def _replace_once(sql: str, old: str, new: str) -> str:
    if sql.count(old) != 1:
        raise RuntimeError(f"{GRANTS}: expected exactly one {old[:60]!r} in its stored definition")
    return sql.replace(old, new)


def _rebuild_grants(*, widen: bool) -> None:
    """Rebuild ``live_grants`` from its own stored definition with the stage CHECKs changed.

    Everything else — columns, every other constraint, every row, index and trigger — is kept.
    """
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    table_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": GRANTS},
    ).scalar_one()
    extras = [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE tbl_name = :name"
                " AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name"
            ),
            {"name": GRANTS},
        )
    ]
    if widen:
        new_sql = _replace_once(table_sql, f"CHECK ({_STAGES_0026})", f"CHECK ({_STAGES_0037})")
        new_sql = _replace_once(new_sql, f"CHECK ({_BINDING_0026})", f"CHECK ({_BINDING_0037})")
        new_sql = _replace_once(new_sql, _CREATE_BUDGET, f"{_CREATE_BUDGET}, \n\t{_DELETE_BUDGET}")
    else:
        new_sql = _replace_once(table_sql, f"CHECK ({_STAGES_0037})", f"CHECK ({_STAGES_0026})")
        new_sql = _replace_once(new_sql, f"CHECK ({_BINDING_0037})", f"CHECK ({_BINDING_0026})")
        new_sql = _replace_once(new_sql, f"{_CREATE_BUDGET}, \n\t{_DELETE_BUDGET}", _CREATE_BUDGET)
    # SQLite stores the name quoted once a table has been renamed into place.
    header = re.compile(rf'^CREATE TABLE "?{GRANTS}"? \(')
    if not header.match(new_sql):
        raise RuntimeError(f"{GRANTS}: unexpected stored definition header")
    new_sql = header.sub(f"CREATE TABLE {REBUILT} (", new_sql, count=1)
    count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {GRANTS}")).scalar_one()
    bind.exec_driver_sql(new_sql)
    bind.exec_driver_sql(f"INSERT INTO {REBUILT} SELECT * FROM {GRANTS}")
    bind.exec_driver_sql(f"DROP TABLE {GRANTS}")
    bind.exec_driver_sql(f"ALTER TABLE {REBUILT} RENAME TO {GRANTS}")
    for statement in extras:
        bind.exec_driver_sql(statement)
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {GRANTS}")).scalar_one() != count:
        raise RuntimeError(f"rebuilding {GRANTS} lost rows")
    # The rebuilt table and every table that refers to it; other tables are not this change's.
    tables = [
        row[0]
        for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    related = [GRANTS] + [
        table
        for table in tables
        if any(
            ref[2] == GRANTS
            for ref in bind.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")').fetchall()
        )
    ]
    problems = [
        problem
        for table in related
        for problem in bind.exec_driver_sql(f'PRAGMA foreign_key_check("{table}")').fetchall()
    ]
    if problems:
        raise RuntimeError(f"foreign key check failed after rebuilding {GRANTS}: {problems}")


def _create_deletions() -> None:
    op.create_table(
        DELETIONS,
        sa.Column("deletion_id", sa.String(length=36), nullable=False),
        sa.Column("registration_id", sa.String(length=36), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("marketplace_product_id", sa.String(length=64), nullable=False),
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("verification", sa.String(length=24), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        _check(DELETIONS, "attempt_no > 0", "attempt_no_positive"),
        _check(
            DELETIONS,
            "state IN ('STARTED', 'APPLIED_PROVEN', 'NOT_APPLIED_PROVEN', 'UNKNOWN')",
            "state_valid",
        ),
        _check(
            DELETIONS,
            "verification IS NULL OR verification IN ('DELETE_CONFIRMED', 'STILL_PRESENT')",
            "verification_valid",
        ),
        _check(DELETIONS, "(state = 'STARTED') = (finished_at IS NULL)", "finished_unless_started"),
        _check(
            DELETIONS, "finished_at IS NULL OR finished_at >= started_at", "finished_after_start"
        ),
        _check(DELETIONS, "(verification IS NULL) = (verified_at IS NULL)", "verified_together"),
        _check(
            DELETIONS,
            "verification IS NULL OR state IN ('APPLIED_PROVEN', 'UNKNOWN')",
            "verified_only_when_possibly_applied",
        ),
        _check(DELETIONS, "marketplace_product_id <> ''", "product_present"),
        _check(DELETIONS, "grant_id <> ''", "grant_present"),
        _check(DELETIONS, "actor <> ''", "actor_present"),
        _check(DELETIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["registration_id"],
            [f"{REGISTRATIONS}.registration_id"],
            name=op.f(f"fk_{DELETIONS}_registration_id_{REGISTRATIONS}"),
        ),
        sa.PrimaryKeyConstraint("deletion_id", name=op.f(f"pk_{DELETIONS}")),
        sa.UniqueConstraint(
            "registration_id",
            "attempt_no",
            name=op.f(f"uq_{DELETIONS}_registration_id_attempt_no"),
        ),
    )
    op.create_index(
        "ux_registration_deletions_open",
        DELETIONS,
        ["registration_id"],
        unique=True,
        sqlite_where=sa.text(_OPEN),
    )
    _trigger(DELETIONS, "no_delete", "DELETE", _raise(f"{DELETIONS} is append-only", "1"))
    _trigger(
        DELETIONS,
        "append",
        "INSERT",
        _raise(
            f"{DELETIONS}: an attempt opens STARTED",
            "NEW.state <> 'STARTED' OR NEW.finished_at IS NOT NULL OR NEW.verification IS NOT NULL"
            " OR NEW.response_status IS NOT NULL OR NEW.error_code IS NOT NULL",
        )
        + _raise(
            f"{DELETIONS}: attempts are numbered in order",
            f"NEW.attempt_no <> (SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM {DELETIONS}"
            " WHERE registration_id = NEW.registration_id)",
        )
        + _raise(
            f"{DELETIONS}: only an ACTIVE registration is deleted",
            f"(SELECT lifecycle_state FROM {REGISTRATIONS}"
            " WHERE registration_id = NEW.registration_id) IS NOT 'ACTIVE'",
        )
        + _raise(
            f"{DELETIONS}: the identity is that of the registration",
            f"NOT EXISTS (SELECT 1 FROM {REGISTRATIONS} WHERE registration_id = NEW.registration_id"
            " AND intent_id = NEW.intent_id AND marketplace_key = NEW.marketplace_key"
            " AND marketplace_account_id = NEW.marketplace_account_id"
            " AND marketplace_product_id = NEW.marketplace_product_id)",
        ),
    )
    _trigger(
        DELETIONS,
        "forward_only",
        "UPDATE",
        _raise(
            f"{DELETIONS}: the identity of an attempt never changes",
            _changed(_DELETION_FROZEN),
        )
        + _raise(
            f"{DELETIONS}: an attempt ends exactly once",
            "OLD.state <> 'STARTED' AND (NEW.state IS NOT OLD.state"
            " OR NEW.finished_at IS NOT OLD.finished_at"
            " OR NEW.response_status IS NOT OLD.response_status"
            " OR NEW.error_code IS NOT OLD.error_code)",
        )
        + _raise(
            f"{DELETIONS}: a verification moves forward only",
            "NEW.verification IS NOT OLD.verification AND NOT ("
            "(OLD.verification IS NULL AND NEW.verification IS NOT NULL)"
            " OR (OLD.verification = 'STILL_PRESENT' AND OLD.state = 'APPLIED_PROVEN'"
            " AND NEW.verification = 'DELETE_CONFIRMED'))",
        ),
    )


def upgrade() -> None:
    _rebuild_grants(widen=True)
    _create_deletions()


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {DELETIONS}")).scalar_one()
    grants = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {GRANTS} WHERE stage = 'DELETE'")
    ).scalar_one()
    if held or grants:
        raise RuntimeError(
            f"cannot downgrade 0037: {held} deletion attempt(s) and {grants} DELETE grant(s)"
            " exist; grant and deletion history is never silently destroyed"
        )
    op.drop_index("ux_registration_deletions_open", table_name=DELETIONS)
    op.drop_table(DELETIONS)
    _rebuild_grants(widen=False)
