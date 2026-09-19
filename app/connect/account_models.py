"""Persistence of the canonical seller and marketplace-account identity (Issue #89 PR-B).

Canonical v3.1 §9 freezes ``SellerEntity`` and ``MarketplaceAccount(seller_entity_id NOT NULL)``;
``ACCOUNT_IDENTITY.md`` §2 names the ICBM-owned identifier ``marketplace_account_id`` and reserves
``account_id`` for the NAVER OAuth/token wire parameter. Registration state is scoped by this
identity (ADR-0014; review 5255746944, blocker 2), never by a free caller string.

- ``seller_entities``: the seller behind one or more marketplace accounts.
- ``marketplace_accounts``: one canonical account, established only from the committed M2 binding
  unit of its marketplace connection (ACCOUNT_IDENTITY §5). ``provider_account_uid`` is the strong
  provider identity that binding proved (NAVER ``accountUid``); a provider display name is never
  part of the identity (§6). One provider identity has one canonical account per marketplace, so
  no alias can open a second scope for the same account.

Both tables are append-only: migration 0016 rejects UPDATE and DELETE, and an account is inserted
only while its connection's committed binding names the same provider identity.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime

# ``mpa-`` and 32 lowercase hex digits: minted by ICBM, never a provider value or a store name.
ACCOUNT_ID_FORMAT = (
    "length(marketplace_account_id) = 36 AND substr(marketplace_account_id, 1, 4) = 'mpa-'"
    " AND substr(marketplace_account_id, 5) NOT GLOB '*[^0-9a-f]*'"
)


class SellerEntity(Base):
    __tablename__ = "seller_entities"
    __table_args__ = (
        CheckConstraint("created_by <> ''", name="created_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    seller_entity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class MarketplaceAccount(Base):
    __tablename__ = "marketplace_accounts"
    __table_args__ = (
        # The target of every registration row's (marketplace_key, marketplace_account_id) key.
        UniqueConstraint("marketplace_key", "marketplace_account_id"),
        # No alias: one canonical account per provider identity in a marketplace.
        UniqueConstraint("marketplace_key", "provider_account_uid"),
        CheckConstraint(ACCOUNT_ID_FORMAT, name="account_id_format"),
        CheckConstraint("provider_account_uid <> ''", name="provider_identity_present"),
        CheckConstraint("established_by <> ''", name="established_by_present"),
        CheckConstraint("correlation_id <> ''", name="correlation_present"),
    )

    marketplace_account_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    seller_entity_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("seller_entities.seller_entity_id")
    )
    marketplace_key: Mapped[str] = mapped_column(
        String(40), ForeignKey("marketplace_connections.marketplace_key")
    )
    provider_account_uid: Mapped[str] = mapped_column(String(100))
    established_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    established_at: Mapped[datetime] = mapped_column(UTCDateTime)
