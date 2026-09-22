"""View contracts consumed by the UI shell.

Fields are limited to state that canonical documents already define (SupplierConnection,
MarketplaceRegistration, ReviewItem kinds, Job, execution mode). Row-level list contracts for
products, orders, etc. are added with the milestone that first produces such rows, after
architect schema review — M0 does not invent them.
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel

from app.connect.contracts import MarketplaceConnectionSummary, SupplierConnectionSummary
from app.core.execution import ExecutionMode
from app.register.target_policy import EditableSurface


class ScreenKey(StrEnum):
    DASHBOARD = "dashboard"
    COLLECT = "collect"
    DB = "db"
    REGISTER = "register"
    ORDERS = "orders"
    INQUIRY = "inquiry"
    SOLDOUT = "soldout"
    AI_INSIGHT = "ai-insight"
    ANALYTICS = "analytics"
    SETTINGS = "settings"


class ScreenState(StrEnum):
    EMPTY = "EMPTY"
    READY = "READY"


class EmptyReason(StrEnum):
    NO_CONNECTIONS = "NO_CONNECTIONS"
    NO_COLLECTION_JOBS = "NO_COLLECTION_JOBS"
    NO_PRODUCTS = "NO_PRODUCTS"
    NO_REGISTRATION_CANDIDATES = "NO_REGISTRATION_CANDIDATES"
    NO_ORDERS = "NO_ORDERS"
    NO_INQUIRIES = "NO_INQUIRIES"
    NO_STOCK_REVIEW_ITEMS = "NO_STOCK_REVIEW_ITEMS"
    NO_INTERNAL_HISTORY = "NO_INTERNAL_HISTORY"
    NO_OPERATING_DATA = "NO_OPERATING_DATA"
    NO_SETTINGS_SAVED = "NO_SETTINGS_SAVED"


class ScreenMeta(BaseModel):
    screen: ScreenKey
    state: ScreenState
    empty_reason: EmptyReason | None
    generated_at: datetime
    milestone: str


class ReviewCounts(BaseModel):
    """Open ReviewItem counts per kind (ARCHITECTURE.md §9: surfaced as dashboard counts)."""

    collect_evidence: int
    stock: int
    source_change: int
    compliance: int
    registration_error: int
    fulfillment: int


class DashboardView(BaseModel):
    meta: ScreenMeta
    suppliers_connected: int
    marketplaces_connected: int
    products_total: int
    review_counts: ReviewCounts


class CollectView(BaseModel):
    meta: ScreenMeta
    suppliers: list[SupplierConnectionSummary]
    collection_jobs_total: int


class ProductDbView(BaseModel):
    meta: ScreenMeta
    products_total: int


class RegisterView(BaseModel):
    meta: ScreenMeta
    registration_candidates_total: int
    registrations_total: int


class OrdersView(BaseModel):
    meta: ScreenMeta
    orders_total: int
    marketplaces_connected: int


class InquiryView(BaseModel):
    meta: ScreenMeta
    inquiries_total: int
    marketplaces_connected: int


class SoldoutView(BaseModel):
    meta: ScreenMeta
    stock_review_items_total: int


class InsightView(BaseModel):
    meta: ScreenMeta
    products_total: int
    orders_total: int


class DatePeriod(BaseModel):
    start: date
    end: date
    days: int


class AnalyticsView(BaseModel):
    meta: ScreenMeta
    period: DatePeriod
    orders_total: int
    registrations_total: int


SettingValue = str | int | float | bool | None


class SettingsView(BaseModel):
    meta: ScreenMeta
    execution_mode: ExecutionMode
    # Whether the general common/platform settings accept a save. They have no persistence
    # contract, so this stays false; ``editable_surfaces`` names the surfaces that do.
    editable: bool
    # The server-owned scope of what Settings can save (ADR-0015 §2): only the registration
    # target policy, through its own contract. The UI renders this and decides nothing.
    editable_surfaces: list[EditableSurface]
    marketplace_connections: list[MarketplaceConnectionSummary]
    supplier_connections: list[SupplierConnectionSummary]
    # Saved common/platform policy values keyed by setting id. Empty until a settings
    # persistence contract exists; the UI shows every field as unset.
    policy_values: dict[str, SettingValue]


class MarketplaceIdentityView(BaseModel):
    key: str
    label: str
    brand_color: str
    logo_url: str | None


class ShellView(BaseModel):
    app_name: str
    version: str
    milestone: str
    execution_mode: ExecutionMode
    operator_display_name: str
    server_time: datetime
    marketplaces: list[MarketplaceIdentityView]
