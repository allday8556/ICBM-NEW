"""ADR-0027 AIS-1: the AI provider profile and its call ledger.

Revision ID: 0056_ai_provider_profile
Revises: 0055_ai_enrichment_results
Create Date: 2026-10-09

ADR-0027 §2 and §3 (owner decisions Issue #219 `6067968983`, `6068160917`). It adds four tables and
touches no existing table, row, trigger or index.

- `ai_provider_profiles`, `ai_provider_profile_revisions`, `ai_provider_profile_current`: one profile
  per key, its append-only revisions, and a current pointer that moves only forward to its own
  newest revision. A revision's content is one JSON object (provider type, loopback endpoint,
  requested model, credential source, approved executable and routing identity, billing mode, the
  owner's data-transfer approval and the daily call cap). No secret is ever in it.
- `ai_provider_calls`: one row per provider call the profile made, counted per UTC day for the
  daily cap. The row is written `SENT` before the call, in the same serialized write unit that
  counts the day's calls, so concurrent calls can never pass the cap together; after the call it is
  settled once to `OK` or `FAILED`, and nothing else of it ever changes. A row names the profile
  revision, the task, the outcome and the correlation; never a prompt, a value or a key.

Every table starts empty (M0 acceptance). Downgrade fails closed while any row exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_ai_provider_profile"
down_revision: str | None = "0055_ai_enrichment_results"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROFILES = "ai_provider_profiles"
REVISIONS = "ai_provider_profile_revisions"
CURRENT = "ai_provider_profile_current"
CALLS = "ai_provider_calls"
CREATED = (PROFILES, REVISIONS, CURRENT, CALLS)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    op.create_table(
        PROFILES,
        sa.Column("profile_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(PROFILES, "profile_key <> ''", "key_present"),
        sa.PrimaryKeyConstraint("profile_key", name=op.f(f"pk_{PROFILES}")),
    )
    op.create_table(
        REVISIONS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("profile_key", sa.String(length=64), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("authored_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "revision_no >= 1", "revision_no_positive"),
        _check(
            REVISIONS,
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            "content_is_object",
        ),
        _check(
            REVISIONS,
            "length(content_fingerprint) = 64 AND content_fingerprint NOT GLOB '*[^0-9a-f]*'",
            "content_fingerprint_hex",
        ),
        _check(
            REVISIONS,
            "action IN ('CONFIGURE', 'APPROVE_EXECUTABLE', 'APPROVE_ROUTING', 'DATA_TRANSFER')",
            "action_known",
        ),
        _check(REVISIONS, "authored_by <> ''", "authored_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["profile_key"],
            [f"{PROFILES}.profile_key"],
            name=op.f(f"fk_{REVISIONS}_profile_key_{PROFILES}"),
        ),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{REVISIONS}")),
        sa.UniqueConstraint(
            "profile_key", "revision_no", name=op.f(f"uq_{REVISIONS}_profile_key_revision_no")
        ),
    )
    op.create_table(
        CURRENT,
        sa.Column("profile_key", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("moved_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(CURRENT, "moved_by <> ''", "moved_by_present"),
        _check(CURRENT, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["profile_key"],
            [f"{PROFILES}.profile_key"],
            name=op.f(f"fk_{CURRENT}_profile_key_{PROFILES}"),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            [f"{REVISIONS}.revision_id"],
            name=op.f(f"fk_{CURRENT}_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("profile_key", name=op.f(f"pk_{CURRENT}")),
    )
    op.create_table(
        CALLS,
        sa.Column("call_id", sa.String(length=36), nullable=False),
        sa.Column("profile_key", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("call_day", sa.String(length=10), nullable=False),
        sa.Column("task_key", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=8), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("called_at", sa.DateTime(), nullable=False),
        _check(CALLS, "outcome IN ('SENT', 'OK', 'FAILED')", "outcome_known"),
        _check(CALLS, "call_day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", "call_day_iso"),
        _check(CALLS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["profile_key"],
            [f"{PROFILES}.profile_key"],
            name=op.f(f"fk_{CALLS}_profile_key_{PROFILES}"),
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            [f"{REVISIONS}.revision_id"],
            name=op.f(f"fk_{CALLS}_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("call_id", name=op.f(f"pk_{CALLS}")),
    )
    op.create_index(op.f(f"ix_{CALLS}_profile_key_call_day"), CALLS, ["profile_key", "call_day"])

    _trigger(PROFILES, "no_update", "UPDATE", _raise(f"{PROFILES} is never updated", "1"))
    _trigger(PROFILES, "no_delete", "DELETE", _raise(f"{PROFILES} is never deleted", "1"))
    _trigger(
        REVISIONS,
        "revision_follows",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision follows the one before it",
            f"NEW.revision_no <> 1 + (SELECT COALESCE(MAX(revision_no), 0) FROM {REVISIONS} r"
            " WHERE r.profile_key = NEW.profile_key)",
        ),
    )
    _trigger(REVISIONS, "no_update", "UPDATE", _raise(f"a {REVISIONS} row is never updated", "1"))
    _trigger(REVISIONS, "no_delete", "DELETE", _raise(f"a {REVISIONS} row is never deleted", "1"))
    newest = (
        f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r WHERE r.revision_id = NEW.revision_id"
        " AND r.profile_key = NEW.profile_key AND r.revision_no = (SELECT MAX(m.revision_no)"
        f" FROM {REVISIONS} m WHERE m.profile_key = NEW.profile_key))"
    )
    for event, name in (("INSERT", "names_newest_on_insert"), ("UPDATE", "names_newest_on_update")):
        _trigger(
            CURRENT, name, event, _raise(f"{CURRENT}: the current revision is the newest", newest)
        )
    _trigger(
        CURRENT,
        "same_profile",
        "UPDATE",
        _raise(f"{CURRENT}: a pointer never changes profile", "NEW.profile_key <> OLD.profile_key"),
    )
    _trigger(CURRENT, "no_delete", "DELETE", _raise(f"a {CURRENT} row is never deleted", "1"))
    _trigger(
        CALLS,
        "settles_once",
        "UPDATE",
        _raise(
            f"a {CALLS} row is only settled once, from SENT to OK or FAILED",
            "OLD.outcome <> 'SENT' OR NEW.outcome NOT IN ('OK', 'FAILED')"
            " OR NEW.call_id IS NOT OLD.call_id OR NEW.profile_key IS NOT OLD.profile_key"
            " OR NEW.revision_id IS NOT OLD.revision_id OR NEW.call_day IS NOT OLD.call_day"
            " OR NEW.task_key IS NOT OLD.task_key OR NEW.correlation_id IS NOT OLD.correlation_id"
            " OR NEW.called_at IS NOT OLD.called_at",
        ),
    )
    _trigger(CALLS, "no_delete", "DELETE", _raise(f"a {CALLS} row is never deleted", "1"))


def downgrade() -> None:
    bind = op.get_bind()
    held = {t: bind.execute(sa.text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in CREATED}
    if any(held.values()):
        raise RuntimeError(
            "cannot drop the AI provider profile: "
            + ", ".join(f"{n} row(s) in {t}" for t, n in held.items() if n)
            + " are held; an approval or call history is never silently destroyed"
        )
    op.drop_index(op.f(f"ix_{CALLS}_profile_key_call_day"), table_name=CALLS)
    for table in reversed(CREATED):
        op.drop_table(table)
