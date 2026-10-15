"""ADR-0042 S1a: the StoreCatalog owner — sync runs, store listings, observations and endings.

Revision ID: 0067_store_catalog
Revises: 0066_landed_cost
Create Date: 2026-10-15

ADR-0042 §5, §12 (SCP-03, SCP-04, SCP-08). Six tables, nothing else changes:

- **``store_catalog_sync_runs``**: one catalog sync of one store, one running per store. Its
  progress moves while it runs; a finished run never changes (trigger). An operator's full
  re-read also carries the shape-only runtime capture of §5.1 item 3 (key paths, JSON types and
  enum tokens, never a value).
- **``store_catalog_listings``**: one store listing's identity, the store and its origin product.
  Immutable: never updated, never deleted (triggers).
- **``store_catalog_field_sets``**: one distinct §5.3 allowlisted field set with its per-group
  digests, keyed by the SHA-256 of its canonical JSON. Append-only.
- **``store_catalog_memberships``**: what one consistent enumeration listed, content-addressed.
  Append-only.
- **``store_catalog_observations``**: what one origin read of one listing answered, at most one per
  listing per sync — the field set it read, a provider removal, or a failure. Append-only.
- **``store_catalog_listing_endings``**: the provider evidence that ended a listing (``DELETE`` or a
  404 of its origin read; ADR-0023 §3), at most one per listing. Append-only.

No marketplace data outside the allowlist is stored, and no buyer data at all.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_store_catalog"
down_revision: str | None = "0066_landed_cost"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNS = "store_catalog_sync_runs"
LISTINGS = "store_catalog_listings"
FIELD_SETS = "store_catalog_field_sets"
MEMBERSHIPS = "store_catalog_memberships"
OBSERVATIONS = "store_catalog_observations"
ENDINGS = "store_catalog_listing_endings"


def _append_only(table: str, what: str) -> None:
    op.execute(
        f"""
        CREATE TRIGGER {table}_no_update
        BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, '{what} are append-only'); END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {table}_no_delete
        BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, '{what} are append-only'); END;
        """
    )


def upgrade() -> None:
    op.create_table(
        MEMBERSHIPS,
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("entries_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'", name="digest_hex"
        ),
        sa.CheckConstraint("entries_json <> ''", name="content_present"),
        sa.PrimaryKeyConstraint("digest"),
    )
    _append_only(MEMBERSHIPS, "store catalog memberships")
    op.create_table(
        RUNS,
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("contract_version", sa.String(length=80), nullable=False),
        sa.Column("full_read", sa.Boolean(), nullable=False),
        sa.Column("membership_digest", sa.String(length=64), nullable=True),
        sa.Column("total_elements", sa.Integer(), nullable=True),
        sa.Column("pages_read", sa.Integer(), nullable=False),
        sa.Column("listings_enumerated", sa.Integer(), nullable=False),
        sa.Column("details_target", sa.Integer(), nullable=False),
        sa.Column("details_done", sa.Integer(), nullable=False),
        sa.Column("details_failed", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("search_shape_json", sa.Text(), nullable=True),
        sa.Column("origin_shape_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('AUTO', 'OPERATOR', 'CONNECT', 'PRE_REGISTRATION')", name="trigger_valid"
        ),
        sa.CheckConstraint("state IN ('RUNNING', 'FINISHED')", name="state_valid"),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN"
            " ('COMPLETE', 'INCOMPLETE', 'UNAVAILABLE', 'ERROR', 'INTERRUPTED')",
            name="outcome_valid",
        ),
        sa.CheckConstraint(
            "(state = 'FINISHED') = (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="finished_has_outcome",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR (outcome = 'COMPLETE') = (error_code IS NULL)",
            name="complete_has_no_error",
        ),
        sa.CheckConstraint(
            "pages_read >= 0 AND (total_elements IS NULL OR total_elements >= 0)"
            " AND listings_enumerated >= 0 AND details_target >= 0 AND details_done >= 0"
            " AND details_failed >= 0 AND details_done + details_failed <= details_target",
            name="counts_bounded",
        ),
        sa.CheckConstraint(
            "marketplace_key <> '' AND marketplace_account_id <> '' AND correlation_id <> ''"
            " AND contract_version <> ''",
            name="identity_present",
        ),
        sa.CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
        sa.CheckConstraint(
            "full_read OR (search_shape_json IS NULL AND origin_shape_json IS NULL)",
            name="shape_only_on_full_read",
        ),
        sa.ForeignKeyConstraint(["membership_digest"], [f"{MEMBERSHIPS}.digest"]),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "ux_store_catalog_sync_runs_one_running",
        RUNS,
        ["marketplace_key", "marketplace_account_id"],
        unique=True,
        sqlite_where=sa.text("state = 'RUNNING'"),
    )
    op.create_index(
        "ix_store_catalog_sync_runs_store",
        RUNS,
        ["marketplace_key", "marketplace_account_id", "started_at"],
        unique=False,
    )
    # A finished run is evidence: it never changes, and no run is ever deleted.
    op.execute(
        f"""
        CREATE TRIGGER {RUNS}_finished_is_final
        BEFORE UPDATE ON {RUNS}
        WHEN OLD.state = 'FINISHED'
          OR NEW.run_id IS NOT OLD.run_id
          OR NEW.marketplace_key IS NOT OLD.marketplace_key
          OR NEW.marketplace_account_id IS NOT OLD.marketplace_account_id
          OR NEW.trigger IS NOT OLD.trigger
          OR NEW.contract_version IS NOT OLD.contract_version
          OR NEW.full_read IS NOT OLD.full_read
          OR NEW.correlation_id IS NOT OLD.correlation_id
          OR NEW.started_at IS NOT OLD.started_at
        BEGIN SELECT RAISE(ABORT, 'a finished catalog sync run is final'); END;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {RUNS}_no_delete
        BEFORE DELETE ON {RUNS}
        BEGIN SELECT RAISE(ABORT, 'catalog sync runs are never deleted'); END;
        """
    )
    op.create_table(
        LISTINGS,
        sa.Column("listing_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("origin_product_no", sa.String(length=20), nullable=False),
        sa.Column("first_seen_run_id", sa.String(length=36), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "marketplace_key <> '' AND marketplace_account_id <> ''"
            " AND origin_product_no <> '' AND origin_product_no NOT GLOB '*[^0-9]*'",
            name="identity_valid",
        ),
        sa.ForeignKeyConstraint(["first_seen_run_id"], [f"{RUNS}.run_id"]),
        sa.PrimaryKeyConstraint("listing_id"),
    )
    op.create_index(
        "ux_store_catalog_listings_identity",
        LISTINGS,
        ["marketplace_key", "marketplace_account_id", "origin_product_no"],
        unique=True,
    )
    _append_only(LISTINGS, "store listings")
    op.create_table(
        FIELD_SETS,
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("fields_version", sa.String(length=80), nullable=False),
        sa.Column("fields_json", sa.Text(), nullable=False),
        sa.Column("field_digests_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'", name="digest_hex"
        ),
        sa.CheckConstraint(
            "fields_version <> '' AND fields_json <> '' AND field_digests_json <> ''",
            name="content_present",
        ),
        sa.PrimaryKeyConstraint("digest"),
    )
    _append_only(FIELD_SETS, "store listing field sets")
    op.create_table(
        OBSERVATIONS,
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("listing_id", sa.String(length=36), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("enumerated", sa.Boolean(), nullable=False),
        sa.Column("channel_product_no", sa.String(length=20), nullable=True),
        sa.Column("listed_seller_code", sa.String(length=64), nullable=True),
        sa.Column("sale_status", sa.String(length=32), nullable=True),
        sa.Column("display_status", sa.String(length=32), nullable=True),
        sa.Column("sale_price", sa.Integer(), nullable=True),
        sa.Column("stock_quantity", sa.Integer(), nullable=True),
        sa.Column("seller_management_code", sa.Text(), nullable=True),
        sa.Column("seller_barcode", sa.Text(), nullable=True),
        sa.Column("field_set_digest", sa.String(length=64), nullable=True),
        sa.Column("removal_evidence", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("result IN ('OBSERVED', 'REMOVED', 'READ_FAILED')", name="result_valid"),
        sa.CheckConstraint(
            "(result = 'OBSERVED') = (field_set_digest IS NOT NULL)",
            name="only_observed_carries_fields",
        ),
        sa.CheckConstraint(
            "result = 'OBSERVED' OR (sale_status IS NULL AND display_status IS NULL"
            " AND sale_price IS NULL AND stock_quantity IS NULL"
            " AND seller_management_code IS NULL AND seller_barcode IS NULL)",
            name="only_observed_carries_columns",
        ),
        sa.CheckConstraint(
            "(result = 'READ_FAILED') = (error_code IS NOT NULL)", name="failure_has_code"
        ),
        sa.CheckConstraint(
            "(result = 'REMOVED') = (removal_evidence IS NOT NULL)", name="removal_has_evidence"
        ),
        sa.CheckConstraint(
            "removal_evidence IS NULL OR removal_evidence IN ('STATUS_DELETE', 'HTTP_404')",
            name="removal_evidence_valid",
        ),
        sa.CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
        sa.ForeignKeyConstraint(["run_id"], [f"{RUNS}.run_id"]),
        sa.ForeignKeyConstraint(["listing_id"], [f"{LISTINGS}.listing_id"]),
        sa.ForeignKeyConstraint(["field_set_digest"], [f"{FIELD_SETS}.digest"]),
        sa.PrimaryKeyConstraint("observation_id"),
    )
    op.create_index(
        "ux_store_catalog_observations_run_listing",
        OBSERVATIONS,
        ["run_id", "listing_id"],
        unique=True,
    )
    op.create_index(
        "ix_store_catalog_observations_listing",
        OBSERVATIONS,
        ["listing_id", "observed_at"],
        unique=False,
    )
    _append_only(OBSERVATIONS, "store listing observations")
    op.create_table(
        ENDINGS,
        sa.Column("listing_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("evidence", sa.String(length=20), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("evidence IN ('STATUS_DELETE', 'HTTP_404')", name="evidence_valid"),
        sa.ForeignKeyConstraint(["listing_id"], [f"{LISTINGS}.listing_id"]),
        sa.ForeignKeyConstraint(["run_id"], [f"{RUNS}.run_id"]),
        sa.PrimaryKeyConstraint("listing_id"),
    )
    _append_only(ENDINGS, "store listing endings")


def downgrade() -> None:
    for table in (ENDINGS, OBSERVATIONS, FIELD_SETS, LISTINGS):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
    op.drop_table(ENDINGS)
    op.drop_index("ix_store_catalog_observations_listing", table_name=OBSERVATIONS)
    op.drop_index("ux_store_catalog_observations_run_listing", table_name=OBSERVATIONS)
    op.drop_table(OBSERVATIONS)
    op.drop_table(FIELD_SETS)
    op.drop_index("ux_store_catalog_listings_identity", table_name=LISTINGS)
    op.drop_table(LISTINGS)
    op.execute(f"DROP TRIGGER IF EXISTS {RUNS}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {RUNS}_finished_is_final")
    op.drop_index("ix_store_catalog_sync_runs_store", table_name=RUNS)
    op.drop_index("ux_store_catalog_sync_runs_one_running", table_name=RUNS)
    op.drop_table(RUNS)
    op.execute(f"DROP TRIGGER IF EXISTS {MEMBERSHIPS}_no_delete")
    op.execute(f"DROP TRIGGER IF EXISTS {MEMBERSHIPS}_no_update")
    op.drop_table(MEMBERSHIPS)
