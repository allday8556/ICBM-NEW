"""OPERATE routes (M6, ADR-0023; M6.5, ADR-0025). Nothing here writes to a supplier: the
supplier order is placed by hand and only recorded. The one marketplace write, 발송처리, runs only
through the DISPATCH stage of the send-time safety stack (ADR-0025 §5)."""

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import new_correlation_id
from app.platform.core.errors import InputValidationError, PolicyBlockedError
from app.stages.operate.catalog.service import OPERATOR as CATALOG_OPERATOR
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
        # ADR-0030 §11 S2: each supplier whose listings can be adopted, KM통상 and any site.
        "adoptable_suppliers": [
            {"supplier_key": key, "display_name": container.supplier_names.get(key, key)}
            for key in container.adoptions.adoptable_suppliers()
        ],
    }


@router.post("/listings/sync")
async def sync_listings(container: ContainerDep) -> dict[str, Any]:
    """The operator's 지금 동기화: the same read-only pass the periodic job runs, now."""
    run = await asyncio.to_thread(
        container.listing_sync.sync, trigger=OPERATOR, correlation_id=new_correlation_id()
    )
    return asdict(run)


@router.get("/store-catalog")
def store_catalog(container: ContainerDep) -> dict[str, Any]:
    """The connected store's catalog as the last syncs saw it (ADR-0042 §5.4): the running sync
    and its progress, the last sync whatever its outcome, the last COMPLETE sync (「마지막
    동기화」, SCP-07) and every listing with its derived state and latest read fields."""
    status = container.store_catalog.status()
    return {
        "status": asdict(status),
        "listings": [asdict(listing) for listing in container.store_catalog.listings()],
    }


@router.get("/store-catalog/shape")
def store_catalog_shape(container: ContainerDep) -> dict[str, Any]:
    """「문제 해결용 정보」 (ADR-0042 §5.1 item 3), read only: the response shape the latest
    operator 「지금 동기화」 recorded — key paths, JSON types and enum tokens, never a value — for
    the search pages and the origin reads. ``shape`` is ``null`` before any such sync."""
    shape = container.store_catalog.shape()
    if shape is None:
        return {"shape": None}
    return {
        "shape": {
            "run_id": shape.run_id,
            "as_of": shape.as_of,
            "outcome": shape.outcome,
            "path_count": shape.path_count,
            "search": shape.search,
            "origin_read": shape.origin_read,
        }
    }


@router.post("/store-catalog/sync", status_code=202)
def sync_store_catalog(container: ContainerDep) -> dict[str, Any]:
    """「지금 동기화」: start one read-only catalog sync in the background. A store that is not
    connected is refused; a sync already running is the one the operator asked for."""
    if container.store_catalog.account() is None:
        raise PolicyBlockedError(
            "STORE_CATALOG_STORE_NOT_CONNECTED",
            "connect and bind the store before its catalog can be synced",
        )
    started = container.store_catalog_scheduler.request(trigger=CATALOG_OPERATOR)
    return {"started": started, "status": asdict(container.store_catalog.status())}


# ---------------------------------------------------------------- ADR-0042 S2a: listing links


class StoreLinkConfirmRequest(BaseModel):
    """「같은 상품」: a listing (by id, or by its store and product number) is this product.
    ``survivor_group_id`` is the product kept when two become one (default: this product)."""

    product_group_id: str = Field(min_length=1, max_length=36)
    listing_id: str | None = Field(default=None, min_length=1, max_length=36)
    marketplace_key: str | None = Field(default=None, min_length=1, max_length=40)
    marketplace_account_id: str | None = Field(default=None, min_length=1, max_length=40)
    origin_product_no: str | None = Field(default=None, min_length=1, max_length=20)
    survivor_group_id: str | None = Field(default=None, min_length=1, max_length=36)
    actor: str = Field(min_length=2, max_length=64)


class StoreLinkRejectRequest(BaseModel):
    """「다른 상품」: this listing is not that product. Revocable."""

    listing_id: str = Field(min_length=1, max_length=36)
    product_group_id: str = Field(min_length=1, max_length=36)
    actor: str = Field(min_length=2, max_length=64)


class StoreLinkActorRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=64)


class StoreLinkUnlinkRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=64)
    # 「되돌리기」 of an automatic link's notice, rather than 「연결 해제」 of the product.
    undo: bool = False


class StoreLinkAcknowledgeRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=64)
    # None: every open notice (「확인」 on the notice).
    notice_ids: list[str] | None = Field(default=None, max_length=500)


@router.get("/store-links")
def store_links(container: ContainerDep) -> dict[str, Any]:
    """What the 통합DB shows after an import (ADR-0042 §0 b3): the automatic links not yet
    acknowledged, the 「같은 상품인가요?」 proposals and the 「다른 상품」 decisions, with the last
    complete sync time (SCP-07). A read; nothing is linked here."""
    return asdict(container.store_links.overview())


@router.get("/store-links/products")
def store_links_of_products(
    container: ContainerDep, group_id: Annotated[list[str] | None, Query()] = None
) -> dict[str, Any]:
    """The store side of the 통합DB rows: each product's listings and whether it has a supplier
    (「도매처 미연결」 when it has none)."""
    groups = list(group_id or ())[:50]
    return {"products": [asdict(view) for view in container.store_links.for_groups(groups)]}


@router.post("/store-links/confirm")
def confirm_store_link(container: ContainerDep, request: StoreLinkConfirmRequest) -> dict[str, Any]:
    links = container.store_links
    listing_id = request.listing_id
    if listing_id is None:
        if not (
            request.marketplace_key and request.marketplace_account_id and request.origin_product_no
        ):
            raise InputValidationError(
                "STORE_LINK_LISTING_REQUIRED", "name the listing by id or by store and product"
            )
        listing_id = links.listing_id_of(
            request.marketplace_key, request.marketplace_account_id, request.origin_product_no
        )
    result = links.confirm(
        listing_id,
        request.product_group_id,
        survivor_group_id=request.survivor_group_id,
        actor=request.actor,
        correlation_id=new_correlation_id(),
    )
    return asdict(result)


@router.post("/store-links/reject")
def reject_store_link(container: ContainerDep, request: StoreLinkRejectRequest) -> dict[str, Any]:
    result = container.store_links.reject(
        request.listing_id,
        request.product_group_id,
        actor=request.actor,
        correlation_id=new_correlation_id(),
    )
    return asdict(result)


@router.post("/store-links/decisions/{decision_id}/revoke")
def revoke_store_link_decision(
    container: ContainerDep, decision_id: str, request: StoreLinkActorRequest
) -> dict[str, Any]:
    container.store_links.revoke_decision(
        decision_id, actor=request.actor, correlation_id=new_correlation_id()
    )
    return {"decision_id": decision_id, "revoked": True}


@router.post("/store-links/notices/acknowledge")
def acknowledge_store_link_notices(
    container: ContainerDep, request: StoreLinkAcknowledgeRequest
) -> dict[str, Any]:
    count = container.store_links.acknowledge(
        request.notice_ids, actor=request.actor, correlation_id=new_correlation_id()
    )
    return {"acknowledged": count}


@router.post("/store-links/{link_id}/unlink")
def unlink_store_listing(
    container: ContainerDep, link_id: str, request: StoreLinkUnlinkRequest
) -> dict[str, Any]:
    result = container.store_links.unlink(
        link_id, actor=request.actor, correlation_id=new_correlation_id(), undo=request.undo
    )
    return asdict(result)


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
    states = container.fulfillment.states(order.product_order_id for order in overview.orders)
    return {
        "capability": overview.capability,
        "attested": overview.attested,
        "interval_s": overview.interval_s,
        "synced_until": overview.synced_until,
        "total": overview.total,
        "last_run": None if overview.last_run is None else asdict(overview.last_run),
        "orders": [
            asdict(order) | {"fulfillment_state": states.get(order.product_order_id)}
            for order in overview.orders
        ],
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


class SupplierOrderRequest(BaseModel):
    supplier_order_ref: str = Field(min_length=1, max_length=100)
    purchase_amount: int = Field(ge=0)
    ordered_at: datetime | None = None
    # The revision the operator read; None only for the first record.
    expected_revision: int | None = None


class TrackingRequest(BaseModel):
    carrier_code: str = Field(min_length=1, max_length=40)
    tracking_number: str = Field(min_length=1, max_length=50)
    expected_revision: int


@router.get("/carriers")
def carriers(container: ContainerDep) -> dict[str, Any]:
    """The documented delivery-company codes a tracking number may name (ADR-0025 §4)."""
    return {
        "carriers": [
            {"code": code, "name": name} for code, name in container.fulfillment.carriers().items()
        ]
    }


@router.get("/orders/{product_order_id}/fulfillment")
def order_fulfillment(product_order_id: str, container: ContainerDep) -> dict[str, Any]:
    """One order's fulfillment: its canonical identity, supplier order, tracking and history, and
    its dispatch attempts (ADR-0025 §5)."""
    return asdict(container.fulfillment.view(product_order_id)) | {
        "dispatch_attempts": [
            asdict(attempt) for attempt in container.dispatches.attempts(product_order_id)
        ]
    }


@router.post("/orders/{product_order_id}/dispatch")
async def dispatch_order(product_order_id: str, container: ContainerDep) -> dict[str, Any]:
    """발송처리: one dispatch through the send-time safety stack (ADR-0025 §5). Without LIVE, the
    released brake, the exact DISPATCH grant and every other layer it is refused and nothing is
    sent; the refusal names every layer that refused."""
    attempt = await asyncio.to_thread(
        container.dispatches.dispatch,
        product_order_id,
        actor="operator",
        correlation_id=new_correlation_id(),
    )
    return asdict(attempt)


@router.post("/orders/{product_order_id}/dispatch/verify")
async def verify_dispatch(product_order_id: str, container: ContainerDep) -> dict[str, Any]:
    """Read the order back and record what it shows about its latest sent dispatch, once."""
    attempt = await asyncio.to_thread(
        container.dispatches.verify,
        product_order_id,
        actor="operator",
        correlation_id=new_correlation_id(),
    )
    return {"attempt": None if attempt is None else asdict(attempt)}


@router.put("/orders/{product_order_id}/supplier-order")
def record_supplier_order(
    product_order_id: str, request: SupplierOrderRequest, container: ContainerDep
) -> dict[str, Any]:
    """Record, or amend, the supplier order the operator placed by hand (ADR-0025 §3)."""
    view = container.fulfillment.record_supplier_order(
        product_order_id,
        supplier_order_ref=request.supplier_order_ref,
        purchase_amount=request.purchase_amount,
        ordered_at=request.ordered_at,
        expected_revision=request.expected_revision,
        actor="operator",
        correlation_id=new_correlation_id(),
    )
    return asdict(view)


@router.put("/orders/{product_order_id}/tracking")
def capture_tracking(
    product_order_id: str, request: TrackingRequest, container: ContainerDep
) -> dict[str, Any]:
    """Capture, or amend, the carrier and tracking number the operator typed in (ADR-0025 §4)."""
    view = container.fulfillment.capture_tracking(
        product_order_id,
        carrier_code=request.carrier_code,
        tracking_number=request.tracking_number,
        expected_revision=request.expected_revision,
        actor="operator",
        correlation_id=new_correlation_id(),
    )
    return asdict(view)


class AdoptionRunRequest(BaseModel):
    supplier_key: str = Field(min_length=1, max_length=40)


@router.get("/adoptions")
def adoptions(container: ContainerDep) -> dict[str, Any]:
    """The SmartStore listings ICBM did not create but adopted (ADR-0024)."""
    return {"adoptions": [asdict(record) for record in container.adoptions.all()]}


@router.post("/adoptions/run")
async def run_adoptions(request: AdoptionRunRequest, container: ContainerDep) -> dict[str, Any]:
    """One adoption pass over a supplier's source products by its owner-declared seller-code
    convention (ADR-0024 §3). Read-only towards the marketplace."""
    run = await asyncio.to_thread(
        container.adoptions.run,
        request.supplier_key,
        actor="operator",
        correlation_id=new_correlation_id(),
    )
    return asdict(run)
