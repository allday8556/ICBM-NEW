"""Persistence of the 해외 구매대행 settings (ADR-0038 §5; migration 0064).

One table, ``overseas_settings_revisions``: the append-only revisions of the one settings card.
The newest revision is the settings; with no revision the defaults apply and nothing is seeded. A
revision is never updated or deleted, and its number opens at one and moves by exactly one.

Percentages are integer **basis points** (``500`` = 5%), never a float: ``fx_margin_bp`` and
``duty_rate_bp`` are 0–10000. ``app.capabilities.overseas.settings.OverseasSettingsStore`` is the
only writer.
"""

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime

# Frozen with migration 0064 (``settings`` validation states the same bounds).
SETTINGS_CHECKS = {
    "revision_no_positive": "revision_no >= 1",
    "fx_margin_bp_range": "fx_margin_bp BETWEEN 0 AND 10000",
    "duty_rate_bp_range": "duty_rate_bp BETWEEN 0 AND 10000",
    "shipping_default_range": (
        "overseas_shipping_default_krw IS NULL"
        " OR overseas_shipping_default_krw BETWEEN 0 AND 10000000"
    ),
    "duties_flag": "duties_included_over_usd150 IN (0, 1)",
    "delivery_days_range": (
        "overseas_delivery_days_min >= 1"
        " AND overseas_delivery_days_min <= overseas_delivery_days_max"
        " AND overseas_delivery_days_max <= 60"
    ),
    "address_id_digits": (
        "smartstore_overseas_shipping_address_id IS NULL"
        " OR (length(smartstore_overseas_shipping_address_id) BETWEEN 1 AND 20"
        " AND smartstore_overseas_shipping_address_id NOT GLOB '*[^0-9]*')"
    ),
    "importer_label_length": "importer_label IS NULL OR length(importer_label) BETWEEN 1 AND 50",
    "fingerprint_hex": "length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'",
    "changed_by_present": "changed_by <> ''",
    "correlation_present": "correlation_id <> ''",
}


class OverseasSettingsRevision(Base):
    __tablename__ = "overseas_settings_revisions"
    __table_args__ = (
        UniqueConstraint("revision_no"),
        *(CheckConstraint(text, name=name) for name, text in SETTINGS_CHECKS.items()),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    fx_margin_bp: Mapped[int] = mapped_column(Integer)
    overseas_shipping_default_krw: Mapped[int | None] = mapped_column(Integer)
    duties_included_over_usd150: Mapped[bool] = mapped_column(Boolean)
    duty_rate_bp: Mapped[int] = mapped_column(Integer)
    smartstore_overseas_shipping_address_id: Mapped[str | None] = mapped_column(String(20))
    overseas_delivery_days_min: Mapped[int] = mapped_column(Integer)
    overseas_delivery_days_max: Mapped[int] = mapped_column(Integer)
    importer_label: Mapped[str | None] = mapped_column(String(50))
    # SHA-256 of the canonical content (``settings.CONTENT_VERSION``).
    fingerprint: Mapped[str] = mapped_column(String(64))
    changed_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime)
