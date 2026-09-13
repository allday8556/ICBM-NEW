from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.screens.contracts import (
    AnalyticsView,
    CollectView,
    DashboardView,
    InquiryView,
    InsightView,
    OrdersView,
    ProductDbView,
    RegisterView,
    SettingsView,
    ShellView,
    SoldoutView,
)

router = APIRouter(tags=["screens"])


@router.get("/api/v1/shell")
def shell(container: ContainerDep) -> ShellView:
    return container.screens.shell()


@router.get("/api/v1/screens/dashboard")
def dashboard(container: ContainerDep) -> DashboardView:
    return container.screens.dashboard()


@router.get("/api/v1/screens/collect")
def collect(container: ContainerDep) -> CollectView:
    return container.screens.collect()


@router.get("/api/v1/screens/db")
def product_db(container: ContainerDep) -> ProductDbView:
    return container.screens.product_db()


@router.get("/api/v1/screens/register")
def register(container: ContainerDep) -> RegisterView:
    return container.screens.register()


@router.get("/api/v1/screens/orders")
def orders(container: ContainerDep) -> OrdersView:
    return container.screens.orders()


@router.get("/api/v1/screens/inquiry")
def inquiry(container: ContainerDep) -> InquiryView:
    return container.screens.inquiry()


@router.get("/api/v1/screens/soldout")
def soldout(container: ContainerDep) -> SoldoutView:
    return container.screens.soldout()


@router.get("/api/v1/screens/ai-insight")
def ai_insight(container: ContainerDep) -> InsightView:
    return container.screens.insight()


@router.get("/api/v1/screens/analytics")
def analytics(container: ContainerDep) -> AnalyticsView:
    return container.screens.analytics()


@router.get("/api/v1/screens/settings")
def settings(container: ContainerDep) -> SettingsView:
    return container.screens.settings()
