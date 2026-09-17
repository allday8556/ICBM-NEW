"""M3: what the source wrote for an image reference, and why its fetch target was refused.

Revision ID: 0011_m3_image_reference_diagnostics
Revises: 0010_m3_same_product_pacing
Create Date: 2026-09-18

Issue #52 ruling 5716978033, PR-1 (diagnostic and contract separation only).

A reference the transport's target check refused used to be stored as ``FETCH_FAILED`` with nothing
else: not how the page wrote it, not what it resolved to, not which rule refused it. Three nullable
columns keep that apart from the fetch outcome:

* ``source_form`` — ``ABSOLUTE``, ``PROTOCOL_RELATIVE`` or ``RELATIVE``: how the page wrote the
  reference. The written text itself is never stored; it may carry a token (ADR-0010 §9).
* ``source_trimmed`` — whether surrounding whitespace was trimmed before it was resolved.
* ``target_refusal`` — the closed reason the target check gave, for a reference it refused. Such a
  reference is REVIEW_REQUIRED with no bytes and no locator.

Additive. SQLite adds each column with its own CHECK in place, so the table keeps its append-only
triggers and every existing row, whose new columns are NULL: those references were recorded before
this was kept. The CHECK literals are frozen with this revision; the integration tests compare them
with the ORM models.

Downgrade refuses while any reference holds one of these values: source truth is never silently
destroyed.
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_m3_image_reference_diagnostics"
down_revision: str | None = "0010_m3_same_product_pacing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

IMAGE_REFS = "product_facts_image_refs"
# Vocabularies frozen with this revision (app.collect.facts).
_LOCATOR_FORMS = ("ABSOLUTE", "PROTOCOL_RELATIVE", "RELATIVE")
_TARGET_REFUSALS = (
    "UNPARSEABLE",
    "WHITESPACE",
    "FRAGMENT",
    "NOT_ABSOLUTE",
    "NON_HTTPS",
    "UNSUPPORTED_SCHEME",
    "CREDENTIALS_PRESENT",
    "NON_STANDARD_PORT",
    "HOST_NOT_ALLOWLISTED",
    "PATH_NOT_ALLOWED",
    "QUERY_NOT_ALLOWED",
)
# Dropped in reverse: a column CHECK that names another column must go before that column does.
_COLUMNS = ("source_form", "source_trimmed", "target_refusal")


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IS NULL OR {column} IN ({', '.join(repr(v) for v in values)})"


def _check(name: str, expression: str) -> str:
    return f"CONSTRAINT ck_{IMAGE_REFS}_{name} CHECK ({expression})"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE {IMAGE_REFS} ADD COLUMN source_form VARCHAR(20) "
        + _check("source_form_valid", _in("source_form", _LOCATOR_FORMS))
    )
    op.execute(
        f"ALTER TABLE {IMAGE_REFS} ADD COLUMN source_trimmed BOOLEAN "
        + _check("source_trimmed_has_form", "source_trimmed IS NULL OR source_form IS NOT NULL")
    )
    op.execute(
        f"ALTER TABLE {IMAGE_REFS} ADD COLUMN target_refusal VARCHAR(30) "
        + _check("target_refusal_valid", _in("target_refusal", _TARGET_REFUSALS))
        + " "
        + _check(
            "refused_target_has_nothing",
            "target_refusal IS NULL OR "
            "(status = 'REVIEW_REQUIRED' AND sha256 IS NULL AND locator IS NULL)",
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {IMAGE_REFS} WHERE "
            + " OR ".join(f"{column} IS NOT NULL" for column in _COLUMNS)
        )
    ).scalar_one()
    if held:
        raise RuntimeError(
            f"cannot drop the image reference diagnostics: {held} reference(s) hold them; COLLECT "
            "source truth is never silently destroyed"
        )
    for column in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE {IMAGE_REFS} DROP COLUMN {column}")
