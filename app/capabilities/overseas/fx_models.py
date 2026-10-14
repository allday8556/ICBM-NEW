"""Persistence of the FX snapshot owner (ADR-0038 §3; migration 0064).

One table, ``fx_snapshots``: one immutable row per publication day and currency of Korea
Eximbank's 현재환율 (``koreaexim/AP01``), rate type TTS (송금 보낼때). A row is never updated or
deleted, and a day holds at most one row per currency.

- ``publication_date`` is the day the publication was asked for and answered (ISO
  ``YYYY-MM-DD``, KST): the API's rows carry no date of their own.
- ``tts`` is exact decimal text **per ``unit``** of the currency, exactly as published, with the
  thousands separators removed: ``JPY(100)`` is recorded with ``unit = 100``, so its ``tts`` is the
  won price of 100 yen. A reader divides by ``unit`` (``FxSnapshot.tts_per_unit``); nothing is
  divided, rounded or converted to a float in storage.
- ``source_code`` (``CNH``, ``JPY(100)``, ``USD``), ``deal_bas_r`` and ``published_json`` (the
  whole row) are kept as fetched.

``app.capabilities.overseas.fx.FxSnapshotStore`` is the only writer.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime

# Frozen with migration 0064: each wanted currency, its published code and publication unit.
CURRENCY_CODES = (
    "(currency = 'CNY' AND source_code = 'CNH' AND unit = 1)"
    " OR (currency = 'JPY' AND source_code = 'JPY(100)' AND unit = 100)"
    " OR (currency = 'USD' AND source_code = 'USD' AND unit = 1)"
)
ISO_DAY = "publication_date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]'"
EXACT_DECIMAL = (
    "tts <> '' AND tts NOT GLOB '*[^0-9.]*' AND tts NOT GLOB '*.*.*'"
    " AND tts NOT GLOB '.*' AND tts NOT GLOB '*.' AND CAST(tts AS REAL) > 0"
)


class FxRateSnapshot(Base):
    __tablename__ = "fx_snapshots"
    __table_args__ = (
        UniqueConstraint("publication_date", "currency"),
        CheckConstraint(ISO_DAY, name="publication_date_iso"),
        CheckConstraint(CURRENCY_CODES, name="currency_code_unit"),
        CheckConstraint("rate_type = 'TTS'", name="rate_type_tts"),
        CheckConstraint(EXACT_DECIMAL, name="tts_exact_decimal"),
        CheckConstraint("deal_bas_r <> ''", name="deal_bas_r_present"),
        CheckConstraint("json_valid(published_json)", name="published_json_valid"),
        CheckConstraint("source = 'koreaexim/AP01'", name="source_known"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    publication_date: Mapped[str] = mapped_column(String(10))
    currency: Mapped[str] = mapped_column(String(3))
    rate_type: Mapped[str] = mapped_column(String(8))
    source_code: Mapped[str] = mapped_column(String(16))
    unit: Mapped[int] = mapped_column(Integer)
    tts: Mapped[str] = mapped_column(String(32))
    deal_bas_r: Mapped[str] = mapped_column(String(32))
    published_json: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    correlation_id: Mapped[str] = mapped_column(String(64))
