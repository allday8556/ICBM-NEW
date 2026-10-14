"""Persistence of the per-product landed-cost inputs of a purchase-agency source (ADR-0038 §4, §8;
migration 0066).

One table, ``overseas_cost_override_revisions``: the append-only revisions of one overseas source
product's own landed-cost inputs, keyed by ``source_product_uid`` — the overseas shipping the user
entered for it (``overseas_shipping_krw``) and its duty rate (``duty_rate_bp``, phase 1 of §8's
per-item duty). Either may be ``NULL``: the settings default then applies. The newest revision is
the product's inputs; with no revision both defaults apply, and nothing is seeded. A revision is
never updated or deleted, and its number opens at one and moves by exactly one per source product.

``app.stages.products.overseas_costs.OverseasCostOverrideStore`` is the only writer.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.platform.db.base import Base
from app.platform.db.types import UTCDateTime

# Frozen with migration 0066 (``overseas_costs`` validation states the same bounds, which are the
# overseas settings' own: ``app.capabilities.overseas.settings_models``).
OVERRIDE_CHECKS = {
    "revision_no_positive": "revision_no >= 1",
    "shipping_range": (
        "overseas_shipping_krw IS NULL OR overseas_shipping_krw BETWEEN 0 AND 10000000"
    ),
    "duty_rate_bp_range": "duty_rate_bp IS NULL OR duty_rate_bp BETWEEN 0 AND 10000",
    "fingerprint_hex": "length(fingerprint) = 64 AND fingerprint NOT GLOB '*[^0-9a-f]*'",
    "changed_by_present": "changed_by <> ''",
    "correlation_present": "correlation_id <> ''",
}


class OverseasCostOverrideRevision(Base):
    __tablename__ = "overseas_cost_override_revisions"
    __table_args__ = (
        UniqueConstraint("source_product_uid", "revision_no"),
        *(CheckConstraint(text, name=name) for name, text in OVERRIDE_CHECKS.items()),
    )

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_product_uid: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_products.source_product_uid")
    )
    revision_no: Mapped[int] = mapped_column(Integer)
    overseas_shipping_krw: Mapped[int | None] = mapped_column(Integer)
    duty_rate_bp: Mapped[int | None] = mapped_column(Integer)
    # SHA-256 of the canonical content (``overseas_costs.CONTENT_VERSION``).
    fingerprint: Mapped[str] = mapped_column(String(64))
    changed_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime)
