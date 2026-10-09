"""M6-E adopted listings (ADR-0024): SmartStore listings ICBM did not create, linked to ICBM's
canonical identity by an owner-declared seller-code convention and proven by a read-back.

**Read only** (M6E-01). The provider calls are the adopted seller-code search and origin read,
behind
:class:`~app.stages.operate.adoption_facts.ListingFinder`; nothing here writes the marketplace.

**Identity** (M6E-02, M6E-03). The convention gives the one code a source product's listing would
carry (``kmretail`` 287 → ``KM287``). Only an exact ``STOREFARM`` candidate whose origin read-back
carries the same code and is not ``DELETE`` is adopted, for a source product with exactly one
open-bound Item. Never by name. At most one ``ACTIVE`` adoption per source and per listing; an
adoption is never rewritten, it ends only as ``EXTERNALLY_REMOVED`` on provider evidence and stays
as history, and a later re-adoption is a new row proven the same way. A source product an
``ACTIVE`` ICBM registration sells is never adopted (M6E-04).
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, InputValidationError
from app.platform.db.database import Database
from app.stages.operate.adoption_facts import FOUND, RATE_LIMITED, ListingFinder
from app.stages.operate.adoption_models import AdoptedListing, AdoptedObservation

logger = logging.getLogger("icbm.operate.adoption")

ACTIVE: Final = "ACTIVE"
EXTERNALLY_REMOVED: Final = "EXTERNALLY_REMOVED"

ADOPTED: Final = "ADOPTED"
ALREADY_ADOPTED: Final = "ALREADY_ADOPTED"
REGISTERED_BY_ICBM: Final = "REGISTERED_BY_ICBM"
NOT_SINGLE_ITEM: Final = "NOT_SINGLE_ITEM"
LISTING_TAKEN: Final = "LISTING_TAKEN"
NOT_REACHED: Final = "NOT_REACHED"

# ADR-0024 §2: owner-declared seller-code conventions (Issue #219 ``6049563563``; the KM code is
# upper-case ``KM``, as the provider's listings carry it — correction ``6051294742``). A supplier
# not here has no convention, and none of its source products is ever adopted.
CONVENTIONS: Final[Mapping[str, str]] = {"kmretail": "KM{source_product_id}"}

# ICBM policy: the pause between two provider calls of one pass (as order ingest; ADR-0024 §3).
PROVIDER_PAUSE_S: Final = 1.0
ACTOR: Final = "operate.adoption"


class AdoptionBusy(AppError):
    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class AdoptedRecord:
    adoption_id: str
    marketplace_key: str
    marketplace_product_id: str
    marketplace_channel_product_id: str | None
    seller_code: str
    supplier_key: str
    source_product_id: str
    item_id: str
    state: str
    adopted_at: datetime
    adopted_by: str


@dataclass(frozen=True)
class AdoptionOutcome:
    source_product_id: str
    seller_code: str
    outcome: str
    adoption_id: str | None = None
    marketplace_product_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class AdoptionRun:
    supplier_key: str
    outcomes: tuple[AdoptionOutcome, ...]


def _record(row: AdoptedListing) -> AdoptedRecord:
    return AdoptedRecord(
        adoption_id=row.adoption_id,
        marketplace_key=row.marketplace_key,
        marketplace_product_id=row.marketplace_product_id,
        marketplace_channel_product_id=row.marketplace_channel_product_id,
        seller_code=row.seller_code,
        supplier_key=row.supplier_key,
        source_product_id=row.source_product_id,
        item_id=row.item_id,
        state=row.state,
        adopted_at=row.adopted_at,
        adopted_by=row.adopted_by,
    )


def seller_code_of(
    supplier_key: str,
    source_product_id: str,
    conventions: Mapping[str, str] = CONVENTIONS,
) -> str | None:
    """The code the supplier's convention gives this source product, or ``None``."""
    convention = conventions.get(supplier_key)
    return None if convention is None else convention.format(source_product_id=source_product_id)


class AdoptionService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        finder_factory: Callable[[Callable[[], None]], ListingFinder],
        bound_items: Callable[[str], Mapping[str, tuple[str, ...]]],
        registered_sources: Callable[[], Iterable[tuple[str, str]]],
        marketplace_key: str = "smartstore",
        conventions: Mapping[str, str] = CONVENTIONS,
        pause_s: float = PROVIDER_PAUSE_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._finder_factory = finder_factory
        self._bound_items = bound_items
        self._registered_sources = registered_sources
        self._marketplace_key = marketplace_key
        # ADR-0024 §2, ADR-0030 §3: KM통상's convention and each site's owner-declared one.
        self._conventions = dict(conventions)
        self._pause_s = pause_s
        self._sleep = sleep
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reads

    def active(self) -> tuple[AdoptedRecord, ...]:
        with self._db.read() as session:
            rows = session.scalars(
                select(AdoptedListing)
                .where(
                    AdoptedListing.marketplace_key == self._marketplace_key,
                    AdoptedListing.state == ACTIVE,
                )
                .order_by(AdoptedListing.supplier_key, AdoptedListing.source_product_id)
            ).all()
            return tuple(_record(row) for row in rows)

    def all(self) -> tuple[AdoptedRecord, ...]:
        with self._db.read() as session:
            rows = session.scalars(
                select(AdoptedListing)
                .where(AdoptedListing.marketplace_key == self._marketplace_key)
                .order_by(AdoptedListing.adopted_at)
            ).all()
            return tuple(_record(row) for row in rows)

    def active_by_product(self, marketplace_product_id: str) -> AdoptedRecord | None:
        with self._db.read() as session:
            row = session.scalars(
                select(AdoptedListing).where(
                    AdoptedListing.marketplace_key == self._marketplace_key,
                    AdoptedListing.marketplace_product_id == marketplace_product_id,
                    AdoptedListing.state == ACTIVE,
                )
            ).first()
            return None if row is None else _record(row)

    def active_by_channel(self, marketplace_channel_product_id: str) -> AdoptedRecord | None:
        with self._db.read() as session:
            row = session.scalars(
                select(AdoptedListing).where(
                    AdoptedListing.marketplace_key == self._marketplace_key,
                    AdoptedListing.marketplace_channel_product_id == marketplace_channel_product_id,
                    AdoptedListing.state == ACTIVE,
                )
            ).first()
            return None if row is None else _record(row)

    def adopted_items(self, marketplace_key: str, item_ids: Iterable[str]) -> tuple[str, ...]:
        """The ``ACTIVE`` adoptions of ``marketplace_key`` whose Item is one of ``item_ids``
        (REGISTER's duplicate source; ADR-0024 §5)."""
        wanted = set(item_ids)
        if not wanted or marketplace_key != self._marketplace_key:
            return ()
        return tuple(sorted(r.adoption_id for r in self.active() if r.item_id in wanted))

    def last_observation(self, adoption_id: str) -> AdoptedObservation | None:
        with self._db.read() as session:
            row = session.scalars(
                select(AdoptedObservation)
                .where(AdoptedObservation.adoption_id == adoption_id)
                .order_by(AdoptedObservation.observed_at.desc())
                .limit(1)
            ).first()
            if row is not None:
                session.expunge(row)
            return row

    # ------------------------------------------------------------------ the adoption pass

    def adoptable_suppliers(self) -> tuple[str, ...]:
        """The suppliers with an owner-declared seller-code convention (ADR-0024 §2)."""
        return tuple(sorted(self._conventions))

    def run(self, supplier_key: str, *, actor: str, correlation_id: str) -> AdoptionRun:
        """Try to adopt the listing of every source product of ``supplier_key`` with a single
        open-bound Item, in stable order. One pass at a time; a rate limit ends it."""
        if self._conventions.get(supplier_key) is None:
            raise InputValidationError(
                "OPERATE_ADOPTION_NO_CONVENTION",
                "the owner declared no seller-code convention for this supplier",
            )
        if not self._lock.acquire(blocking=False):
            raise AdoptionBusy("OPERATE_ADOPTION_RUNNING", "an adoption pass is already running")
        try:
            return self._run(supplier_key, actor=actor, correlation_id=correlation_id)
        finally:
            self._lock.release()

    def _run(self, supplier_key: str, *, actor: str, correlation_id: str) -> AdoptionRun:
        last_call: list[float] = []

        def pause() -> None:
            if last_call:
                waited = time.monotonic() - last_call[0]
                if waited < self._pause_s:
                    self._sleep(self._pause_s - waited)
            last_call[:] = [time.monotonic()]

        finder = self._finder_factory(pause)
        registered = set(self._registered_sources())
        active = {(r.supplier_key, r.source_product_id): r for r in self.active()}
        outcomes: list[AdoptionOutcome] = []
        stopped = False
        for source_product_id, items in self._bound_items(supplier_key).items():
            code = seller_code_of(supplier_key, source_product_id, self._conventions)
            assert code is not None
            if stopped:
                outcomes.append(AdoptionOutcome(source_product_id, code, NOT_REACHED))
                continue
            known = active.get((supplier_key, source_product_id))
            if known is not None:
                outcomes.append(
                    AdoptionOutcome(
                        source_product_id,
                        code,
                        ALREADY_ADOPTED,
                        known.adoption_id,
                        known.marketplace_product_id,
                    )
                )
                continue
            if (supplier_key, source_product_id) in registered:
                outcomes.append(AdoptionOutcome(source_product_id, code, REGISTERED_BY_ICBM))
                continue
            if len(items) != 1:
                outcomes.append(AdoptionOutcome(source_product_id, code, NOT_SINGLE_ITEM))
                continue
            found = finder.find(code)
            if found.outcome == RATE_LIMITED:
                stopped = True
            if found.outcome != FOUND or found.origin_product_no is None:
                outcomes.append(
                    AdoptionOutcome(
                        source_product_id,
                        code,
                        found.outcome,
                        marketplace_product_id=found.origin_product_no,
                        error_code=found.error_code,
                    )
                )
                continue
            outcomes.append(
                self._adopt(
                    supplier_key,
                    source_product_id,
                    items[0],
                    code,
                    found,
                    actor=actor,
                    correlation_id=correlation_id,
                )
            )
        return AdoptionRun(supplier_key, tuple(outcomes))

    def _adopt(
        self,
        supplier_key: str,
        source_product_id: str,
        item_id: str,
        code: str,
        found: Any,
        *,
        actor: str,
        correlation_id: str,
    ) -> AdoptionOutcome:
        adoption_id = str(uuid.uuid4())
        try:
            with self._db.write() as session:
                session.add(
                    AdoptedListing(
                        adoption_id=adoption_id,
                        marketplace_key=self._marketplace_key,
                        marketplace_product_id=found.origin_product_no,
                        marketplace_channel_product_id=found.channel_product_no,
                        seller_code=code,
                        convention=self._conventions[supplier_key],
                        supplier_key=supplier_key,
                        source_product_id=source_product_id,
                        item_id=item_id,
                        adopted_sale_status=found.sale_status,
                        adopted_display_status=found.display_status,
                        adopted_by=actor,
                        adopted_at=self._clock.now(),
                        correlation_id=correlation_id,
                        state=ACTIVE,
                        removed_at=None,
                        removal_evidence=None,
                    )
                )
                session.flush()
                self._audit.append(
                    AuditEntry(
                        event_type=AuditEventType.LISTING_ADOPTED,
                        action="operate.listing.adopt",
                        actor=actor,
                        outcome=AuditOutcome.RECORDED,
                        target_ref=adoption_id,
                        after={
                            "marketplace_product_id": found.origin_product_no,
                            "supplier_key": supplier_key,
                            "source_product_id": source_product_id,
                            "item_id": item_id,
                        },
                        correlation_id=correlation_id,
                    ),
                    session=session,
                )
        except IntegrityError:
            # Another ACTIVE adoption already holds this marketplace product.
            return AdoptionOutcome(
                source_product_id,
                code,
                LISTING_TAKEN,
                marketplace_product_id=found.origin_product_no,
            )
        return AdoptionOutcome(
            source_product_id, code, ADOPTED, adoption_id, found.origin_product_no
        )

    # ------------------------------------------------------------------ listing-state evidence

    def observe(
        self,
        run_id: str,
        adoption_id: str,
        result: str,
        *,
        sale_status: str | None = None,
        display_status: str | None = None,
        sale_price: int | None = None,
        stock_quantity: int | None = None,
        seller_code_matches: bool | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._db.write() as session:
            session.add(
                AdoptedObservation(
                    observation_id=str(uuid.uuid4()),
                    run_id=run_id,
                    adoption_id=adoption_id,
                    result=result,
                    sale_status=sale_status,
                    display_status=display_status,
                    sale_price=sale_price,
                    stock_quantity=stock_quantity,
                    seller_code_matches=seller_code_matches,
                    error_code=error_code,
                    observed_at=self._clock.now(),
                )
            )

    def record_removed(self, adoption_id: str, evidence: str, *, correlation_id: str) -> None:
        """Provider evidence (``statusType`` ``DELETE`` or a 404) ends an adoption (M6E-03)."""
        with self._db.write() as session:
            row = session.get(AdoptedListing, adoption_id)
            if row is None or row.state != ACTIVE:
                return
            row.state = EXTERNALLY_REMOVED
            row.removed_at = self._clock.now()
            row.removal_evidence = evidence
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.ADOPTED_LISTING_REMOVED,
                    action="operate.listing.adoption_removed",
                    actor=ACTOR,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=adoption_id,
                    reason_code=evidence,
                    correlation_id=correlation_id,
                ),
                session=session,
            )


__all__ = [
    "ACTIVE",
    "ADOPTED",
    "ALREADY_ADOPTED",
    "CONVENTIONS",
    "EXTERNALLY_REMOVED",
    "LISTING_TAKEN",
    "NOT_REACHED",
    "NOT_SINGLE_ITEM",
    "REGISTERED_BY_ICBM",
    "AdoptedRecord",
    "AdoptionBusy",
    "AdoptionOutcome",
    "AdoptionRun",
    "AdoptionService",
    "seller_code_of",
]
