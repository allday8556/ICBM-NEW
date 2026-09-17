from collections.abc import Sequence
from datetime import timedelta

from app import MILESTONE, __version__
from app.collect.service import CollectService
from app.connect.service import ConnectService
from app.core.clock import Clock
from app.operate.service import OperateService
from app.products.service import ProductsService
from app.register.service import RegisterService
from app.review.service import ReviewKind, ReviewService
from app.screens.contracts import (
    AnalyticsView,
    CollectView,
    DashboardView,
    DatePeriod,
    EmptyReason,
    InquiryView,
    InsightView,
    MarketplaceIdentityView,
    OrdersView,
    ProductDbView,
    RegisterView,
    ReviewCounts,
    ScreenKey,
    ScreenMeta,
    ScreenState,
    SettingsView,
    ShellView,
    SoldoutView,
)
from app.system.execution_mode import ExecutionModeService
from integrations.marketplaces.identity import MarketplaceIdentity

ANALYTICS_PERIOD_DAYS = 7


class ScreenService:
    """Assembles each top-level screen from stage-service state.

    Emptiness is decided here, from canonical counts; the UI only renders the verdict
    (CLAUDE.md §5.1: the UI displays server-owned state).
    """

    def __init__(
        self,
        *,
        clock: Clock,
        operator_name: str,
        marketplaces: Sequence[MarketplaceIdentity],
        connect: ConnectService,
        collect: CollectService,
        products: ProductsService,
        register: RegisterService,
        operate: OperateService,
        review: ReviewService,
        execution_mode: ExecutionModeService,
    ) -> None:
        self._clock = clock
        self._operator_name = operator_name
        self._marketplaces = tuple(marketplaces)
        self._connect = connect
        self._collect = collect
        self._products = products
        self._register = register
        self._operate = operate
        self._review = review
        self._execution_mode = execution_mode

    def _meta(self, screen: ScreenKey, empty_reason: EmptyReason | None) -> ScreenMeta:
        return ScreenMeta(
            screen=screen,
            state=ScreenState.EMPTY if empty_reason else ScreenState.READY,
            empty_reason=empty_reason,
            generated_at=self._clock.now(),
            milestone=MILESTONE,
        )

    def shell(self) -> ShellView:
        return ShellView(
            app_name="ICBM",
            version=__version__,
            milestone=MILESTONE,
            execution_mode=self._execution_mode.state().mode,
            operator_display_name=self._operator_name,
            server_time=self._clock.now(),
            marketplaces=[
                MarketplaceIdentityView(
                    key=m.key,
                    label=m.label,
                    wordmark=m.wordmark,
                    brand_color=m.brand_color,
                    logo_url=f"/assets/marketplaces/{m.logo_asset}" if m.logo_asset else None,
                )
                for m in self._marketplaces
            ],
        )

    def dashboard(self) -> DashboardView:
        suppliers = self._connect.connected_supplier_count()
        marketplaces = self._connect.connected_marketplace_count()
        products = self._products.product_count()
        review = self._review.open_counts()
        empty = suppliers == 0 and marketplaces == 0 and products == 0 and not any(review.values())
        return DashboardView(
            meta=self._meta(ScreenKey.DASHBOARD, EmptyReason.NO_CONNECTIONS if empty else None),
            suppliers_connected=suppliers,
            marketplaces_connected=marketplaces,
            products_total=products,
            review_counts=ReviewCounts(
                collect_evidence=review[ReviewKind.COLLECT_EVIDENCE],
                stock=review[ReviewKind.STOCK],
                source_change=review[ReviewKind.SOURCE_CHANGE],
                compliance=review[ReviewKind.COMPLIANCE],
                registration_error=review[ReviewKind.REGISTRATION_ERROR],
                fulfillment=review[ReviewKind.FULFILLMENT],
            ),
        )

    def collect(self) -> CollectView:
        jobs = self._collect.collection_job_count()
        return CollectView(
            meta=self._meta(
                ScreenKey.COLLECT, EmptyReason.NO_COLLECTION_JOBS if jobs == 0 else None
            ),
            suppliers=self._connect.supplier_connections(),
            collection_jobs_total=jobs,
        )

    def product_db(self) -> ProductDbView:
        products = self._products.product_count()
        return ProductDbView(
            meta=self._meta(ScreenKey.DB, EmptyReason.NO_PRODUCTS if products == 0 else None),
            products_total=products,
        )

    def register(self) -> RegisterView:
        candidates = self._register.registration_candidate_count()
        registrations = self._register.registration_count()
        empty = candidates == 0 and registrations == 0
        return RegisterView(
            meta=self._meta(
                ScreenKey.REGISTER, EmptyReason.NO_REGISTRATION_CANDIDATES if empty else None
            ),
            registration_candidates_total=candidates,
            registrations_total=registrations,
        )

    def orders(self) -> OrdersView:
        orders = self._operate.order_count()
        return OrdersView(
            meta=self._meta(ScreenKey.ORDERS, EmptyReason.NO_ORDERS if orders == 0 else None),
            orders_total=orders,
            marketplaces_connected=self._connect.connected_marketplace_count(),
        )

    def inquiry(self) -> InquiryView:
        inquiries = self._operate.inquiry_count()
        return InquiryView(
            meta=self._meta(
                ScreenKey.INQUIRY, EmptyReason.NO_INQUIRIES if inquiries == 0 else None
            ),
            inquiries_total=inquiries,
            marketplaces_connected=self._connect.connected_marketplace_count(),
        )

    def soldout(self) -> SoldoutView:
        items = self._review.open_counts()[ReviewKind.STOCK]
        return SoldoutView(
            meta=self._meta(
                ScreenKey.SOLDOUT, EmptyReason.NO_STOCK_REVIEW_ITEMS if items == 0 else None
            ),
            stock_review_items_total=items,
        )

    def insight(self) -> InsightView:
        products = self._products.product_count()
        orders = self._operate.order_count()
        empty = products == 0 and orders == 0
        return InsightView(
            meta=self._meta(
                ScreenKey.AI_INSIGHT, EmptyReason.NO_INTERNAL_HISTORY if empty else None
            ),
            products_total=products,
            orders_total=orders,
        )

    def analytics(self) -> AnalyticsView:
        today = self._clock.now().astimezone().date()
        orders = self._operate.order_count()
        registrations = self._register.registration_count()
        empty = orders == 0 and registrations == 0
        return AnalyticsView(
            meta=self._meta(ScreenKey.ANALYTICS, EmptyReason.NO_OPERATING_DATA if empty else None),
            period=DatePeriod(
                start=today - timedelta(days=ANALYTICS_PERIOD_DAYS - 1),
                end=today,
                days=ANALYTICS_PERIOD_DAYS,
            ),
            orders_total=orders,
            registrations_total=registrations,
        )

    def settings(self) -> SettingsView:
        policy_values: dict[str, str | int | float | bool | None] = {}
        return SettingsView(
            meta=self._meta(
                ScreenKey.SETTINGS, EmptyReason.NO_SETTINGS_SAVED if not policy_values else None
            ),
            execution_mode=self._execution_mode.state().mode,
            editable=False,
            marketplace_connections=self._connect.marketplace_connections(),
            supplier_connections=self._connect.supplier_connections(),
            policy_values=policy_values,
        )
