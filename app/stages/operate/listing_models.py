"""The M6-A listing-state sync rows (ADR-0023 §3; migration 0048)."""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class ListingSyncRun(Base):
    """One pass over the ACTIVE registrations, automatic or operator-requested. One at a time."""

    __tablename__ = "operate_listing_sync_runs"
    __table_args__ = (
        Index(
            "ux_operate_listing_sync_runs_one_running",
            "state",
            unique=True,
            sqlite_where=sql("state = 'RUNNING'"),
        ),
        Index("ix_operate_listing_sync_runs_started", "started_at"),
        CheckConstraint("trigger IN ('AUTO', 'OPERATOR')", name="trigger_valid"),
        CheckConstraint("state IN ('RUNNING', 'FINISHED')", name="state_valid"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('COMPLETED', 'COMPLETED_WITH_FAILURES',"
            " 'SESSION_UNAVAILABLE', 'RATE_LIMITED', 'INTERRUPTED')",
            name="outcome_valid",
        ),
        CheckConstraint(
            "(state = 'FINISHED') = (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="finished_has_outcome",
        ),
        CheckConstraint(
            "targets >= 0 AND observed >= 0 AND failed >= 0 AND observed + failed <= targets",
            name="counts_bounded",
        ),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    outcome: Mapped[str | None] = mapped_column(String(32))
    targets: Mapped[int] = mapped_column(Integer)
    observed: Mapped[int] = mapped_column(Integer)
    failed: Mapped[int] = mapped_column(Integer)
    correlation_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ListingObservation(Base):
    """What one origin-product read showed for one registration. Append-only (triggers)."""

    __tablename__ = "operate_listing_observations"
    __table_args__ = (
        Index("ix_operate_listing_observations_registration", "registration_id", "observed_at"),
        CheckConstraint("result IN ('OBSERVED', 'NOT_FOUND', 'READ_FAILED')", name="result_valid"),
        CheckConstraint(
            "result = 'OBSERVED' OR (sale_status IS NULL AND display_status IS NULL"
            " AND sale_price IS NULL AND stock_quantity IS NULL AND seller_code_matches IS NULL)",
            name="only_observed_carries_fields",
        ),
        CheckConstraint(
            "(result = 'READ_FAILED') = (error_code IS NOT NULL)", name="failure_has_code"
        ),
        CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
    )

    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("operate_listing_sync_runs.run_id"))
    registration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("marketplace_registrations.registration_id")
    )
    result: Mapped[str] = mapped_column(String(20))
    sale_status: Mapped[str | None] = mapped_column(String(32))
    display_status: Mapped[str | None] = mapped_column(String(32))
    sale_price: Mapped[int | None] = mapped_column(Integer)
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    seller_code_matches: Mapped[bool | None] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
