"""M6.5-C the DISPATCH stage (ADR-0025 §5): the fourth mutation stage of ``live_grants``, the exact
unit of a DISPATCH grant, and the dispatch-attempt owner.

- ``live_grants`` is rebuilt from its own stored definition (as 0037 did) with ``DISPATCH`` added to
  the stage CHECK, a DISPATCH binding that names none of the other stages' columns, and a budget of
  exactly one. Every other column, constraint, row, index and trigger is kept.
- ``live_grant_dispatch_bindings``: one row per DISPATCH grant — the product order and the supplier
  order revision whose carrier and tracking number it may send. Append-only.
- ``operate_dispatch_attempts``: one row per dispatch attempt, opened ``STARTED`` and ended once,
  verified at most once; never deleted. No attempt is opened while another blocks the order.

Revision ID: 0057_m65_dispatch_stage
Revises: 0056_ai_provider_profile
Create Date: 2026-10-08
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_m65_dispatch_stage"
down_revision: str | None = "0056_ai_provider_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANTS = "live_grants"
SAVED = "_live_grants_0057"
BINDINGS = "live_grant_dispatch_bindings"
ATTEMPTS = "operate_dispatch_attempts"

_NONE_BOUND = (
    "registration_snapshot_id IS NULL AND intent_id IS NULL"
    " AND idempotency_key IS NULL AND create_attempt_no IS NULL"
    " AND preparation_revision_id IS NULL AND candidate_fingerprint IS NULL"
    " AND artifact_set_json IS NULL AND artifact_set_digest IS NULL AND asset_profile IS NULL"
)
_DELETE_BOUND = (
    "registration_snapshot_id IS NOT NULL AND intent_id IS NOT NULL"
    " AND idempotency_key IS NULL AND create_attempt_no IS NULL"
    " AND preparation_revision_id IS NULL AND candidate_fingerprint IS NULL"
    " AND artifact_set_json IS NULL AND artifact_set_digest IS NULL AND asset_profile IS NULL"
)
_STAGES_0037 = "stage IN ('ASSET', 'CREATE', 'DELETE')"
_STAGES_0054 = "stage IN ('ASSET', 'CREATE', 'DELETE', 'DISPATCH')"
_DELETE_CLAUSE = f" OR (stage = 'DELETE' AND {_DELETE_BOUND})"
_DISPATCH_CLAUSE = f" OR (stage = 'DISPATCH' AND {_NONE_BOUND})"
_DELETE_BUDGET = (
    f"CONSTRAINT ck_{GRANTS}_delete_budget_is_one CHECK (stage <> 'DELETE' OR budget_max = 1)"
)
_DISPATCH_BUDGET = (
    f"CONSTRAINT ck_{GRANTS}_dispatch_budget_is_one CHECK (stage <> 'DISPATCH' OR budget_max = 1)"
)

# An attempt that blocks another dispatch of its order (ADR-0025 §5; dispatch_models.BLOCKING).
_BLOCKING = (
    "state IN ('STARTED', 'APPLIED_PROVEN', 'UNKNOWN')"
    " OR (state = 'REJECTED' AND (verification IS NULL OR verification <> 'UNDISPATCHED'))"
    " OR verification IN ('DISPATCH_CONFIRMED', 'CONFLICT')"
)
_ATTEMPT_IDENTITY = (
    "attempt_id",
    "product_order_id",
    "attempt_no",
    "grant_id",
    "marketplace_key",
    "marketplace_account_id",
    "supplier_order_revision",
    "carrier_code",
    "tracking_number",
    "dispatch_date",
    "actor",
    "correlation_id",
    "started_at",
)
_ENDING = ("state", "fail_code", "error_code", "response_status", "ended_at")
_BINDING_COLUMNS = (
    "grant_id",
    "product_order_id",
    "supplier_order_revision",
    "carrier_code",
    "tracking_number",
)


def _changed(columns: Sequence[str]) -> str:
    return " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns)


def _replace_once(sql: str, old: str, new: str) -> str:
    if sql.count(old) != 1:
        raise RuntimeError(f"{GRANTS}: expected exactly one {old[:60]!r} in its stored definition")
    return sql.replace(old, new)


def _rebuild_grants(*, widen: bool) -> None:
    """Rebuild ``live_grants`` from its own stored definition with the stage CHECKs changed."""
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
        new_sql = _replace_once(table_sql, f"CHECK ({_STAGES_0037})", f"CHECK ({_STAGES_0054})")
        new_sql = _replace_once(
            new_sql, _DELETE_CLAUSE + ")", _DELETE_CLAUSE + _DISPATCH_CLAUSE + ")"
        )
        new_sql = _replace_once(
            new_sql, _DELETE_BUDGET, f"{_DELETE_BUDGET}, \n\t{_DISPATCH_BUDGET}"
        )
    else:
        new_sql = _replace_once(table_sql, f"CHECK ({_STAGES_0054})", f"CHECK ({_STAGES_0037})")
        new_sql = _replace_once(new_sql, _DISPATCH_CLAUSE + ")", ")")
        new_sql = _replace_once(
            new_sql, f"{_DELETE_BUDGET}, \n\t{_DISPATCH_BUDGET}", _DELETE_BUDGET
        )
    if not re.match(rf"^CREATE TABLE {GRANTS} \(", new_sql):
        raise RuntimeError(f"{GRANTS}: unexpected stored definition header")
    count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {GRANTS}")).scalar_one()
    bind.exec_driver_sql(f"CREATE TEMP TABLE {SAVED} AS SELECT * FROM {GRANTS}")
    bind.exec_driver_sql(f"DROP TABLE {GRANTS}")
    bind.exec_driver_sql(new_sql)
    bind.exec_driver_sql(f"INSERT INTO {GRANTS} SELECT * FROM {SAVED}")
    bind.exec_driver_sql(f"DROP TABLE {SAVED}")
    for statement in extras:
        bind.exec_driver_sql(statement)
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {GRANTS}")).scalar_one() != count:
        raise RuntimeError(f"rebuilding {GRANTS} lost rows")
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


def upgrade() -> None:
    _rebuild_grants(widen=True)

    op.create_table(
        BINDINGS,
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("product_order_id", sa.String(length=40), nullable=False),
        sa.Column("supplier_order_revision", sa.Integer(), nullable=False),
        sa.Column("carrier_code", sa.String(length=40), nullable=False),
        sa.Column("tracking_number", sa.String(length=50), nullable=False),
        sa.CheckConstraint("product_order_id <> ''", name="product_order_present"),
        sa.CheckConstraint("supplier_order_revision >= 1", name="revision_positive"),
        sa.CheckConstraint("carrier_code <> '' AND tracking_number <> ''", name="tracking_present"),
        sa.ForeignKeyConstraint(["grant_id"], [f"{GRANTS}.grant_id"]),
        sa.PrimaryKeyConstraint("grant_id"),
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{BINDINGS}_dispatch_grant_only
        BEFORE INSERT ON {BINDINGS}
        WHEN NOT EXISTS (
            SELECT 1 FROM {GRANTS} WHERE grant_id = NEW.grant_id AND stage = 'DISPATCH'
        )
        BEGIN SELECT RAISE(ABORT, 'a dispatch binding belongs to a DISPATCH grant'); END;
        """
    )
    for verb in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{BINDINGS}_no_{verb.lower()}
            BEFORE {verb} ON {BINDINGS}
            BEGIN SELECT RAISE(ABORT, 'a dispatch grant binding never changes'); END;
            """
        )

    op.create_table(
        ATTEMPTS,
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("product_order_id", sa.String(length=40), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("supplier_order_revision", sa.Integer(), nullable=False),
        sa.Column("carrier_code", sa.String(length=40), nullable=False),
        sa.Column("tracking_number", sa.String(length=50), nullable=False),
        sa.Column("dispatch_date", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("fail_code", sa.String(length=100), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("verification", sa.String(length=24), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('STARTED', 'APPLIED_PROVEN', 'REJECTED', 'NOT_APPLIED_PROVEN', 'UNKNOWN')",
            name="state_valid",
        ),
        sa.CheckConstraint("(state = 'STARTED') = (ended_at IS NULL)", name="ended_has_time"),
        sa.CheckConstraint(
            "verification IS NULL"
            " OR verification IN ('DISPATCH_CONFIRMED', 'UNDISPATCHED', 'CONFLICT')",
            name="verification_valid",
        ),
        sa.CheckConstraint(
            "(verification IS NULL) = (verified_at IS NULL)", name="verified_has_time"
        ),
        sa.CheckConstraint(
            "verification IS NULL OR state IN ('APPLIED_PROVEN', 'REJECTED', 'UNKNOWN')",
            name="verification_of_a_sent_attempt",
        ),
        sa.CheckConstraint(
            "verification IS NOT 'UNDISPATCHED' OR state = 'REJECTED'",
            name="undispatched_only_after_rejection",
        ),
        sa.CheckConstraint(
            "attempt_no >= 1 AND supplier_order_revision >= 1", name="numbers_positive"
        ),
        sa.CheckConstraint("correlation_id <> '' AND grant_id <> ''", name="identities_present"),
        sa.ForeignKeyConstraint(["product_order_id"], ["operate_orders.product_order_id"]),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index(
        "ux_operate_dispatch_attempts_number",
        ATTEMPTS,
        ["product_order_id", "attempt_no"],
        unique=True,
    )
    op.create_index(
        "ux_operate_dispatch_attempts_open",
        ATTEMPTS,
        ["product_order_id"],
        unique=True,
        sqlite_where=sa.text("state = 'STARTED'"),
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{ATTEMPTS}_opens_unblocked
        BEFORE INSERT ON {ATTEMPTS}
        WHEN NEW.state IS NOT 'STARTED' OR NEW.verification IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM {ATTEMPTS}
                WHERE product_order_id = NEW.product_order_id AND ({_BLOCKING})
            )
        BEGIN SELECT RAISE(ABORT, 'a dispatch opens STARTED, and only when nothing blocks it'); END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{ATTEMPTS}_forward_only
        BEFORE UPDATE ON {ATTEMPTS}
        WHEN {_changed(_ATTEMPT_IDENTITY)}
            OR (OLD.state = 'STARTED' AND NEW.state = 'STARTED')
            OR (OLD.state <> 'STARTED' AND ({_changed(_ENDING)}))
            OR (OLD.verification IS NOT NULL
                AND (NEW.verification IS NOT OLD.verification
                     OR NEW.verified_at IS NOT OLD.verified_at))
        BEGIN SELECT RAISE(ABORT, 'a dispatch attempt ends once and is verified once'); END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_{ATTEMPTS}_no_delete
        BEFORE DELETE ON {ATTEMPTS}
        BEGIN SELECT RAISE(ABORT, 'dispatch attempts are kept as evidence'); END;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    held = (
        bind.execute(sa.text(f"SELECT COUNT(*) FROM {ATTEMPTS}")).scalar_one()
        + bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {GRANTS} WHERE stage = 'DISPATCH'")
        ).scalar_one()
    )
    if held:
        raise RuntimeError("DISPATCH grants or attempts exist; they are evidence and stay")
    for name in ("opens_unblocked", "forward_only", "no_delete"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{ATTEMPTS}_{name}")
    op.drop_index("ux_operate_dispatch_attempts_open", table_name=ATTEMPTS)
    op.drop_index("ux_operate_dispatch_attempts_number", table_name=ATTEMPTS)
    op.drop_table(ATTEMPTS)
    for name in ("dispatch_grant_only", "no_update", "no_delete"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{BINDINGS}_{name}")
    op.drop_table(BINDINGS)
    _rebuild_grants(widen=False)
