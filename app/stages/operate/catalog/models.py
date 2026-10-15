"""The StoreCatalog rows (ADR-0042 §5, §12; migration 0067).

- ``store_catalog_sync_runs``: one catalog sync of one store. Its progress moves while it runs;
  once finished it never changes (trigger).
- ``store_catalog_listings``: one store listing's identity — the store and its origin product.
  Immutable (triggers): a listing is never a product (SCP-01).
- ``store_catalog_field_sets``: one distinct §5.3 allowlisted field set, content-addressed by the
  SHA-256 of its canonical JSON, with its per-group digests. An unchanged listing's observations
  share one row, so a sync that changes nothing stores no field text again. Append-only.
- ``store_catalog_observations``: what one sync saw of one listing — the field set it read, a
  provider removal, or a failure. Append-only, one per listing per sync (ADR-0042 §5.4).
- ``store_catalog_listing_endings``: the provider evidence that ended a listing (ADR-0023 §3: the
  sale status ``DELETE`` or a 404 of its origin read). Append-only, at most one per listing.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class CatalogSyncRun(Base):
    """One catalog sync of one store, automatic or requested. One running per store."""

    __tablename__ = "store_catalog_sync_runs"
    __table_args__ = (
        Index(
            "ux_store_catalog_sync_runs_one_running",
            "marketplace_key",
            "marketplace_account_id",
            unique=True,
            sqlite_where=sql("state = 'RUNNING'"),
        ),
        Index(
            "ix_store_catalog_sync_runs_store",
            "marketplace_key",
            "marketplace_account_id",
            "started_at",
        ),
        CheckConstraint(
            "trigger IN ('AUTO', 'OPERATOR', 'CONNECT', 'PRE_REGISTRATION')", name="trigger_valid"
        ),
        CheckConstraint("state IN ('RUNNING', 'FINISHED')", name="state_valid"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN"
            " ('COMPLETE', 'INCOMPLETE', 'UNAVAILABLE', 'ERROR', 'INTERRUPTED')",
            name="outcome_valid",
        ),
        CheckConstraint(
            "(state = 'FINISHED') = (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="finished_has_outcome",
        ),
        CheckConstraint(
            "outcome IS NULL OR (outcome = 'COMPLETE') = (error_code IS NULL)",
            name="complete_has_no_error",
        ),
        CheckConstraint(
            "pages_read >= 0 AND (total_elements IS NULL OR total_elements >= 0)"
            " AND listings_enumerated >= 0 AND details_target >= 0 AND details_done >= 0"
            " AND details_failed >= 0 AND details_done + details_failed <= details_target",
            name="counts_bounded",
        ),
        CheckConstraint(
            "marketplace_key <> '' AND marketplace_account_id <> '' AND correlation_id <> ''"
            " AND contract_version <> ''",
            name="identity_present",
        ),
        CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    marketplace_account_id: Mapped[str] = mapped_column(String(40))
    trigger: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    outcome: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(64))
    contract_version: Mapped[str] = mapped_column(String(80))
    total_elements: Mapped[int | None] = mapped_column(Integer)
    pages_read: Mapped[int] = mapped_column(Integer)
    listings_enumerated: Mapped[int] = mapped_column(Integer)
    details_target: Mapped[int] = mapped_column(Integer)
    details_done: Mapped[int] = mapped_column(Integer)
    details_failed: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[str] = mapped_column(String(64))
    # The sync's ``as_of`` (ADR-0042 §5.2): what it proves holds relative to its start.
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class CatalogListing(Base):
    """One store listing: the store and its provider origin product. Never a product."""

    __tablename__ = "store_catalog_listings"
    __table_args__ = (
        Index(
            "ux_store_catalog_listings_identity",
            "marketplace_key",
            "marketplace_account_id",
            "origin_product_no",
            unique=True,
        ),
        CheckConstraint(
            "marketplace_key <> '' AND marketplace_account_id <> ''"
            " AND origin_product_no <> '' AND origin_product_no NOT GLOB '*[^0-9]*'",
            name="identity_valid",
        ),
    )

    listing_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    marketplace_account_id: Mapped[str] = mapped_column(String(40))
    origin_product_no: Mapped[str] = mapped_column(String(20))
    first_seen_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("store_catalog_sync_runs.run_id")
    )
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CatalogFieldSet(Base):
    """One distinct allowlisted field set, keyed by the SHA-256 of its canonical JSON."""

    __tablename__ = "store_catalog_field_sets"
    __table_args__ = (
        CheckConstraint("length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'", name="digest_hex"),
        CheckConstraint(
            "fields_version <> '' AND fields_json <> '' AND field_digests_json <> ''",
            name="content_present",
        ),
    )

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    fields_version: Mapped[str] = mapped_column(String(80))
    fields_json: Mapped[str] = mapped_column(Text)
    field_digests_json: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CatalogObservation(Base):
    """What one sync saw of one listing. Append-only; one per listing per sync."""

    __tablename__ = "store_catalog_observations"
    __table_args__ = (
        Index("ux_store_catalog_observations_run_listing", "run_id", "listing_id", unique=True),
        Index("ix_store_catalog_observations_listing", "listing_id", "observed_at"),
        CheckConstraint("result IN ('OBSERVED', 'REMOVED', 'READ_FAILED')", name="result_valid"),
        CheckConstraint(
            "(result = 'OBSERVED') = (field_set_digest IS NOT NULL)",
            name="only_observed_carries_fields",
        ),
        CheckConstraint(
            "result = 'OBSERVED' OR (sale_status IS NULL AND display_status IS NULL"
            " AND sale_price IS NULL AND stock_quantity IS NULL"
            " AND seller_management_code IS NULL AND seller_barcode IS NULL)",
            name="only_observed_carries_columns",
        ),
        CheckConstraint(
            "(result = 'READ_FAILED') = (error_code IS NOT NULL)", name="failure_has_code"
        ),
        CheckConstraint(
            "(result = 'REMOVED') = (removal_evidence IS NOT NULL)", name="removal_has_evidence"
        ),
        CheckConstraint(
            "removal_evidence IS NULL OR removal_evidence IN ('STATUS_DELETE', 'HTTP_404')",
            name="removal_evidence_valid",
        ),
        CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
    )

    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("store_catalog_sync_runs.run_id"))
    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("store_catalog_listings.listing_id")
    )
    result: Mapped[str] = mapped_column(String(20))
    # Whether this sync's enumeration listed the listing; a listing it did not list is read only
    # for provider evidence of its end (ADR-0042 §5.2, ADR-0023 §3).
    enumerated: Mapped[bool] = mapped_column(Boolean)
    channel_product_no: Mapped[str | None] = mapped_column(String(20))
    listed_seller_code: Mapped[str | None] = mapped_column(String(64))
    sale_status: Mapped[str | None] = mapped_column(String(32))
    display_status: Mapped[str | None] = mapped_column(String(32))
    sale_price: Mapped[int | None] = mapped_column(Integer)
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    seller_management_code: Mapped[str | None] = mapped_column(Text)
    seller_barcode: Mapped[str | None] = mapped_column(Text)
    field_set_digest: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("store_catalog_field_sets.digest")
    )
    removal_evidence: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CatalogListingEnding(Base):
    """The provider evidence that ended one listing. Append-only; never reopened."""

    __tablename__ = "store_catalog_listing_endings"
    __table_args__ = (
        CheckConstraint("evidence IN ('STATUS_DELETE', 'HTTP_404')", name="evidence_valid"),
    )

    listing_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("store_catalog_listings.listing_id"), primary_key=True
    )
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("store_catalog_sync_runs.run_id"))
    evidence: Mapped[str] = mapped_column(String(20))
    ended_at: Mapped[datetime] = mapped_column(UTCDateTime)
