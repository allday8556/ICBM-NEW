"""ADR-0033 G5: the Detail Guidance placement — the Snapshot pin and the guidance upload kind.

Revision ID: 0059_detail_guidance_placement
Revises: 0058_detail_guidance
Create Date: 2026-10-10

ADR-0033 §6, §8. Two changes, nothing else:

- **``registration_snapshots.guidance_assets_json``** (ADR-0033 §6, DG-06). A Snapshot whose
  payload is ``registration-payload/v3`` (a composition v3 unit) pins one entry per distinct Detail
  Guidance image — ``{asset_kind: GUIDANCE_ARTIFACT, sha256, derivation_id: null, asset_profile,
  provider_asset_ref}`` — as a JSON array, empty when no notice resolved. Every other Snapshot holds
  ``NULL``: the CHECK ties the column to the payload's builder version, so no v1 or v2 Snapshot can
  carry one and no v3 Snapshot can lack it. The column is added in place (``ADD COLUMN``): no row
  is rewritten, and every existing Snapshot — none of which is v3 — keeps exactly what it holds.
- **``asset_upload_attempts.asset_kind``** admits ``GUIDANCE_ARTIFACT`` (ADR-0033 §8, DG-07): an
  upload attempt of a rendered guidance image. SQLite cannot alter a CHECK in place, so the table is
  rebuilt — as 0037 and 0057 rebuilt ``live_grants`` — from its own stored definition with only that
  CHECK widened. Every row, index and trigger is kept: they are read back from ``sqlite_master``
  before the old table is dropped and recreated unchanged, inside a savepoint, and the migration
  refuses to finish if any row is lost or ``PRAGMA foreign_key_check`` reports anything.

**Every canonical table starts empty** (M0 acceptance): nothing is seeded.

**Downgrade fails closed** while any Snapshot pins guidance assets (``guidance_assets_json`` is not
``NULL`` — an empty pin included, because its v3 payload is evidence an older head cannot read) or
any upload attempt names a ``GUIDANCE_ARTIFACT``: frozen evidence is never silently destroyed.
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_detail_guidance_placement"
down_revision: str | None = "0058_detail_guidance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SNAPSHOTS = "registration_snapshots"
COLUMN = "guidance_assets_json"
ATTEMPTS = "asset_upload_attempts"
SAVEPOINT = "rebuild_asset_upload_attempts_0059"
SAVED = "_asset_upload_attempts_0059"

# Frozen with this revision (``app.stages.register.models.GUIDANCE_ASSETS_PINNED``).
GUIDANCE_ASSETS_PINNED = (
    "(json_extract(payload_json, '$.builder_version') = 'registration-payload/v3')"
    f" = ({COLUMN} IS NOT NULL)"
    f" AND ({COLUMN} IS NULL OR (json_valid({COLUMN})"
    f" AND json_type({COLUMN}) = 'array'))"
)
_KINDS_0026 = "asset_kind IN ('SOURCE_ASSET', 'DERIVED_ARTIFACT')"
_KINDS_0059 = "asset_kind IN ('SOURCE_ASSET', 'DERIVED_ARTIFACT', 'GUIDANCE_ARTIFACT')"


def _replace_once(sql: str, old: str, new: str) -> str:
    if sql.count(old) != 1:
        raise RuntimeError(
            f"{ATTEMPTS}: expected exactly one {old[:60]!r} in its stored definition"
        )
    return sql.replace(old, new)


def _rebuild_attempts(*, widen: bool) -> None:
    """Rebuild ``asset_upload_attempts`` from its own stored definition with only the asset-kind
    CHECK changed, inside a savepoint (see 0057: without one ``defer_foreign_keys`` has no effect).
    """
    bind = op.get_bind()
    bind.exec_driver_sql(f"SAVEPOINT {SAVEPOINT}")
    try:
        _rebuild_in_savepoint(bind, widen=widen)
    except Exception:
        bind.exec_driver_sql(f"ROLLBACK TO SAVEPOINT {SAVEPOINT}")
        bind.exec_driver_sql(f"RELEASE SAVEPOINT {SAVEPOINT}")
        raise
    bind.exec_driver_sql(f"RELEASE SAVEPOINT {SAVEPOINT}")


def _rebuild_in_savepoint(bind: sa.Connection, *, widen: bool) -> None:
    bind.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    table_sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": ATTEMPTS},
    ).scalar_one()
    extras = [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE tbl_name = :name"
                " AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name"
            ),
            {"name": ATTEMPTS},
        )
    ]
    old, new = (_KINDS_0026, _KINDS_0059) if widen else (_KINDS_0059, _KINDS_0026)
    new_sql = _replace_once(table_sql, f"CHECK ({old})", f"CHECK ({new})")
    if not re.match(rf"^CREATE TABLE {ATTEMPTS} \(", new_sql):
        raise RuntimeError(f"{ATTEMPTS}: unexpected stored definition header")
    count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {ATTEMPTS}")).scalar_one()
    bind.exec_driver_sql(f"CREATE TEMP TABLE {SAVED} AS SELECT * FROM {ATTEMPTS}")
    bind.exec_driver_sql(f"DROP TABLE {ATTEMPTS}")
    bind.exec_driver_sql(new_sql)
    bind.exec_driver_sql(f"INSERT INTO {ATTEMPTS} SELECT * FROM {SAVED}")
    bind.exec_driver_sql(f"DROP TABLE {SAVED}")
    for statement in extras:
        bind.exec_driver_sql(statement)
    if bind.execute(sa.text(f"SELECT COUNT(*) FROM {ATTEMPTS}")).scalar_one() != count:
        raise RuntimeError(f"rebuilding {ATTEMPTS} lost rows")
    problems = bind.exec_driver_sql(f'PRAGMA foreign_key_check("{ATTEMPTS}")').fetchall()
    if problems:
        raise RuntimeError(f"foreign key check failed after rebuilding {ATTEMPTS}: {problems}")


def upgrade() -> None:
    # A nullable column with a column CHECK: SQLite adds it in place and rewrites no row, so the
    # Snapshot's immutability triggers never fire.
    op.execute(
        f"ALTER TABLE {SNAPSHOTS} ADD COLUMN {COLUMN} TEXT"
        f" CONSTRAINT ck_{SNAPSHOTS}_guidance_assets_pinned CHECK ({GUIDANCE_ASSETS_PINNED})"
    )
    _rebuild_attempts(widen=True)


def downgrade() -> None:
    bind = op.get_bind()
    pinned = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {SNAPSHOTS} WHERE {COLUMN} IS NOT NULL")
    ).scalar_one()
    guidance = bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {ATTEMPTS} WHERE asset_kind = 'GUIDANCE_ARTIFACT'")
    ).scalar_one()
    if pinned or guidance:
        raise RuntimeError(
            f"cannot remove the Detail Guidance placement: {pinned} Snapshot(s) pin guidance assets"
            f" and {guidance} upload attempt(s) name a guidance image; frozen evidence is never"
            " silently destroyed"
        )
    _rebuild_attempts(widen=False)
    op.execute(f"ALTER TABLE {SNAPSHOTS} DROP COLUMN {COLUMN}")
