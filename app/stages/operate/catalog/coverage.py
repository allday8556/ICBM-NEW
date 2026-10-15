"""The store-catalog coverage confirmation (ADR-0042 §5.2, S3b tightening; seller decides).

Whether the paging-only enumeration lists **every** sale status is not captured (E1 per status:
``NOT_CAPTURED``): a whole status class could be missing from a ``COMPLETE`` snapshot, and a
``NO_MATCH`` against it would then be fail-open. So a snapshot is duplicate ``NO_MATCH`` evidence
only after the seller has confirmed, once per store, in 통합DB › 「스토어 상품」, that the catalog's
per-status counts equal the seller center's (「판매자센터 상품 수와 같습니다」).

**The record** is one append-only audit event (``STORE_CATALOG_COVERAGE_CONFIRMED``) per
confirmation: who, when, the sync run the seller looked at, its total and its per-status counts.
The latest one is the store's confirmation; nothing is ever rewritten.

**The rule that invalidates it** (ICBM policy, stated here and in ADR-0042 §5.2): the confirmation
stops holding for a later COMPLETE sync when

- a status class the seller confirmed with at least one listing has none any more (a class may
  have gone missing from the enumeration), or
- the total moved by more than ``max(10, 20 %)`` of the confirmed total (an unexpected jump).

Then the seller confirms again. A positive match never needs the confirmation: only ``NO_MATCH``
does.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.errors import AppError, ErrorClass, PolicyBlockedError
from app.stages.operate.catalog.service import (
    LISTED,
    CatalogListingView,
    StoreCatalogService,
    listing_filter,
)

CONFIRMED: Final = "CONFIRMED"
UNCONFIRMED: Final = "UNCONFIRMED"
INVALIDATED: Final = "INVALIDATED"
NO_SNAPSHOT: Final = "NO_SNAPSHOT"

# Why a confirmation stopped holding.
STATUS_CLASS_MISSING: Final = "STATUS_CLASS_MISSING"
TOTAL_JUMPED: Final = "TOTAL_JUMPED"

# ICBM policy: how far the total may move before the seller is asked again.
TOTAL_JUMP_MIN: Final = 10
TOTAL_JUMP_SHARE: Final = 0.2

RUN_MOVED: Final = "STORE_CATALOG_COVERAGE_RUN_MOVED"
NOTHING_TO_CONFIRM: Final = "STORE_CATALOG_COVERAGE_NO_SNAPSHOT"


class CoverageConflict(AppError):
    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class CoverageCounts:
    """What one COMPLETE sync listed: its total and its count per status filter."""

    run_id: str
    as_of: datetime
    total: int
    counts: Mapping[str, int]


@dataclass(frozen=True)
class CoverageConfirmation:
    event_id: str
    confirmed_at: datetime
    confirmed_by: str
    run_id: str
    total: int
    counts: Mapping[str, int]


@dataclass(frozen=True)
class CoverageView:
    state: str
    current: CoverageCounts | None
    confirmed: CoverageConfirmation | None
    reason: str | None = None


def counts_of(listings: Iterable[CatalogListingView]) -> dict[str, int]:
    """The per-status counts of the listings one sync listed (as 「스토어 상품」 files them)."""
    found: dict[str, int] = {}
    for view in listings:
        if view.state != LISTED:
            continue
        key = listing_filter(view)
        found[key] = found.get(key, 0) + 1
    return dict(sorted(found.items()))


def judge(
    confirmed: CoverageConfirmation | None, total: int, counts: Mapping[str, int]
) -> tuple[str, str | None]:
    """Whether a confirmation still holds for a sync with this total and these counts."""
    if confirmed is None:
        return UNCONFIRMED, None
    if any(n > 0 and counts.get(key, 0) == 0 for key, n in confirmed.counts.items()):
        return INVALIDATED, STATUS_CLASS_MISSING
    allowed = max(TOTAL_JUMP_MIN, int(confirmed.total * TOTAL_JUMP_SHARE))
    if abs(total - confirmed.total) > allowed:
        return INVALIDATED, TOTAL_JUMPED
    return CONFIRMED, None


class CatalogCoverage:
    """The seller's coverage confirmation of the bound store, recorded in the audit log."""

    def __init__(self, *, catalog: StoreCatalogService, audit: AuditLog) -> None:
        self._catalog = catalog
        self._audit = audit

    def _target(self, account: str) -> str:
        return f"store-catalog:{self._catalog.marketplace_key}:{account}"

    def confirmation(self, account: str) -> CoverageConfirmation | None:
        event = self._audit.latest(
            AuditEventType.STORE_CATALOG_COVERAGE_CONFIRMED.value, self._target(account)
        )
        if event is None:
            return None
        details = event.details
        counts = details.get("counts")
        return CoverageConfirmation(
            event_id=event.event_id,
            confirmed_at=event.occurred_at,
            confirmed_by=event.actor,
            run_id=str(details.get("run_id")),
            total=int(details.get("total", 0)),
            counts={str(k): int(v) for k, v in (counts or {}).items()},
        )

    def state(self, account: str, total: int, counts: Mapping[str, int]) -> str:
        """``CONFIRMED`` only when the store's latest confirmation holds for these counts."""
        return judge(self.confirmation(account), total, counts)[0]

    def current(self) -> CoverageCounts | None:
        snapshot = self._catalog.snapshot()
        if snapshot.run is None:
            return None
        counts = counts_of(snapshot.listings)
        return CoverageCounts(snapshot.run.run_id, snapshot.run.as_of, sum(counts.values()), counts)

    def view(self) -> CoverageView:
        """What 「스토어 상품」 shows beside the seller-center hint."""
        account = self._catalog.account()
        if account is None:
            return CoverageView(NO_SNAPSHOT, None, None)
        current = self.current()
        confirmed = self.confirmation(account)
        if current is None:
            return CoverageView(NO_SNAPSHOT, None, confirmed)
        state, reason = judge(confirmed, current.total, current.counts)
        return CoverageView(state, current, confirmed, reason)

    def confirm(self, *, run_id: str, actor: str, correlation_id: str) -> CoverageView:
        """「판매자센터 상품 수와 같습니다」 for exactly the sync the seller looked at."""
        account = self._catalog.account()
        if account is None:
            raise PolicyBlockedError(
                "STORE_CATALOG_STORE_NOT_CONNECTED",
                "connect and bind the store before its catalog can be confirmed",
            )
        current = self.current()
        if current is None:
            raise CoverageConflict(NOTHING_TO_CONFIRM, "no complete store sync to confirm yet")
        if current.run_id != run_id:
            raise CoverageConflict(
                RUN_MOVED,
                "a newer store sync finished; look at its counts and confirm again",
                details={"run_id": current.run_id},
            )
        self._audit.append(
            AuditEntry(
                event_type=AuditEventType.STORE_CATALOG_COVERAGE_CONFIRMED,
                action="operate.store_catalog.confirm_coverage",
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=self._target(account),
                details={
                    "marketplace_key": self._catalog.marketplace_key,
                    "marketplace_account_id": account,
                    "run_id": current.run_id,
                    "as_of": current.as_of.isoformat(),
                    "total": current.total,
                    "counts": dict(current.counts),
                },
                correlation_id=correlation_id,
            )
        )
        return self.view()


__all__ = [
    "CONFIRMED",
    "INVALIDATED",
    "NO_SNAPSHOT",
    "STATUS_CLASS_MISSING",
    "TOTAL_JUMPED",
    "UNCONFIRMED",
    "CatalogCoverage",
    "CoverageConfirmation",
    "CoverageCounts",
    "CoverageView",
    "counts_of",
    "judge",
]
