"""The FX snapshot owner's pure reads and rules (ADR-0038 §3; FX-02, FX-03).

What a pricing owner needs of a recorded rate, and nothing that fetches one: the snapshot as read
(:class:`FxSnapshot`), the newest snapshot of a currency on or before a day
(:func:`newest_snapshot`, inside the caller's unit of work), the KST day and the FX-03 staleness
rule (:func:`fx_reason`). It loads no publication adapter and no network module, so the pricing
owner — and everything that reads prices, such as REGISTER's preflight — reads rates without one.
``app.capabilities.overseas.fx`` (the store, the key, the refresh) re-exports every name here.

A business day is Monday to Friday, counted in KST. **There is no holiday calendar**: a run of
weekday holidays (설날, 추석) counts as business days, so a rate can turn stale over a long holiday.
That errs on the side of REVIEW_REQUIRED, never of a guessed rate.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.overseas.fx_models import FxRateSnapshot

CURRENCIES: Final = ("CNY", "JPY", "USD")
KST: Final = timezone(timedelta(hours=9))
# FX-03: a snapshot more than this many business days old is stale.
STALE_AFTER_BUSINESS_DAYS: Final = 3

PRICING_FX_STALE: Final = "PRICING_FX_STALE"
PRICING_FX_UNAVAILABLE: Final = "PRICING_FX_UNAVAILABLE"


@dataclass(frozen=True)
class FxSnapshot:
    """One recorded rate. ``tts`` is per ``unit`` of the currency (1, or 100 for JPY)."""

    snapshot_id: str
    publication_date: date
    currency: str
    rate_type: str
    source_code: str
    unit: int
    tts: str
    deal_bas_r: str
    source: str
    fetched_at: datetime

    def tts_per_unit(self) -> Decimal:
        """The won price of one unit of the currency, exact (the unit is 1 or 100)."""
        return Decimal(self.tts) / self.unit


def kst_day(moment: datetime) -> date:
    """The KST calendar day of an aware moment."""
    return moment.astimezone(KST).date()


def business_days_after(start: date, end: date) -> int:
    """How many Monday-to-Friday days ``d`` lie in ``start < d <= end`` (0 when ``end <= start``).
    No holiday calendar (module docstring)."""
    if end <= start:
        return 0
    weeks, rest = divmod((end - start).days, 7)
    return weeks * 5 + sum(
        1 for offset in range(1, rest + 1) if (start + timedelta(days=offset)).weekday() < 5
    )


def fx_reason(snapshot: FxSnapshot | None, today: date) -> str | None:
    """FX-03 for one currency on ``today`` (KST): ``PRICING_FX_UNAVAILABLE`` with no snapshot (or
    one published after ``today``, which no rate known on ``today`` can be),
    ``PRICING_FX_STALE`` when more than three business days have passed since its publication,
    else ``None``."""
    if snapshot is None or snapshot.publication_date > today:
        return PRICING_FX_UNAVAILABLE
    if business_days_after(snapshot.publication_date, today) > STALE_AFTER_BUSINESS_DAYS:
        return PRICING_FX_STALE
    return None


def _require_currency(currency: str) -> str:
    if currency not in CURRENCIES:
        raise ValueError(f"no FX snapshot is kept for {currency!r}")
    return currency


def snapshot_of(row: FxRateSnapshot) -> FxSnapshot:
    return FxSnapshot(
        snapshot_id=row.snapshot_id,
        publication_date=date.fromisoformat(row.publication_date),
        currency=row.currency,
        rate_type=row.rate_type,
        source_code=row.source_code,
        unit=row.unit,
        tts=row.tts,
        deal_bas_r=row.deal_bas_r,
        source=row.source,
        fetched_at=row.fetched_at,
    )


def newest_snapshot(session: Session, currency: str, as_of: date) -> FxSnapshot | None:
    """The newest snapshot of ``currency`` published on or before ``as_of``, read inside a
    caller's unit of work: the one read a price will pin (FX-02, O2)."""
    row = session.scalars(
        select(FxRateSnapshot)
        .where(
            FxRateSnapshot.currency == _require_currency(currency),
            FxRateSnapshot.publication_date <= as_of.isoformat(),
        )
        .order_by(FxRateSnapshot.publication_date.desc())
        .limit(1)
    ).first()
    return None if row is None else snapshot_of(row)


__all__ = [
    "CURRENCIES",
    "KST",
    "PRICING_FX_STALE",
    "PRICING_FX_UNAVAILABLE",
    "STALE_AFTER_BUSINESS_DAYS",
    "FxSnapshot",
    "business_days_after",
    "fx_reason",
    "kst_day",
    "newest_snapshot",
    "snapshot_of",
]
