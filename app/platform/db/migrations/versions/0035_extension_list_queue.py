"""The extension list queue and its items (ADR-0019 §8.1; E3).

Revision ID: 0035_extension_list_queue
Revises: 0034_collect_transport_provenance
Create Date: 2026-10-02

The user's instruction of 2026-10-02 ordered E3 after the security gate. ADR-0019 §8.1 is its
contract: every queue read is server-issued work, written durably before it happens. These two
tables are where that is written. It creates two tables with their indexes; it changes no existing
row, table, trigger, index or constraint, and it backfills nothing.

- ``extension_queues`` — one operator-declared queue: the supplier, the operator's own bounds (the
  number of products and the interval between them), whether collected products are skipped, its
  state and when it last issued a read. It never holds the list page, its URL or page content.
- ``extension_queue_items`` — one product of a queue, in discovery order: its product URL, the key
  the same-product interval is measured on, its own state, and for an issued read the SHA-256 of
  its single-use ticket (never the ticket), when it was issued and when it expires. A captured item
  names its collection run, whose outcome stays the run's own (AC-31).

**Downgrade fails closed.** It refuses while any queue exists: an issued read is a counted read of
the supplier, and that record is never silently destroyed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_extension_list_queue"
down_revision: str | None = "0034_collect_transport_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUEUES = "extension_queues"
ITEMS = "extension_queue_items"
RUNS = "collection_runs"
QUEUE_STATES = ("OPEN", "FINISHED", "STOPPED", "CANCELLED")
ITEM_STATES = ("WAITING", "ISSUED", "CAPTURED", "SKIPPED", "EXPIRED", "CANCELLED")


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        QUEUES,
        sa.Column("queue_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("max_products", sa.Integer(), nullable=False),
        sa.Column("interval_s", sa.Float(), nullable=False),
        sa.Column("skip_collected", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_issued_at", sa.DateTime(), nullable=True),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        _check(QUEUES, f"state IN ({_listed(QUEUE_STATES)})", "state_valid"),
        _check(QUEUES, "max_products >= 1", "max_products_positive"),
        _check(QUEUES, "interval_s > 0", "interval_positive"),
        _check(QUEUES, "(state = 'OPEN') = (finished_at IS NULL)", "finished_when_closed"),
        _check(QUEUES, "(state = 'STOPPED') = (stop_reason IS NOT NULL)", "stop_has_reason"),
        sa.PrimaryKeyConstraint("queue_id", name=op.f(f"pk_{QUEUES}")),
    )
    op.create_index("ix_extension_queues_supplier", QUEUES, ["supplier_key", "state"])
    op.create_table(
        ITEMS,
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("queue_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("product_key", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("ticket_sha256", sa.String(length=64), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("collection_run_id", sa.String(length=36), nullable=True),
        _check(ITEMS, f"state IN ({_listed(ITEM_STATES)})", "state_valid"),
        _check(ITEMS, "source_url LIKE 'https://%'", "source_url_https"),
        _check(
            ITEMS,
            "ticket_sha256 IS NULL OR (length(ticket_sha256) = 64"
            " AND ticket_sha256 NOT GLOB '*[^0-9a-f]*')",
            "ticket_sha256_hex",
        ),
        _check(
            ITEMS,
            "(state IN ('WAITING', 'SKIPPED', 'CANCELLED')) = (issued_at IS NULL)",
            "issued_when_read",
        ),
        _check(
            ITEMS,
            "(issued_at IS NULL) = (ticket_sha256 IS NULL AND expires_at IS NULL)",
            "ticket_when_issued",
        ),
        _check(
            ITEMS, "(state = 'CAPTURED') = (collection_run_id IS NOT NULL)", "run_when_captured"
        ),
        sa.ForeignKeyConstraint(
            ["queue_id"], [f"{QUEUES}.queue_id"], name=op.f(f"fk_{ITEMS}_queue_id_{QUEUES}")
        ),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            [f"{RUNS}.collection_run_id"],
            name=op.f(f"fk_{ITEMS}_collection_run_id_{RUNS}"),
        ),
        sa.PrimaryKeyConstraint("item_id", name=op.f(f"pk_{ITEMS}")),
        sa.UniqueConstraint("queue_id", "position", name=op.f(f"uq_{ITEMS}_queue_id_position")),
        sa.UniqueConstraint(
            "queue_id", "product_key", name=op.f(f"uq_{ITEMS}_queue_id_product_key")
        ),
    )
    op.create_index("ix_extension_queue_items_ticket", ITEMS, ["ticket_sha256"], unique=True)
    op.create_index(
        "ix_extension_queue_items_pacing", ITEMS, ["supplier_key", "product_key", "issued_at"]
    )


def downgrade() -> None:
    held = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {QUEUES}")).scalar_one()
    if held:
        raise RuntimeError(
            f"{held} extension queue(s) exist: an issued queue read is a counted supplier read and"
            " is never silently destroyed"
        )
    op.drop_index("ix_extension_queue_items_pacing", table_name=ITEMS)
    op.drop_index("ix_extension_queue_items_ticket", table_name=ITEMS)
    op.drop_table(ITEMS)
    op.drop_index("ix_extension_queues_supplier", table_name=QUEUES)
    op.drop_table(QUEUES)
