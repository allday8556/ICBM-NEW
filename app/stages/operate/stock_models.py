"""The M6-B supplier stock recheck rows (ADR-0023 §4; migration 0049)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime


class StockRecheck(Base):
    """One re-collection M6 asked COLLECT for, and what its revision judged. One pending at a time
    per source product."""

    __tablename__ = "operate_stock_rechecks"
    __table_args__ = (
        Index(
            "ix_operate_stock_rechecks_source", "supplier_key", "source_product_id", "requested_at"
        ),
        Index(
            "ux_operate_stock_rechecks_one_pending",
            "supplier_key",
            "source_product_id",
            unique=True,
            sqlite_where=sql("state IN ('SUBMITTING', 'REQUESTED')"),
        ),
        CheckConstraint("trigger IN ('AUTO', 'OPERATOR')", name="trigger_valid"),
        CheckConstraint("state IN ('SUBMITTING', 'REQUESTED', 'FINISHED')", name="state_valid"),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('RECORDED', 'NO_REVISION', 'FAILED', 'REFUSED')",
            name="outcome_valid",
        ),
        CheckConstraint(
            "(state = 'FINISHED') = (finished_at IS NOT NULL AND outcome IS NOT NULL)",
            name="finished_has_outcome",
        ),
        CheckConstraint(
            "state <> 'REQUESTED' OR collection_run_id IS NOT NULL", name="requested_has_run"
        ),
        CheckConstraint(
            "availability IS NULL OR availability IN ('ON_SALE', 'SOLD_OUT', 'REVIEW_REQUIRED')",
            name="availability_valid",
        ),
        CheckConstraint(
            "(outcome = 'RECORDED') = (revision_id IS NOT NULL)", name="recorded_has_revision"
        ),
        CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
    )

    recheck_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    supplier_key: Mapped[str] = mapped_column(String(40))
    source_product_id: Mapped[str] = mapped_column(String(200))
    trigger: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    outcome: Mapped[str | None] = mapped_column(String(20))
    collection_run_id: Mapped[str | None] = mapped_column(String(36))
    revision_id: Mapped[str | None] = mapped_column(String(36))
    availability: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(64))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
