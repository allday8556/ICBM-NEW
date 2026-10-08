"""ADR-0026 AIF-1: the PromptTemplate and PlatformPolicy stores.

Revision ID: 0054_ai_prompt_registry
Revises: 0053_m65_delivery_readback
Create Date: 2026-10-08

ADR-0026 §3 and its AIF-1 amendment (owner decision Issue #219 `6057252039`). It adds six tables in
two separate families and touches no existing table, row, trigger or index.

**Two stores, never one** (Issue #30: no shared storage or version lifecycle).
- `ai_prompt_templates`, `ai_prompt_template_revisions`, `ai_prompt_template_current`: the GLOBAL,
  ROLE and TASK layers.
- `ai_platform_policies`, `ai_platform_policy_revisions`, `ai_platform_policy_current`: the
  platform policies.

Each family is the target-policy pattern: an immutable identity, append-only revisions whose number
opens at one and moves by exactly one, and one current pointer that only moves forward to its own
newest revision.

**Content.** A revision's content is one JSON object of text fields: a GLOBAL, ROLE or policy
revision holds `prompt`; a TASK revision holds `mode`, `prompt`, `variables` and `output`. The
content fingerprint is the SHA-256 of its canonical JSON.

**Seeds.** Revision 1 of every entry is the v29 prototype's text (`origin = SEED`, `seed_version =
v29`), written by the application when it starts on a database that lacks it
(`app/capabilities/ai/seed_v29.json`), never by this migration: every canonical table starts
empty (M0 acceptance). Every later revision is an operator's save or reset.

**Downgrade fails closed** while any operator revision exists: an operator's prompt history is never
silently dropped. Seeds alone are dropped; the application writes them again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_ai_prompt_registry"
down_revision: str | None = "0053_m65_delivery_readback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATES = "ai_prompt_templates"
TEMPLATE_REVISIONS = "ai_prompt_template_revisions"
TEMPLATE_CURRENT = "ai_prompt_template_current"
POLICIES = "ai_platform_policies"
POLICY_REVISIONS = "ai_platform_policy_revisions"
POLICY_CURRENT = "ai_platform_policy_current"

# Every table this migration creates, newest dependency last: the order a downgrade drops them.
CREATED = (
    TEMPLATES,
    TEMPLATE_REVISIONS,
    TEMPLATE_CURRENT,
    POLICIES,
    POLICY_REVISIONS,
    POLICY_CURRENT,
)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _text_field(field: str) -> str:
    return f"json_type(NEW.content_json, '$.{field}') = 'text'"


def upgrade() -> None:
    _create_family(
        TEMPLATES, TEMPLATE_REVISIONS, TEMPLATE_CURRENT, key="template_key", layered=True
    )
    _create_family(POLICIES, POLICY_REVISIONS, POLICY_CURRENT, key="policy_key", layered=False)


def _create_family(identity: str, revisions: str, current: str, *, key: str, layered: bool) -> None:
    columns = [sa.Column(key, sa.String(length=64), nullable=False)]
    checks = [_check(identity, f"{key} <> ''", "key_present")]
    if layered:
        columns.append(sa.Column("layer", sa.String(length=8), nullable=False))
        checks.append(_check(identity, "layer IN ('GLOBAL', 'ROLE', 'TASK')", "layer_known"))
    op.create_table(
        identity,
        *columns,
        sa.Column("created_at", sa.DateTime(), nullable=False),
        *checks,
        sa.PrimaryKeyConstraint(key, name=op.f(f"pk_{identity}")),
    )
    op.create_table(
        revisions,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column(key, sa.String(length=64), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("seed_version", sa.String(length=16), nullable=True),
        sa.Column("authored_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        _check(revisions, "revision_no >= 1", "revision_no_positive"),
        _check(
            revisions,
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            "content_is_object",
        ),
        _check(revisions, _hex64("content_fingerprint"), "content_fingerprint_hex"),
        _check(revisions, "origin IN ('SEED', 'OPERATOR', 'RESET')", "origin_known"),
        # A seed is revision 1 and names its seed version; nothing else is a seed.
        _check(
            revisions,
            "(origin = 'SEED') = (revision_no = 1)"
            " AND (origin = 'SEED') = (seed_version IS NOT NULL)",
            "seed_is_first",
        ),
        _check(revisions, "authored_by <> ''", "authored_by_present"),
        _check(revisions, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            [key], [f"{identity}.{key}"], name=op.f(f"fk_{revisions}_{key}_{identity}")
        ),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{revisions}")),
        _unique(revisions, key, "revision_no"),
    )
    op.create_table(
        current,
        sa.Column(key, sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("moved_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("moved_at", sa.DateTime(), nullable=False),
        _check(current, "moved_by <> ''", "moved_by_present"),
        _check(current, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            [key], [f"{identity}.{key}"], name=op.f(f"fk_{current}_{key}_{identity}")
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            [f"{revisions}.revision_id"],
            name=op.f(f"fk_{current}_revision_id_{revisions}"),
        ),
        sa.PrimaryKeyConstraint(key, name=op.f(f"pk_{current}")),
    )
    _install_triggers(identity, revisions, current, key=key, layered=layered)


def _install_triggers(
    identity: str, revisions: str, current: str, *, key: str, layered: bool
) -> None:
    _trigger(identity, "no_update", "UPDATE", _raise(f"{identity} is never updated", "1"))
    _trigger(identity, "no_delete", "DELETE", _raise(f"{identity} is never deleted", "1"))
    _trigger(
        revisions,
        "revision_follows",
        "INSERT",
        _raise(
            f"{revisions}: a revision follows the one before it",
            f"NEW.revision_no <> 1 + (SELECT COALESCE(MAX(revision_no), 0)"
            f" FROM {revisions} r WHERE r.{key} = NEW.{key})",
        ),
    )
    # The content holds exactly the text fields of its layer.
    prompt_only = (
        f"{_text_field('prompt')} AND (SELECT COUNT(*) FROM json_each(NEW.content_json)) = 1"
    )
    task = (
        f"{_text_field('mode')} AND {_text_field('prompt')} AND {_text_field('variables')}"
        f" AND {_text_field('output')} AND (SELECT COUNT(*) FROM json_each(NEW.content_json)) = 4"
    )
    if layered:
        shape = (
            f"CASE (SELECT layer FROM {identity} t WHERE t.{key} = NEW.{key})"
            f" WHEN 'TASK' THEN ({task}) ELSE ({prompt_only}) END"
        )
    else:
        shape = prompt_only
    _trigger(
        revisions,
        "content_shape",
        "INSERT",
        _raise(
            f"{revisions}: the content holds exactly the text fields of its layer", f"NOT ({shape})"
        ),
    )
    _trigger(revisions, "no_update", "UPDATE", _raise(f"a {revisions} row is never updated", "1"))
    _trigger(revisions, "no_delete", "DELETE", _raise(f"a {revisions} row is never deleted", "1"))
    newest = (
        f"NOT EXISTS (SELECT 1 FROM {revisions} r"
        f" WHERE r.revision_id = NEW.revision_id AND r.{key} = NEW.{key}"
        f" AND r.revision_no = (SELECT MAX(m.revision_no) FROM {revisions} m"
        f" WHERE m.{key} = NEW.{key}))"
    )
    _trigger(
        current,
        "names_newest_on_insert",
        "INSERT",
        _raise(f"{current}: the current revision is the newest of its own entry", newest),
    )
    _trigger(
        current,
        "same_entry",
        "UPDATE",
        _raise(f"{current}: a pointer never changes entry", f"NEW.{key} <> OLD.{key}"),
    )
    _trigger(
        current,
        "names_newest_on_update",
        "UPDATE",
        _raise(f"{current}: the current revision is the newest of its own entry", newest),
    )
    _trigger(current, "no_delete", "DELETE", _raise(f"a {current} row is never deleted", "1"))


def downgrade() -> None:
    bind = op.get_bind()
    beyond_seed = {
        table: bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE origin <> 'SEED'")
        ).scalar_one()
        for table in (TEMPLATE_REVISIONS, POLICY_REVISIONS)
    }
    if any(beyond_seed.values()):
        raise RuntimeError(
            "cannot drop the AI prompt registry: "
            + ", ".join(f"{n} operator revision(s) in {t}" for t, n in beyond_seed.items() if n)
            + " are held; an operator's prompt history is never silently destroyed"
        )
    for table in reversed(CREATED):
        op.drop_table(table)
