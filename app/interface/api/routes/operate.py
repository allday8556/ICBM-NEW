"""OPERATE routes (M6, ADR-0023). Read-only towards the marketplace: nothing here writes it."""

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import new_correlation_id
from app.stages.operate.listing import OPERATOR
from app.stages.operate.orders import OPERATOR as ORDER_OPERATOR
from app.stages.operate.stock import OPERATOR as STOCK_OPERATOR

router = APIRouter(prefix="/api/v1/operate", tags=["operate"])


@router.get("/listings")
def listings(container: ContainerDep) -> dict[str, Any]:
    """Every SmartStore registration's operated state: the last listing observation, the drift the
    server sees and whether ICBM itself deleted it (ADR-0023 §3, §8)."""
    overview = container.listing_sync.overview()
    return {
        "interval_s": overview.interval_s,
        "last_run": None if overview.last_run is None else asdict(overview.last_run),
        "listings": [asdict(listing) for listing in overview.listings],
    }


@router.post("/listings/sync")
async def sync_listings(container: ContainerDep) -> dict[str, Any]:
    """The operator's 지금 동기화: the same read-only pass the periodic job runs, now."""
    run = await asyncio.to_thread(
        container.listing_sync.sync, trigger=OPERATOR, correlation_id=new_correlation_id()
    )
    return asdict(run)


@router.get("/stock")
def stock(container: ContainerDep) -> dict[str, Any]:
    """The listed source products' supplier stock and their last recheck (ADR-0023 §4, §8)."""
    overview = container.stock_recheck.overview()
    return {
        "interval_s": overview.interval_s,
        "cap": overview.cap,
        "sources": [asdict(source) for source in overview.sources],
    }


@router.post("/stock/recheck")
async def recheck_stock(container: ContainerDep) -> dict[str, Any]:
    """The operator's 지금 재확인: settle what finished, then ask COLLECT to re-collect now."""
    settled = await asyncio.to_thread(container.stock_recheck.settle)
    opened = await asyncio.to_thread(container.stock_recheck.request_round, trigger=STOCK_OPERATOR)
    return {"settled": settled, "requested": len(opened)}


@router.get("/orders")
def orders(container: ContainerDep) -> dict[str, Any]:
    """The ingested product orders with their resolution and masked recipient (ADR-0023 §8)."""
    overview = container.order_sync.overview()
    return {
        "capability": overview.capability,
        "interval_s": overview.interval_s,
        "synced_until": overview.synced_until,
        "total": overview.total,
        "last_run": None if overview.last_run is None else asdict(overview.last_run),
        "orders": [asdict(order) for order in overview.orders],
    }


@router.post("/orders/sync")
async def sync_orders(container: ContainerDep) -> dict[str, Any]:
    """The operator's 지금 동기화 of orders: the same pass the schedule runs."""
    run = await asyncio.to_thread(
        container.order_sync.sync, trigger=ORDER_OPERATOR, correlation_id=new_correlation_id()
    )
    return asdict(run)


@router.get("/orders/{product_order_id}/shipping")
def order_shipping(product_order_id: str, container: ContainerDep) -> JSONResponse:
    """One order's shipping record, opened for the detail view only (ADR-0023 §7). Audited by
    id, and never cached."""
    record = container.order_sync.shipping(
        product_order_id, actor="operator", correlation_id=new_correlation_id()
    )
    return JSONResponse(
        {
            "product_order_id": product_order_id,
            "recipient_name": record.recipient_name,
            "phone1": record.phone1,
            "phone2": record.phone2,
            "base_address": record.base_address,
            "detail_address": record.detail_address,
            "zip_code": record.zip_code,
            "memo": record.memo,
        },
        headers={"Cache-Control": "no-store"},
    )
