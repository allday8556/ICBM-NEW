"""M3 PR-B: COLLECT source truth — revisions, fields, evidence, image references, source assets.

Revision ID: 0007_m3_product_facts_revisions
Revises: 0006_m2_marketplace_connections
Create Date: 2026-09-16

Additive only (ADR-0010 §6–§9). Every successful collection appends one immutable revision with
its fields, product-scoped evidence and ordered image references; image bytes are stored once as
content-addressed source assets. Every table is append-only: the database rejects UPDATE and
DELETE, as for audit_events. No canonical Product table is created (ADR-0010 §1).

The CHECK expressions are frozen with this revision; the integration tests compare them with the
ORM models.

Downgrade refuses while any revision or source asset exists: source truth is never silently
destroyed.
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_m3_product_facts_revisions"
down_revision: str | None = "0006_m2_marketplace_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REVISIONS = "product_facts_revisions"
FIELDS = "product_facts_fields"
EVIDENCE = "product_facts_evidence"
ASSETS = "source_assets"
IMAGE_REFS = "product_facts_image_refs"
# Creation order; dropped in reverse.
TABLES = (REVISIONS, FIELDS, EVIDENCE, ASSETS, IMAGE_REFS)

# Vocabularies frozen with this revision (app.collect.facts).
_FACTS_STATUSES = ("CONFIRMED", "REVIEW_REQUIRED")
_FIELD_STATUSES = ("CONFIRMED", "ABSENT", "REVIEW_REQUIRED")
_LEVELS = ("CORE", "COVERAGE")
_EVIDENCE_KINDS = (
    "DOM_TEXT",
    "ATTRIBUTE",
    "EMBEDDED_JSON",
    "JSON_LD",
    "CONTROL_STATE",
    "URL",
    "PRODUCT_HTML_FRAGMENT",
    "IMAGE",
)
_ROLES = ("REPRESENTATIVE", "DETAIL")
_ISSUES = (
    "BAD_HOST",
    "BAD_CONTENT_TYPE",
    "OVERSIZE",
    "BUDGET_EXHAUSTED",
    "UNSUPPORTED_FORMAT",
    "FETCH_FAILED",
)
_OBSERVED_MAX_BYTES = 4096


def _in(column: str, values: Iterable[str], *, nullable: bool = False) -> str:
    clause = f"{column} IN ({', '.join(repr(v) for v in values)})"
    return f"{column} IS NULL OR {clause}" if nullable else clause


def _hex64(column: str, *, nullable: bool = False) -> str:
    clause = f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"
    return f"{column} IS NULL OR ({clause})" if nullable else clause


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    op.create_table(
        REVISIONS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("supplier_key", sa.String(length=40), nullable=False),
        sa.Column("source_product_id", sa.String(length=200), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("extractor_revision", sa.String(length=64), nullable=False),
        sa.Column("extractor_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("collection_run_id", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("facts_status", sa.String(length=20), nullable=False),
        _check(REVISIONS, "sequence >= 1", "sequence_positive"),
        _check(REVISIONS, "supplier_key <> ''", "supplier_key_present"),
        _check(REVISIONS, "source_product_id <> ''", "source_identity_present"),
        _check(REVISIONS, "source_url LIKE 'https://%'", "source_url_https"),
        _check(REVISIONS, "currency = 'KRW'", "currency_krw"),
        _check(REVISIONS, "extractor_revision <> ''", "extractor_revision_present"),
        _check(REVISIONS, _hex64("extractor_fingerprint"), "extractor_fingerprint_hex"),
        _check(REVISIONS, _hex64("source_fingerprint"), "source_fingerprint_hex"),
        _check(REVISIONS, "collection_run_id <> ''", "collection_run_present"),
        _check(REVISIONS, "correlation_id <> ''", "correlation_present"),
        _check(REVISIONS, _in("facts_status", _FACTS_STATUSES), "facts_status_valid"),
        sa.PrimaryKeyConstraint("revision_id", name=op.f(f"pk_{REVISIONS}")),
        sa.UniqueConstraint(
            "supplier_key",
            "source_product_id",
            "sequence",
            name=op.f(f"uq_{REVISIONS}_supplier_key_source_product_id_sequence"),
        ),
    )
    op.create_table(
        FIELDS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("field_key", sa.String(length=40), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=True),
        sa.Column("field_fingerprint", sa.String(length=64), nullable=False),
        _check(FIELDS, "field_key <> ''", "field_key_present"),
        _check(FIELDS, _in("level", _LEVELS), "level_valid"),
        _check(FIELDS, _in("status", _FIELD_STATUSES), "status_valid"),
        _check(FIELDS, "value_json IS NULL OR json_valid(value_json)", "value_is_json"),
        _check(FIELDS, "status <> 'ABSENT' OR value_json IS NULL", "absent_has_no_value"),
        _check(FIELDS, "status <> 'CONFIRMED' OR value_json IS NOT NULL", "confirmed_has_value"),
        _check(FIELDS, _hex64("field_fingerprint"), "field_fingerprint_hex"),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            [f"{REVISIONS}.revision_id"],
            name=op.f(f"fk_{FIELDS}_revision_id_{REVISIONS}"),
        ),
        sa.PrimaryKeyConstraint("revision_id", "field_key", name=op.f(f"pk_{FIELDS}")),
    )
    op.create_table(
        EVIDENCE,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("field_key", sa.String(length=40), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("observed", sa.Text(), nullable=True),
        sa.Column("normalized", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        _check(EVIDENCE, "ordinal >= 0", "ordinal_non_negative"),
        _check(EVIDENCE, _in("kind", _EVIDENCE_KINDS), "kind_valid"),
        _check(EVIDENCE, "locator <> ''", "locator_present"),
        _check(
            EVIDENCE,
            f"observed IS NULL OR length(CAST(observed AS BLOB)) <= {_OBSERVED_MAX_BYTES}",
            "observed_bounded",
        ),
        _check(EVIDENCE, _in("status", _FIELD_STATUSES), "status_valid"),
        _check(EVIDENCE, _hex64("digest"), "digest_hex"),
        sa.ForeignKeyConstraint(
            ["revision_id", "field_key"],
            [f"{FIELDS}.revision_id", f"{FIELDS}.field_key"],
            name=op.f(f"fk_{EVIDENCE}_revision_id_{FIELDS}"),
        ),
        sa.PrimaryKeyConstraint("revision_id", "field_key", "ordinal", name=op.f(f"pk_{EVIDENCE}")),
    )
    op.create_table(
        ASSETS,
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=40), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("stored_at", sa.DateTime(), nullable=False),
        _check(ASSETS, _hex64("sha256"), "sha256_hex"),
        _check(ASSETS, "mime_type LIKE 'image/%'", "mime_is_image"),
        _check(ASSETS, "byte_size > 0", "byte_size_positive"),
        _check(ASSETS, "width > 0 AND height > 0", "dimensions_positive"),
        sa.PrimaryKeyConstraint("sha256", name=op.f(f"pk_{ASSETS}")),
    )
    op.create_table(
        IMAGE_REFS,
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("issue", sa.String(length=30), nullable=True),
        sa.Column("http_etag", sa.Text(), nullable=True),
        sa.Column("http_last_modified", sa.Text(), nullable=True),
        _check(IMAGE_REFS, _in("role", _ROLES), "role_valid"),
        _check(IMAGE_REFS, "ordinal >= 0", "ordinal_non_negative"),
        _check(IMAGE_REFS, "host <> ''", "host_present"),
        _check(IMAGE_REFS, "provenance <> ''", "provenance_present"),
        _check(IMAGE_REFS, "locator IS NULL OR locator LIKE 'https://%'", "locator_https"),
        _check(IMAGE_REFS, _hex64("sha256", nullable=True), "sha256_hex"),
        _check(IMAGE_REFS, _in("status", _FACTS_STATUSES), "status_valid"),
        _check(IMAGE_REFS, _in("issue", _ISSUES, nullable=True), "issue_valid"),
        _check(
            IMAGE_REFS,
            "(status = 'CONFIRMED' AND sha256 IS NOT NULL AND issue IS NULL)"
            " OR (status = 'REVIEW_REQUIRED' AND issue IS NOT NULL)",
            "status_matches_issue",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"],
            [f"{REVISIONS}.revision_id"],
            name=op.f(f"fk_{IMAGE_REFS}_revision_id_{REVISIONS}"),
        ),
        sa.ForeignKeyConstraint(
            ["sha256"], [f"{ASSETS}.sha256"], name=op.f(f"fk_{IMAGE_REFS}_sha256_{ASSETS}")
        ),
        sa.PrimaryKeyConstraint("revision_id", "role", "ordinal", name=op.f(f"pk_{IMAGE_REFS}")),
    )
    # Append-only is enforced by the database itself, as for audit_events.
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


def downgrade() -> None:
    # Revisions and source assets are source truth: only empty tables are dropped.
    bind = op.get_bind()
    for table in (REVISIONS, ASSETS):
        held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if held:
            raise RuntimeError(
                f"cannot drop {table}: {held} row(s) of source truth are held; COLLECT source "
                "truth is never silently destroyed"
            )
    for table in reversed(TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        op.drop_table(table)
