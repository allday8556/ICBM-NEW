"""ADR-0033 G2: the Detail Guidance owner — notice identities, rendered images and revisions.

Revision ID: 0058_detail_guidance
Revises: 0057_m65_dispatch_stage
Create Date: 2026-10-10

ADR-0033 §2. It adds three tables and touches no existing table, row, trigger or index.

- `detail_guidances`: the immutable identity of one store-wide notice — its placement (`TOP`,
  `BOTTOM`) and kind (`STANDING`, `PERIOD`). A placement has at most one `STANDING` identity (a
  partial unique index), and its newest revision is the placement's standing notice: so at most one
  `STANDING` revision is ever current per placement (DG-03), by structure.
- `guidance_image_artifacts`: one row per rendered PNG, by its SHA-256, with its dimensions, size,
  template, renderer version and font. The bytes live in the content-addressed guidance store
  (`<data>/guidance/sha256/xx/<sha>`), never in the database. Store-level, never product-scoped.
- `detail_guidance_revisions`: append-only revisions of one identity. The number opens at one and
  moves by exactly one; each names its template, its plain-text content (a JSON object) and that
  content's fingerprint, `enabled`, the half-open UTC period of a `PERIOD` notice (`starts_at <
  ends_at`; a `STANDING` revision has none) and the image it was saved with. Turning a notice off,
  or ending a period early, appends a revision with `enabled = 0`.

The database refuses an update or a delete of any of the three, a second standing identity of a
placement, a gap in the numbering, a revision whose kind is not its identity's, a period that does
not match the kind, and a revision whose template is not its image's.

**Every canonical table starts empty** (M0 acceptance): nothing is seeded.

**Downgrade fails closed** while any revision exists: an operator's notice history is never
silently dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_detail_guidance"
down_revision: str | None = "0057_m65_dispatch_stage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUIDANCES = "detail_guidances"
ARTIFACTS = "guidance_image_artifacts"
REVISIONS = "detail_guidance_revisions"

# Every table this migration creates, newest dependency last: the order a downgrade drops them.
CREATED = (GUIDANCES, ARTIFACTS, REVISIONS)
# The five official templates (ADR-0033 §1, §11), as `renderer.Template` names them.
TEMPLATES = "template IN ('CLEAN', 'MODERN', 'WARM', 'DOMESTIC', 'OVERSEAS')"


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _trigger(table: str, name: str, event: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER trg_{table}_{name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _immutable(table: str) -> None:
    _trigger(table, "no_update", "UPDATE", _raise(f"a {table} row is never updated", "1"))
    _trigger(table, "no_delete", "DELETE", _raise(f"a {table} row is never deleted", "1"))


def upgrade() -> None:
    op.create_table(
        GUIDANCES,
        sa.Column("guidance_id", sa.String(length=36), nullable=False),
        sa.Column("placement", sa.String(length=8), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(GUIDANCES, "guidance_id <> ''", "id_present"),
        _check(GUIDANCES, "placement IN ('TOP', 'BOTTOM')", "placement_known"),
        _check(GUIDANCES, "kind IN ('STANDING', 'PERIOD')", "kind_known"),
        sa.PrimaryKeyConstraint("guidance_id", name=op.f(f"pk_{GUIDANCES}")),
    )
    # At most one standing notice per placement (ADR-0033 §1, §2; DG-03).
    op.create_index(
        "ux_detail_guidances_one_standing",
        GUIDANCES,
        ["placement"],
        unique=True,
        sqlite_where=sa.text("kind = 'STANDING'"),
    )
    op.create_table(
        ARTIFACTS,
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("renderer_version", sa.String(length=64), nullable=False),
        sa.Column("template", sa.String(length=16), nullable=False),
        sa.Column("font_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(ARTIFACTS, _hex64("sha256"), "sha256_hex"),
        _check(ARTIFACTS, "width > 0 AND height > 0 AND byte_size > 0", "sizes_positive"),
        _check(ARTIFACTS, "renderer_version <> ''", "renderer_version_present"),
        _check(ARTIFACTS, TEMPLATES, "template_known"),
        _check(ARTIFACTS, _hex64("font_sha256"), "font_sha256_hex"),
        sa.PrimaryKeyConstraint("sha256", name=op.f(f"pk_{ARTIFACTS}")),
    )
    op.create_table(
        REVISIONS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("guidance_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("template", sa.String(length=16), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("image_sha256", sa.String(length=64), nullable=False),
        sa.Column("authored_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("authored_at", sa.DateTime(), nullable=False),
        _check(REVISIONS, "seq >= 1", "seq_positive"),
        _check(REVISIONS, "kind IN ('STANDING', 'PERIOD')", "kind_known"),
        _check(REVISIONS, TEMPLATES, "template_known"),
        _check(
            REVISIONS,
            "json_valid(content_json) AND json_type(content_json) = 'object'",
            "content_is_object",
        ),
        _check(REVISIONS, _hex64("content_fingerprint"), "content_fingerprint_hex"),
        _check(REVISIONS, "enabled IN (0, 1)", "enabled_flag"),
        # A period notice has a non-empty half-open period; a standing notice has none.
        _check(
            REVISIONS,
            "(kind = 'PERIOD' AND starts_at IS NOT NULL AND ends_at IS NOT NULL"
            " AND starts_at < ends_at)"
            " OR (kind = 'STANDING' AND starts_at IS NULL AND ends_at IS NULL)",
            "period_matches_kind",
        ),
        _check(REVISIONS, "authored_by <> ''", "authored_by_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        sa.ForeignKeyConstraint(
            ["guidance_id"],
            [f"{GUIDANCES}.guidance_id"],
            name=op.f(f"fk_{REVISIONS}_guidance_id_{GUIDANCES}"),
        ),
        sa.ForeignKeyConstraint(
            ["image_sha256"],
            [f"{ARTIFACTS}.sha256"],
            name=op.f(f"fk_{REVISIONS}_image_sha256_{ARTIFACTS}"),
        ),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{REVISIONS}")),
        sa.UniqueConstraint("guidance_id", "seq", name=op.f(f"uq_{REVISIONS}_guidance_id_seq")),
    )
    _install_triggers()


def _install_triggers() -> None:
    _immutable(GUIDANCES)
    _immutable(ARTIFACTS)
    _trigger(
        REVISIONS,
        "seq_follows",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision follows the one before it",
            f"NEW.seq <> 1 + (SELECT COALESCE(MAX(r.seq), 0) FROM {REVISIONS} r"
            " WHERE r.guidance_id = NEW.guidance_id)",
        ),
    )
    _trigger(
        REVISIONS,
        "kind_is_identity_kind",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision has the kind of its notice",
            f"NEW.kind IS NOT (SELECT g.kind FROM {GUIDANCES} g"
            " WHERE g.guidance_id = NEW.guidance_id)",
        ),
    )
    _trigger(
        REVISIONS,
        "template_is_image_template",
        "INSERT",
        _raise(
            f"{REVISIONS}: a revision names an image of its template",
            f"NEW.template IS NOT (SELECT a.template FROM {ARTIFACTS} a"
            " WHERE a.sha256 = NEW.image_sha256)",
        ),
    )
    _immutable(REVISIONS)


def downgrade() -> None:
    held = op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {REVISIONS}")).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot drop the Detail Guidance owner: {held} revision(s) in {REVISIONS} are held;"
            " an operator's notice history is never silently destroyed"
        )
    op.drop_index("ux_detail_guidances_one_standing", table_name=GUIDANCES)
    for table in reversed(CREATED):
        op.drop_table(table)
