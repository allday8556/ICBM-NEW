from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class SupplierConnectionSummary(BaseModel):
    """Read shape of ROADMAP §4.1 ``SupplierConnection``. Persistence begins in M1."""

    supplier_key: str
    base_url: str
    auth_required: bool
    auth_state: str
    session_state: str
    profile_state: str
    last_verified_at: datetime | None


class MarketplaceConnectionState(StrEnum):
    # Further states (verified, failed, ...) are defined with SmartStore CONNECT in M2.
    NOT_CONNECTED = "NOT_CONNECTED"


class MarketplaceConnectionSummary(BaseModel):
    marketplace_key: str
    connection_state: MarketplaceConnectionState
