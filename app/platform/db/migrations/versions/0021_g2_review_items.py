"""Gate 2 G2-A: the durable ReviewItem owner and its append-only history.

Revision ID: 0021_g2_review_items
Revises: 0020_g1_registration_category_metadata
Create Date: 2026-09-24

Issue #89 Gate 2, slice G2-A, under ADR-0016 (G2-0, merged as `d22f297a`). It adds exactly two
tables and touches no existing table, row, trigger or index.

**What it holds.** A ReviewItem is a durable index of human work over a condition an owner
already derives (ADR-0016 §2). It stores references only: kind, producer, canonical owner scope,
subject, owner reason code, source identity and the two server-computed keys. It stores no owner
value, no readiness or verdict and no provider or page content (§9).

**What the database enforces.**
- `review_items`: one row per review key; at most one `OPEN` row per condition key (§3). Identity
  references never change after insert; the generation only ever grows, by one; a row is never
  deleted.
- `review_item_events`: append-only history, numbered per item with no gap. Each event has one
  valid shape (§4), and it names the item's own current state and generation, so the history and
  the row can never disagree. A human disposition appears only on a human resolution, at most once
  per item generation (§5, §8).

**Downgrade fails closed.** It refuses while either table holds a row: review history is never
silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_g2_review_items"
down_revision: str | None = "0020_g1_registration_category_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ITEMS = "review_items"
EVENTS = "review_item_events"
CREATED = (ITEMS, EVENTS)

KINDS = (
    "COLLECT_EVIDENCE",
    "STOCK",
    "SOURCE_CHANGE",
    "COMPLIANCE",
    "REGISTRATION_ERROR",
    "FULFILLMENT",
)
STATES = ("OPEN", "RESOLVED", "SUPERSEDED")
EVENTS_KINDS = ("OPENED", "REOPENED", "SUPERSEDED", "RESOLVED", "RESOLUTION_RECORDED")
BASES = (
    "OWNER_CONDITION_DERIVED",
    "OWNER_CONDITION_CLEARED",
    "OWNER_SOURCE_MOVED",
    "HUMAN_RESOLUTION",
    "CONDITION_PERSISTS",
)
DISPOSITIONS = ("OWNER_ACTION_TAKEN", "FOLLOW_UP_REQUIRED", "NO_ACTION_TAKEN")
NOTE_MAX_CHARS = 500

EVENT_SHAPE = (
    "(event = 'OPENED' AND from_state IS NULL AND to_state = 'OPEN'"
    " AND basis = 'OWNER_CONDITION_DERIVED' AND successor_item_id IS NULL)"
    " OR (event = 'REOPENED' AND from_state IN ('RESOLVED', 'SUPERSEDED') AND to_state = 'OPEN'"
    " AND basis = 'OWNER_CONDITION_DERIVED' AND successor_item_id IS NULL)"
    " OR (event = 'SUPERSEDED' AND from_state = 'OPEN' AND to_state = 'SUPERSEDED'"
    " AND basis = 'OWNER_SOURCE_MOVED' AND successor_item_id IS NOT NULL)"
    " OR (event = 'RESOLVED' AND from_state = 'OPEN' AND to_state = 'RESOLVED'"
    " AND basis IN ('OWNER_CONDITION_CLEARED', 'HUMAN_RESOLUTION') AND successor_item_id IS NULL)"
    " OR (event = 'RESOLUTION_RECORDED' AND successor_item_id IS NULL AND ("
    "(from_state = 'OPEN' AND to_state = 'OPEN' AND basis = 'CONDITION_PERSISTS')"
    " OR (from_state = 'SUPERSEDED' AND to_state = 'SUPERSEDED' AND basis = 'OWNER_SOURCE_MOVED')))"
)
HUMAN_FIELDS = (
    "(disposition IS NOT NULL) = (event = 'RESOLUTION_RECORDED' OR basis = 'HUMAN_RESOLUTION')"
    " AND (disposition IS NOT NULL OR (note IS NULL AND evidence_reference IS NULL))"
)
# The identity references of an item. None of them may change once written (ADR-0016 §9).
IDENTITY = (
    "review_item_id",
    "kind",
    "producer",
    "scope_json",
    "subject",
    "reason_code",
    "source_identity",
    "condition_key",
    "review_key",
    "opened_at",
)


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def upgrade() -> None:
    _create_items()
    _create_events()
    _install_triggers()


def _create_items() -> None:
    op.create_table(
        ITEMS,
        sa.Column("review_item_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("producer", sa.String(length=64), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("source_identity", sa.String(length=128), nullable=False),
        sa.Column("condition_key", sa.String(length=64), nullable=False),
        sa.Column("review_key", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("changed_at", sa.DateTime(), nullable=False),
        _check(ITEMS, _in("kind", KINDS), "kind_valid"),
        _check(ITEMS, _in("state", STATES), "state_valid"),
        _check(ITEMS, "producer <> ''", "producer_present"),
        _check(
            ITEMS,
            "json_valid(scope_json) AND json_type(scope_json) = 'object' AND scope_json <> '{}'",
            "scope_is_object",
        ),
        _check(ITEMS, "subject <> ''", "subject_present"),
        _check(ITEMS, "reason_code <> ''", "reason_code_present"),
        _check(ITEMS, "source_identity <> ''", "source_identity_present"),
        _check(ITEMS, _hex64("condition_key"), "condition_key_hex"),
        _check(ITEMS, _hex64("review_key"), "review_key_hex"),
        _check(ITEMS, "generation >= 1", "generation_positive"),
        sa.PrimaryKeyConstraint("review_item_id", name=op.f(f"pk_{ITEMS}")),
        _unique(ITEMS, "review_key"),
    )
    op.create_index(
        "ux_review_items_one_open_per_condition",
        ITEMS,
        ["condition_key"],
        unique=True,
        sqlite_where=sa.text("state = 'OPEN'"),
    )
    op.create_index("ix_review_items_producer_state", ITEMS, ["producer", "state"])


def _create_events() -> None:
    op.create_table(
        EVENTS,
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("review_item_id", sa.String(length=36), nullable=False),
        sa.Column("event_no", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=24), nullable=False),
        sa.Column("from_state", sa.String(length=16), nullable=True),
        sa.Column("to_state", sa.String(length=16), nullable=False),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("successor_item_id", sa.String(length=36), nullable=True),
        sa.Column("disposition", sa.String(length=32), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("evidence_reference", sa.String(length=128), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        _check(EVENTS, "event_no >= 1", "event_no_positive"),
        _check(EVENTS, "generation >= 1", "generation_positive"),
        _check(EVENTS, _in("event", EVENTS_KINDS), "event_valid"),
        _check(EVENTS, f"from_state IS NULL OR {_in('from_state', STATES)}", "from_state_valid"),
        _check(EVENTS, _in("to_state", STATES), "to_state_valid"),
        _check(EVENTS, _in("basis", BASES), "basis_valid"),
        _check(
            EVENTS,
            f"disposition IS NULL OR {_in('disposition', DISPOSITIONS)}",
            "disposition_valid",
        ),
        _check(EVENTS, EVENT_SHAPE, "event_shape"),
        _check(EVENTS, HUMAN_FIELDS, "human_fields"),
        _check(
            EVENTS, f"note IS NULL OR (length(note) BETWEEN 1 AND {NOTE_MAX_CHARS})", "note_bounded"
        ),
        _check(EVENTS, "actor <> ''", "actor_present"),
        _check(EVENTS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["review_item_id"],
            [f"{ITEMS}.review_item_id"],
            name=op.f(f"fk_{EVENTS}_review_item_id_{ITEMS}"),
        ),
        sa.ForeignKeyConstraint(
            ["successor_item_id"],
            [f"{ITEMS}.review_item_id"],
            name=op.f(f"fk_{EVENTS}_successor_item_id_{ITEMS}"),
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f(f"pk_{EVENTS}")),
        _unique(EVENTS, "review_item_id", "event_no"),
    )
    op.create_index(
        "ux_review_item_events_one_resolution_per_generation",
        EVENTS,
        ["review_item_id", "generation"],
        unique=True,
        sqlite_where=sa.text("disposition IS NOT NULL"),
    )


def _install_triggers() -> None:
    changed = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in IDENTITY)
    _trigger(
        ITEMS,
        "identity_immutable",
        "UPDATE",
        _raise(f"{ITEMS}: the identity references of a review item never change", changed),
    )
    _trigger(
        ITEMS,
        "generation_forward",
        "UPDATE",
        _raise(
            f"{ITEMS}: the generation only grows, by one",
            "NEW.generation < OLD.generation OR NEW.generation > OLD.generation + 1",
        ),
    )
    _trigger(ITEMS, "no_delete", "DELETE", _raise(f"a {ITEMS} row is never deleted", "1"))
    _trigger(
        EVENTS,
        "event_follows",
        "INSERT",
        _raise(
            f"{EVENTS}: an event follows the one before it",
            "NEW.event_no <> 1 + (SELECT COALESCE(MAX(event_no), 0)"
            f" FROM {EVENTS} e WHERE e.review_item_id = NEW.review_item_id)",
        ),
    )
    # The history names the item's own current state and generation: it can never disagree with
    # the row it describes.
    _trigger(
        EVENTS,
        "matches_item",
        "INSERT",
        _raise(
            f"{EVENTS}: an event names the current state and generation of its item",
            f"NOT EXISTS (SELECT 1 FROM {ITEMS} i WHERE i.review_item_id = NEW.review_item_id"
            " AND i.state = NEW.to_state AND i.generation = NEW.generation)",
        ),
    )
    _trigger(EVENTS, "no_update", "UPDATE", _raise(f"a {EVENTS} row is never updated", "1"))
    _trigger(EVENTS, "no_delete", "DELETE", _raise(f"a {EVENTS} row is never deleted", "1"))


def downgrade() -> None:
    bind = op.get_bind()
    held = {
        table: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in CREATED
    }
    if any(held.values()):
        raise RuntimeError(
            "cannot drop the ReviewItem owner:"
            f" {_counted(held)} are held; review history is never silently destroyed"
        )
    for table in reversed(CREATED):
        op.drop_table(table)


def _counted(held: dict[str, int]) -> str:
    return ", ".join(f"{count} row(s) in {table}" for table, count in held.items() if count)
