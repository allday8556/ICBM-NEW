"""Issue #219: an image selection may be a rule's, and may place an additional image.

Revision ID: 0040_image_auto_selection
Revises: 0039_supplier_common_images
Create Date: 2026-10-04

Two CHECKs and one trigger widen; nothing else changes.

* ``image_selection_revisions.decision_origin`` admits ``RULE`` beside ``OPERATOR``: the image
  auto-selection records its selection as its own, never as an operator's (owner decision
  2026-10-03, ADR-0013 §9 amendment).
* ``image_selection_outputs.role`` admits ``ADDITIONAL`` beside ``REPRESENTATIVE`` and ``DETAIL``:
  an output is the representative image, an additional (gallery) image or a detail-body image.
* ``trg_current_image_selection_moves_chain`` lets a RULE selection become current, under every
  other rule it already enforced.

SQLite cannot alter a CHECK in place, so each table is rebuilt — like 0037 did — from its own
stored definition with only that CHECK changed. Every row, index and trigger is read back from
``sqlite_master`` before the old table is dropped and recreated under its own name. Tables refer to
both, so the rebuild runs with ``PRAGMA defer_foreign_keys`` and refuses to finish if
``PRAGMA foreign_key_check`` reports anything for them or a table that refers to them.

**Downgrade fails closed.** It refuses while any RULE selection or ADDITIONAL output exists: a
recorded selection is never silently destroyed.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_image_auto_selection"
down_revision: str | None = "0039_supplier_common_images"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVISIONS = "image_selection_revisions"
OUTPUTS = "image_selection_outputs"
MOVES = "current_image_selection_moves"
CHAIN = "trg_current_image_selection_moves_chain"

_ORIGIN_0014 = "CHECK (decision_origin IN ('OPERATOR'))"
_ORIGIN_0040 = "CHECK (decision_origin IN ('OPERATOR', 'RULE'))"
_ROLE_0014 = "CHECK (role IN ('REPRESENTATIVE', 'DETAIL'))"
_ROLE_0040 = "CHECK (role IN ('REPRESENTATIVE', 'ADDITIONAL', 'DETAIL'))"
_CHAIN_0014 = (
    "SELECT RAISE(ABORT, 'current_image_selection_moves: the selection is an OPERATOR decision"
    " for this Item') WHERE NOT EXISTS (SELECT 1 FROM image_selection_revisions s WHERE"
    " s.selection_revision_id = NEW.selection_revision_id AND s.item_id = NEW.item_id AND"
    " s.decision_origin = 'OPERATOR');"
)
_CHAIN_0040 = (
    "SELECT RAISE(ABORT, 'current_image_selection_moves: the selection is an OPERATOR or RULE"
    " decision for this Item') WHERE NOT EXISTS (SELECT 1 FROM image_selection_revisions s WHERE"
    " s.selection_revision_id = NEW.selection_revision_id AND s.item_id = NEW.item_id AND"
    " s.decision_origin IN ('OPERATOR', 'RULE'));"
)


def _replace_once(where: str, sql: str, old: str, new: str) -> str:
    if sql.count(old) != 1:
        raise RuntimeError(f"{where}: expected exactly one {old[:60]!r} in its stored definition")
    return sql.replace(old, new)


def _rebuild(table: str, old: str, new: str) -> None:
    """Rebuild one table from its own stored definition with one CHECK changed. Everything else —
    columns, every other constraint, every row, index and trigger — is kept."""
    bind = op.get_bind()
    table_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": table},
    ).scalar_one()
    extras = [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE tbl_name = :name"
                " AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name"
            ),
            {"name": table},
        )
    ]
    new_sql = _replace_once(table, table_sql, old, new)
    if not re.match(rf"^CREATE TABLE {table} \(", new_sql):
        raise RuntimeError(f"{table}: unexpected stored definition header")
    saved = f"{table}_0040_saved"
    count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
    # Set the rows aside, drop the table and create it again under its own name, so its stored
    # definition stays exactly the original except for the changed CHECK (no rename, no quoting).
    bind.exec_driver_sql(f"CREATE TEMP TABLE {saved} AS SELECT * FROM {table}")
    bind.exec_driver_sql(f"DROP TABLE {table}")
    bind.exec_driver_sql(new_sql)
    bind.exec_driver_sql(f"INSERT INTO {table} SELECT * FROM {saved}")
    bind.exec_driver_sql(f"DROP TABLE {saved}")
    for statement in extras:
        bind.exec_driver_sql(statement)
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one() != count:
        raise RuntimeError(f"rebuilding {table} lost rows")


def _chain(old: str, new: str) -> None:
    bind = op.get_bind()
    sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :name"),
        {"name": CHAIN},
    ).scalar_one()
    rebuilt = _replace_once(CHAIN, sql, old, new)
    bind.exec_driver_sql(f"DROP TRIGGER {CHAIN}")
    bind.exec_driver_sql(rebuilt)


def _check_foreign_keys() -> None:
    """The rebuilt tables and every table that refers to them; other tables are not this
    change's."""
    bind = op.get_bind()
    tables = [
        row[0]
        for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    related = {REVISIONS, OUTPUTS} | {
        table
        for table in tables
        if any(
            ref[2] in (REVISIONS, OUTPUTS)
            for ref in bind.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")').fetchall()
        )
    }
    for table in sorted(related):
        problems = bind.exec_driver_sql(f'PRAGMA foreign_key_check("{table}")').fetchall()
        if problems:
            raise RuntimeError(f"rebuilding the image selection left {table} inconsistent")


def upgrade() -> None:
    op.get_bind().exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    _rebuild(REVISIONS, _ORIGIN_0014, _ORIGIN_0040)
    _rebuild(OUTPUTS, _ROLE_0014, _ROLE_0040)
    _chain(_CHAIN_0014, _CHAIN_0040)
    _check_foreign_keys()


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(
        sa.text(
            f"SELECT (SELECT COUNT(*) FROM {REVISIONS} WHERE decision_origin = 'RULE')"
            f" + (SELECT COUNT(*) FROM {OUTPUTS} WHERE role = 'ADDITIONAL')"
        )
    ).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot downgrade 0040: {held} rule selection(s) or additional output(s) exist; a"
            " recorded selection is never silently destroyed"
        )
    bind.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    _chain(_CHAIN_0040, _CHAIN_0014)
    _rebuild(OUTPUTS, _ROLE_0040, _ROLE_0014)
    _rebuild(REVISIONS, _ORIGIN_0040, _ORIGIN_0014)
    _check_foreign_keys()
