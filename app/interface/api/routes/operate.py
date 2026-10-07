"""OPERATE routes (M6, ADR-0023). Read-only towards the marketplace: nothing here writes it."""

import asyncio
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter

from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import new_correlation_id
from app.stages.operate.listing import OPERATOR

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
