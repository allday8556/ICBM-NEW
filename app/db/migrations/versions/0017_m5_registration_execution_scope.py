"""M5 PR-E: the REGISTER execution-scope send brake, with its durable resume boundary.

Revision ID: 0017_m5_registration_execution_scope
Revises: 0016_m5_registration_foundation
Create Date: 2026-09-20

Issue #89 PR-E, under ADR-0014 §26 and the architect decision in comment `5749504280`. It adds
exactly one table and touches no existing table, row, trigger or index.

**Why it exists.** PR-E derives the registration failure budget from durable attempt history, so
*entering* a pause needs no state. *Leaving* one does: a release is a fact about an operator
action or an authentication proof, and no existing table records that for a registration
execution scope. Approximating it with `marketplace_capabilities.updated_at` releases a scope on
unrelated capability changes, and with `auth_verified_at` it releases a `POLICY` or
`FAILURE_BUDGET` pause on ordinary re-authentication. Both were rejected (reviews `5259615496`,
`5260076445`).

**What it owns, and what it does not.** One row per `marketplace_key × marketplace_account_id ×
endpoint_group`: whether this owner may keep sending there, why it stopped, and the durable
boundary (`resumed_at` / `resume_generation`) the budget counts attempts after. It is not
capability truth. CONNECT keeps account binding, auth, permission/write scope, workflow overlays
and contract freshness; REGISTER keeps Intents, Attempts, retry, reconcile, read-back, the failure
budget and this brake. No provider wire identity, no secret and no payload value belongs here.

**Invariants in the schema.** ACTIVE holds no open pause; PAUSED names its cause, its time and the
policy version that judged it; a reason is never paired with a class that did not cause it; a
resume boundary is complete (time, actor and reason together) and exists exactly when
`resume_generation > 0`; a pause recorded after a resume is later than it. The
triggers add the cross-row half: the scope key and the creation time never change, the generation
only ever moves forward by one, a generation move is an accepted release (ACTIVE with a boundary),
`resumed_at` never goes backwards, and the row is never deleted. A resume therefore cannot rewrite
or erase one recorded `RegistrationAttempt`: it only moves the boundary they are counted after.

**Downgrade fails closed.** It refuses while the table holds a row, exactly as 0016 does: an
engaged brake is never silently dropped.
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_m5_registration_execution_scope"
down_revision: str | None = "0016_m5_registration_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPES = "registration_execution_scopes"
ACCOUNTS = "marketplace_accounts"
CONNECTIONS = "marketplace_connections"

_STATES = ("ACTIVE", "PAUSED")
_PAUSE_REASONS = ("AUTH", "POLICY", "FAILURE_BUDGET")
# The frozen ErrorClass vocabulary of ADR-0008, as migration 0016 froze it for the attempts.
_ERROR_CLASSES = (
    "TRANSIENT",
    "RATE_LIMITED",
    "AUTH",
    "VALIDATION",
    "POLICY_BLOCKED",
    "NOT_FOUND",
    "CONFLICT",
    "DUPLICATE",
    "REVIEW_REQUIRED",
    "FATAL",
    "UNKNOWN",
)


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


def _check(expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{SCOPES}_{name}"))


def _trigger(name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {SCOPES} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _unchanged(columns: Iterable[str]) -> str:
    return " AND ".join(f"NEW.{column} IS OLD.{column}" for column in columns)


def upgrade() -> None:
    op.create_table(
        SCOPES,
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("endpoint_group", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("pause_reason", sa.String(length=20), nullable=True),
        sa.Column("pause_error_class", sa.String(length=20), nullable=True),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column("pause_policy_version", sa.String(length=64), nullable=True),
        sa.Column("resume_generation", sa.Integer(), nullable=False),
        sa.Column("resumed_at", sa.DateTime(), nullable=True),
        sa.Column("resumed_by", sa.String(length=64), nullable=True),
        sa.Column("resume_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        _check("marketplace_key <> ''", "marketplace_key_present"),
        _check("endpoint_group <> ''", "endpoint_group_present"),
        _check(_in("state", _STATES), "state_valid"),
        _check(
            f"pause_reason IS NULL OR {_in('pause_reason', _PAUSE_REASONS)}", "pause_reason_valid"
        ),
        _check(
            f"pause_error_class IS NULL OR {_in('pause_error_class', _ERROR_CLASSES)}",
            "pause_error_class_valid",
        ),
        _check(
            "state <> 'ACTIVE' OR (pause_reason IS NULL AND paused_at IS NULL"
            " AND pause_policy_version IS NULL AND pause_error_class IS NULL)",
            "active_holds_no_pause",
        ),
        _check(
            "state <> 'PAUSED' OR (pause_reason IS NOT NULL AND paused_at IS NOT NULL"
            " AND pause_policy_version IS NOT NULL AND pause_policy_version <> '')",
            "paused_states_its_cause",
        ),
        _check(
            "pause_reason IS NULL"
            " OR (pause_reason = 'AUTH' AND pause_error_class = 'AUTH')"
            " OR (pause_reason = 'POLICY' AND pause_error_class = 'POLICY_BLOCKED')"
            " OR (pause_reason = 'FAILURE_BUDGET'"
            " AND (pause_error_class IS NULL"
            " OR pause_error_class NOT IN ('AUTH', 'POLICY_BLOCKED')))",
            "pause_class_is_its_cause",
        ),
        _check("resume_generation >= 0", "resume_generation_non_negative"),
        _check(
            "(resume_generation = 0) = (resumed_at IS NULL)"
            " AND (resumed_at IS NULL) = (resumed_by IS NULL)"
            " AND (resumed_at IS NULL) = (resume_reason IS NULL)",
            "resume_boundary_complete",
        ),
        _check(
            "resumed_by IS NULL OR (resumed_by <> '' AND resume_reason <> '')",
            "resume_actor_present",
        ),
        _check(
            "paused_at IS NULL OR resumed_at IS NULL OR paused_at >= resumed_at",
            "pause_follows_resume",
        ),
        sa.ForeignKeyConstraint(
            ["marketplace_key", "marketplace_account_id"],
            [f"{ACCOUNTS}.marketplace_key", f"{ACCOUNTS}.marketplace_account_id"],
            name=op.f(f"fk_{SCOPES}_marketplace_key_{ACCOUNTS}"),
        ),
        sa.PrimaryKeyConstraint(
            "marketplace_key",
            "marketplace_account_id",
            "endpoint_group",
            name=op.f(f"pk_{SCOPES}"),
        ),
    )
    _install_triggers()


def _install_triggers() -> None:
    _trigger(f"trg_{SCOPES}_no_delete", "DELETE", _raise(f"{SCOPES} is never deleted", "1"))
    # ACCOUNT_IDENTITY §4: no registration state opens for an unbound or mismatched account.
    _trigger(
        f"trg_{SCOPES}_account_bound",
        "INSERT",
        _raise(
            f"{SCOPES}: the marketplace account is not bound to its identity",
            f"NOT EXISTS (SELECT 1 FROM {ACCOUNTS} a JOIN {CONNECTIONS} c"
            " ON c.marketplace_key = a.marketplace_key"
            " WHERE a.marketplace_account_id = NEW.marketplace_account_id"
            " AND a.marketplace_key = NEW.marketplace_key"
            " AND c.provider_account_uid = a.provider_account_uid)",
        ),
    )
    # A new scope starts ACTIVE at generation 0 or PAUSED by its first cause: never with a resume
    # boundary it never had.
    _trigger(
        f"trg_{SCOPES}_opens_unresumed",
        "INSERT",
        _raise(
            f"{SCOPES}: a scope opens with no resume boundary",
            "NEW.resume_generation <> 0",
        ),
    )
    _trigger(
        f"trg_{SCOPES}_transition",
        "UPDATE",
        _raise(
            f"{SCOPES}: the scope key and its resume boundary only move forward",
            "NOT ("
            + _unchanged(("marketplace_key", "marketplace_account_id", "endpoint_group"))
            + " AND NEW.created_at IS OLD.created_at"
            + " AND NEW.updated_at >= OLD.updated_at"
            # The generation is the resume counter: it never falls and never skips.
            + " AND NEW.resume_generation >= OLD.resume_generation"
            + " AND NEW.resume_generation <= OLD.resume_generation + 1"
            # A move of the generation *is* an accepted release: ACTIVE, with its own boundary.
            + " AND (NEW.resume_generation = OLD.resume_generation"
            + " OR (NEW.state = 'ACTIVE' AND NEW.resumed_at IS NOT NULL"
            + " AND (OLD.resumed_at IS NULL OR NEW.resumed_at >= OLD.resumed_at)))"
            # Without a move, the recorded boundary stays exactly as it was.
            + " AND (NEW.resume_generation <> OLD.resume_generation"
            + " OR (NEW.resumed_at IS OLD.resumed_at AND NEW.resumed_by IS OLD.resumed_by"
            + " AND NEW.resume_reason IS OLD.resume_reason))"
            # Leaving PAUSED is a resume, never a quiet clearing of the brake.
            + " AND (OLD.state <> 'PAUSED' OR NEW.state = 'PAUSED'"
            + " OR NEW.resume_generation = OLD.resume_generation + 1)"
            + ")",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {SCOPES}")).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot drop {SCOPES}: {held} row(s) are held; an engaged registration send brake"
            " is never silently destroyed"
        )
    op.drop_table(SCOPES)
