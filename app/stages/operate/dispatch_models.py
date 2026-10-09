"""The M6.5-C dispatch attempts (ADR-0025 §5; migration 0057)."""

from datetime import datetime
from typing import Final

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, func, select
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime

# An attempt that blocks another dispatch of its order, and every new DISPATCH grant for it
# (ADR-0025 §5, M65-06): one in flight or applied; an UNKNOWN, ever; a confirmed or conflicting
# one; a REJECTED one not yet shown undispatched by a read-back. Only NOT_APPLIED_PROVEN, or a
# REJECTED attempt a read-back showed UNDISPATCHED, leaves the way open.
BLOCKING: Final = (
    "state IN ('STARTED', 'APPLIED_PROVEN', 'UNKNOWN')"
    " OR (state = 'REJECTED' AND (verification IS NULL OR verification <> 'UNDISPATCHED'))"
    " OR verification IN ('DISPATCH_CONFIRMED', 'CONFLICT')"
)


class DispatchAttempt(Base):
    """One dispatch of one product order, opened ``STARTED`` before any byte is sent and ended
    exactly once (ADR-0025 §5). Append-only: never deleted, its identity never rewritten, its
    state and verification only ever move forward (triggers)."""

    __tablename__ = "operate_dispatch_attempts"
    __table_args__ = (
        Index(
            "ux_operate_dispatch_attempts_number",
            "product_order_id",
            "attempt_no",
            unique=True,
        ),
        Index(
            "ux_operate_dispatch_attempts_open",
            "product_order_id",
            unique=True,
            sqlite_where=sql("state = 'STARTED'"),
        ),
        CheckConstraint(
            "state IN ('STARTED', 'APPLIED_PROVEN', 'REJECTED', 'NOT_APPLIED_PROVEN', 'UNKNOWN')",
            name="state_valid",
        ),
        CheckConstraint("(state = 'STARTED') = (ended_at IS NULL)", name="ended_has_time"),
        CheckConstraint(
            "verification IS NULL"
            " OR verification IN ('DISPATCH_CONFIRMED', 'UNDISPATCHED', 'CONFLICT')",
            name="verification_valid",
        ),
        CheckConstraint("(verification IS NULL) = (verified_at IS NULL)", name="verified_has_time"),
        # Only a provider's per-order failure can be shown undispatched; nothing verifies an
        # attempt still in flight or one that never left (ADR-0025 §5, GPT audit PR #262).
        CheckConstraint(
            "verification IS NULL OR state IN ('APPLIED_PROVEN', 'REJECTED', 'UNKNOWN')",
            name="verification_of_a_sent_attempt",
        ),
        CheckConstraint(
            "verification IS NOT 'UNDISPATCHED' OR state = 'REJECTED'",
            name="undispatched_only_after_rejection",
        ),
        CheckConstraint(
            "attempt_no >= 1 AND supplier_order_revision >= 1", name="numbers_positive"
        ),
        CheckConstraint("correlation_id <> '' AND grant_id <> ''", name="identities_present"),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_order_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("operate_orders.product_order_id")
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    grant_id: Mapped[str] = mapped_column(String(36))
    marketplace_key: Mapped[str] = mapped_column(String(40))
    marketplace_account_id: Mapped[str] = mapped_column(String(40))
    supplier_order_revision: Mapped[int] = mapped_column(Integer)
    carrier_code: Mapped[str] = mapped_column(String(40))
    tracking_number: Mapped[str] = mapped_column(String(50))
    dispatch_date: Mapped[datetime] = mapped_column(UTCDateTime)
    state: Mapped[str] = mapped_column(String(24))
    fail_code: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    response_status: Mapped[int | None] = mapped_column(Integer)
    verification: Mapped[str | None] = mapped_column(String(24))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    actor: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


def blocking_attempt(session: Session, product_order_id: str) -> bool:
    """Whether an attempt blocks another dispatch of the order, and every new grant for it."""
    return bool(
        session.scalar(
            select(func.count())
            .select_from(DispatchAttempt)
            .where(DispatchAttempt.product_order_id == product_order_id)
            .where(sql(f"({BLOCKING})"))
        )
    )


def latest_attempt(session: Session, product_order_id: str) -> DispatchAttempt | None:
    return session.scalar(
        select(DispatchAttempt)
        .where(DispatchAttempt.product_order_id == product_order_id)
        .order_by(DispatchAttempt.attempt_no.desc())
        .limit(1)
    )
