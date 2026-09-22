"""Gate 1 G1-A: the durable registration target policy, revisioned and append-only.

Revision ID: 0019_g1_registration_target_policy
Revises: 0018_m5_registration_preparation
Create Date: 2026-09-23

Issue #89 Gate 1, slice G1-A, under ADR-0015 §2 and the implementation authorization in comment
`5785935712`. It adds exactly three tables and touches no existing table, row, trigger or index.

**Why it exists.** The REGISTER preflight reads the Settings/platform policy of one marketplace ×
canonical account on every evaluation (ADR-0014 §21), but production had nothing durable behind
that read: an empty in-memory source, so every evaluation stopped at
`REGISTER_TARGET_POLICY_MISSING`. This owner is that durable source.

**What it owns, and what it does not.** Only the policy inputs ADR-0015 §2 names — the taxonomy
revision, the M4 pricing context inputs, the account-scoped template identities, the sanitizer
profile, the asset policy, the duplicate-proof policy and the authoring revision references — as
one canonical, sanitized content document per revision. It stores **no** Product or price, no
PricingSnapshot, no readiness, status or reason code, no Snapshot/Intent/Attempt/Registration truth,
no capability, auth or provider truth and no category metadata.

**Revisions are append-only, and the current one is explicit.**
- `registration_target_policies` is the identity of one policy per `marketplace_key ×
  marketplace_account_id`, scoped by the canonical account's composite foreign key.
- `registration_target_policy_revisions` holds the revisions. The server creates each one, with its
  identity and the fingerprint of its content. A revision is never updated or deleted, and its
  number opens at one and moves by exactly one.
- `registration_target_policy_current` names the one current revision of each policy. It only ever
  moves forward, to the newest revision of the same policy, and is never deleted.

**Downgrade fails closed.** It refuses while any of the three tables holds a row: an operator's
policy history is never silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_g1_registration_target_policy"
down_revision: str | None = "0018_m5_registration_preparation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICIES = "registration_target_policies"
REVISIONS = "registration_target_policy_revisions"
CURRENT = "registration_target_policy_current"
ACCOUNTS = "marketplace_accounts"

# Every table this migration creates, newest dependency last: the order a downgrade drops them.
CREATED = (POLICIES, REVISIONS, CURRENT)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    """Named exactly as the metadata's naming convention names it (`uq_<table>_<columns>`)."""
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    _create_policies()
    _create_revisions()
    _create_current()
    _install_triggers()


def _create_policies() -> None:
    op.create_table(
        POLICIES,
        sa.Column("policy_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(POLICIES, "marketplace_key <> ''", "marketplace_key_present"),
        _check(POLICIES, "created_by <> ''", "created_by_present"),
        _check(POLICIES, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["marketplace_key", "marketplace_account_id"],
            [f"{ACCOUNTS}.marketplace_key", f"{ACCOUNTS}.marketplace_account_id"],
            name=op.f(f"fk_{POLICIES}_marketplace_key_{ACCOUNTS}"),
        ),
        sa.PrimaryKeyConstraint("policy_id", name=op.f(f"pk_{POLICIES}")),
        _unique(POLICIES, "marketplace_key", "marketplace_account_id"),
    )


def _create_revisions() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("policy_revision_id", sa.String(length=36), nullable=False),
        sa.Column("policy_id", sa.String(length=36), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("authored_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "revision_no >= 1", "revision_no_positive"),
        _check(
            REVISIONS,
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            "content_is_object",
        ),
        _check(REVISIONS, _hex64("content_fingerprint"), "content_fingerprint_hex"),
        _check(REVISIONS, "authored_by <> ''", "authored_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            [f"{POLICIES}.policy_id"],
            name=op.f(f"fk_{REVISIONS}_policy_id_{POLICIES}"),
        ),
        sa.PrimaryKeyConstraint("policy_revision_id", name=op.f(f"pk_{REVISIONS}")),
        _unique(REVISIONS, "policy_id", "revision_no"),
    )


def _create_current() -> None:
    op.create_table(
        CURRENT,
        sa.Column("policy_id", sa.String(length=36), nullable=False),
        sa.Column("policy_revision_id", sa.String(length=36), nullable=False),
        sa.Column("moved_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(CURRENT, "moved_by <> ''", "moved_by_present"),
        _check(CURRENT, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["policy_id"],
            [f"{POLICIES}.policy_id"],
            name=op.f(f"fk_{CURRENT}_policy_id_{POLICIES}"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_revision_id"],
            [f"{REVISIONS}.policy_revision_id"],
            name=op.f(f"fk_{CURRENT}_policy_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("policy_id", name=op.f(f"pk_{CURRENT}")),
    )


# The named revision belongs to the pointer's own policy and is that policy's newest revision.
_NEWEST_OF_ITS_POLICY = (
    f"NOT EXISTS (SELECT 1 FROM {REVISIONS} r"
    " WHERE r.policy_revision_id = NEW.policy_revision_id AND r.policy_id = NEW.policy_id"
    f" AND r.revision_no = (SELECT MAX(m.revision_no) FROM {REVISIONS} m"
    " WHERE m.policy_id = NEW.policy_id))"
)


def _install_triggers() -> None:
    _trigger(POLICIES, "no_update", "UPDATE", _raise(f"{POLICIES} is never updated", "1"))
    _trigger(POLICIES, "no_delete", "DELETE", _raise(f"{POLICIES} is never deleted", "1"))
    # The revision counter opens at one and then moves by exactly one: no gap and no rewrite.
    _trigger(
        REVISIONS,
        "revision_follows",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision follows the one before it",
            "NEW.revision_no <> 1 + (SELECT COALESCE(MAX(revision_no), 0)"
            f" FROM {REVISIONS} r WHERE r.policy_id = NEW.policy_id)",
        ),
    )
    # A revision's content names exactly the scope of the policy it belongs to.
    _trigger(
        REVISIONS,
        "content_scope",
        "INSERT",
        _raise(
            f"{REVISIONS}: the content names another marketplace account",
            f"NOT EXISTS (SELECT 1 FROM {POLICIES} p WHERE p.policy_id = NEW.policy_id"
            " AND json_extract(NEW.content_json, '$.marketplace_key') = p.marketplace_key"
            " AND json_extract(NEW.content_json, '$.marketplace_account_id')"
            " = p.marketplace_account_id)",
        ),
    )
    _trigger(REVISIONS, "no_update", "UPDATE", _raise(f"a {REVISIONS} row is never updated", "1"))
    _trigger(REVISIONS, "no_delete", "DELETE", _raise(f"a {REVISIONS} row is never deleted", "1"))
    # Exactly one current revision per policy, always the newest one of that same policy.
    _trigger(
        CURRENT,
        "names_newest_on_insert",
        "INSERT",
        _raise(
            f"{CURRENT}: the current revision is the newest of its own policy",
            _NEWEST_OF_ITS_POLICY,
        ),
    )
    _trigger(
        CURRENT,
        "same_policy",
        "UPDATE",
        _raise(f"{CURRENT}: a pointer never changes policy", "NEW.policy_id <> OLD.policy_id"),
    )
    # Revision numbers only grow, so "the newest of its own policy" also means the pointer only
    # ever moves forward: an older or foreign revision is refused by the same rule.
    _trigger(
        CURRENT,
        "names_newest_on_update",
        "UPDATE",
        _raise(
            f"{CURRENT}: the current revision is the newest of its own policy",
            _NEWEST_OF_ITS_POLICY,
        ),
    )
    _trigger(CURRENT, "no_delete", "DELETE", _raise(f"a {CURRENT} row is never deleted", "1"))


def downgrade() -> None:
    bind = op.get_bind()
    held = {
        table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in CREATED
    }
    if any(held.values()):
        raise RuntimeError(
            "cannot drop the registration target-policy owner:"
            f" {_counted(held)} are held; an operator's policy history is never silently destroyed"
        )
    for table in reversed(CREATED):
        op.drop_table(table)


def _counted(held: dict[str, int]) -> str:
    return ", ".join(f"{count} row(s) in {table}" for table, count in held.items() if count)
