"""Persistence of the SmartStore connection (M2 PR-A).

One row per marketplace account ICBM connects (SmartStore's own-store account in M2; the
marketplace key is ICBM's account identifier, never a provider display name). It holds:

* the credential and session generation high-water marks, so every committed credential bundle
  and every committed token session gets a new, never-reused generation even when a secret-store
  record or a session file is lost (AUTH.md §13);
* the account binding unit (ACCOUNT_IDENTITY.md §5): the provider identity with the generations
  of the fresh read it was bound from, when and by whom. A CHECK constraint admits a complete
  unit or none, so a partial binding can never be stored and ``binding_commit_not_proven ->
  NOT_BOUND`` holds by construction.

No secret, token or client_id is stored here.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import UTCDateTime

BINDING_IS_COMPLETE_OR_ABSENT = (
    "(provider_account_uid IS NULL AND provider_account_id IS NULL"
    " AND bound_credential_generation IS NULL AND bound_session_generation IS NULL"
    " AND bound_at IS NULL AND bound_by IS NULL)"
    " OR (provider_account_uid IS NOT NULL AND provider_account_uid <> ''"
    " AND bound_credential_generation IS NOT NULL AND bound_credential_generation >= 1"
    " AND bound_session_generation IS NOT NULL AND bound_session_generation >= 1"
    " AND bound_at IS NOT NULL AND bound_by IS NOT NULL AND bound_by <> '')"
)


class MarketplaceConnection(Base):
    __tablename__ = "marketplace_connections"
    __table_args__ = (
        CheckConstraint("credential_generation_hwm >= 0", name="credential_hwm_non_negative"),
        CheckConstraint("session_generation_hwm >= 0", name="session_hwm_non_negative"),
        CheckConstraint(BINDING_IS_COMPLETE_OR_ABSENT, name="binding_complete_or_absent"),
    )

    marketplace_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    credential_generation_hwm: Mapped[int] = mapped_column(Integer)
    session_generation_hwm: Mapped[int] = mapped_column(Integer)
    # NAVER accountUid (primary identity) and accountId (secondary, when available).
    provider_account_uid: Mapped[str | None] = mapped_column(String(100))
    provider_account_id: Mapped[str | None] = mapped_column(String(100))
    bound_credential_generation: Mapped[int | None] = mapped_column(Integer)
    bound_session_generation: Mapped[int | None] = mapped_column(Integer)
    bound_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    bound_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
