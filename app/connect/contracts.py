from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app.connect.state import CapabilityStatus, ConnectionState


class SupplierConnectionSummary(BaseModel):
    """Read shape of ROADMAP §4.1 ``SupplierConnection`` (Issue #7).

    ``state`` is the single source of truth. ``auth_state`` and ``session_state`` are the
    canonical field names rendered as views of that one state machine, never stored separately.
    Counters are safe diagnostics; no secret or session content is ever part of this contract.
    """

    connection_id: str | None
    supplier_key: str
    display_name: str
    base_url: str
    auth_required: bool
    state: ConnectionState
    auth_state: str
    session_state: str
    # Collection profiles arrive with COLLECT (M3); CONNECT does not analyse the site.
    profile_state: str
    capability_status: CapabilityStatus
    credentials_stored: bool
    auto_connect: bool
    last_verified_at: datetime | None
    consecutive_auth_failures: int
    auth_retry_limit: int
    real_login_attempts: int
    session_reuse_count: int
    reauth_count: int
    last_login_attempt_at: datetime | None
    last_error_class: str | None
    last_error_code: str | None


class StoredLoginView(BaseModel):
    """What the operator's loopback credential form may show (Issue #7 addendum 5654634584).

    The login ID is read from the OS secret store on demand and is never persisted anywhere
    else. The password is never returned — only whether one is stored.
    """

    username: str | None
    password_stored: bool


class CapabilityReport(BaseModel):
    """One capability in readiness, e.g. ``supplier:kmretail``. Never affects core readiness."""

    key: str
    status: CapabilityStatus
    detail: str


class MarketplaceConnectionState(StrEnum):
    # Further states (verified, failed, ...) are defined with SmartStore CONNECT in M2.
    NOT_CONNECTED = "NOT_CONNECTED"


class MarketplaceConnectionSummary(BaseModel):
    marketplace_key: str
    connection_state: MarketplaceConnectionState
