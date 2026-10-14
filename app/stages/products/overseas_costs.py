"""The per-product landed-cost inputs of a purchase-agency source (ADR-0038 §4, §8 phase 1).

Two inputs the user enters for one overseas source product, each overriding a settings default
(``app.capabilities.overseas.settings``):

- ``overseas_shipping_krw``: the overseas shipping of one unit — the supplier's local delivery, the
  freight forwarder and the international freight (§4). Without one, the settings'
  ``overseas_shipping_default_krw`` applies; without either, pricing is
  ``PRICING_OVERSEAS_SHIPPING_UNSET``.
- ``duty_rate_bp``: the duty rate in basis points (§8 phase 1's per-product override). Without one,
  the settings' ``duty_rate_bp`` applies.

**Keyed by the source product** (``source_product_uid``). The landed cost is a property of buying
from one overseas source: its weight to the freight forwarder, its local delivery and its customs
classification. Every Item and AtomicSKU Item that source procures shares it, and so it is
entered once — per option it would have to be repeated for each AtomicSKU Item, and per Product it
would be wrong for a Product whose members mix a domestic and an overseas source. The pricing
owner reads it through the procurement's member (its source product) in its own unit of work.

**Revisions are append-only and server-created**, exactly like the supplier sales policy
(ADR-0039 §1) and the overseas settings: a save names the revision it was edited from
(``expected_revision_no``, ``None`` while none exists); a save against a moved revision is refused
(``OVERSEAS_COSTS_REVISION_MOVED``), never merged, and one identical to the current inputs is
refused too (``OVERSEAS_COSTS_UNCHANGED``). One revision, its fingerprint and its audit event are
written in one unit of work, or nothing is.

**Pins** (FX-02). A foreign price pins the revision number it read (``None`` while none exists),
so a change of either input makes the current pricing snapshot STALE through the pricing
dependency fingerprint, and a KRW price never reads it at all.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.capabilities.overseas.settings import (
    MAX_SHIPPING_DEFAULT_KRW,
    Percent,
    bp_to_percent,
    percent_to_bp,
    valid_percent,
)
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, NotFoundError
from app.platform.db.database import Database
from app.stages.products.models import SourceProduct
from app.stages.products.overseas_costs_models import OverseasCostOverrideRevision

CONTENT_VERSION: Final = "overseas-cost-override/v1"

OVERSEAS_COSTS_SOURCE_UNKNOWN: Final = "OVERSEAS_COSTS_SOURCE_UNKNOWN"
OVERSEAS_COSTS_REVISION_MOVED: Final = "OVERSEAS_COSTS_REVISION_MOVED"
OVERSEAS_COSTS_UNCHANGED: Final = "OVERSEAS_COSTS_UNCHANGED"


class OverseasCostsConflictError(AppError):
    """A save the current inputs refuse: they moved since they were read, or nothing changes."""

    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class OverseasCostOverride:
    """One source product's landed-cost inputs as they hold now. Each value is ``None`` when the
    settings default applies; ``revision_no`` is ``None`` while the product has no revision."""

    source_product_uid: str
    overseas_shipping_krw: int | None = None
    duty_rate_bp: int | None = None
    revision_id: str | None = None
    revision_no: int | None = None
    fingerprint: str | None = None
    changed_by: str | None = None
    changed_at: datetime | None = None


def content_fingerprint(
    source_product_uid: str, overseas_shipping_krw: int | None, duty_rate_bp: int | None
) -> str:
    """SHA-256 of the canonical content of one revision."""
    content = {
        "content_version": CONTENT_VERSION,
        "source_product_uid": source_product_uid,
        "overseas_shipping_krw": overseas_shipping_krw,
        "duty_rate_bp": duty_rate_bp,
    }
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _newest(session: Session, source_product_uid: str) -> OverseasCostOverrideRevision | None:
    return session.scalars(
        select(OverseasCostOverrideRevision)
        .where(OverseasCostOverrideRevision.source_product_uid == source_product_uid)
        .order_by(OverseasCostOverrideRevision.revision_no.desc())
        .limit(1)
    ).first()


def _override(
    source_product_uid: str, row: OverseasCostOverrideRevision | None
) -> OverseasCostOverride:
    if row is None:
        return OverseasCostOverride(source_product_uid)
    return OverseasCostOverride(
        source_product_uid=source_product_uid,
        overseas_shipping_krw=row.overseas_shipping_krw,
        duty_rate_bp=row.duty_rate_bp,
        revision_id=row.revision_id,
        revision_no=row.revision_no,
        fingerprint=row.fingerprint,
        changed_by=row.changed_by,
        changed_at=row.changed_at,
    )


def overseas_cost_override_of(session: Session, source_product_uid: str) -> OverseasCostOverride:
    """The source product's inputs now, read inside a caller's unit of work: its newest revision,
    or no override when it has none. The one read the pricing owner uses."""
    return _override(source_product_uid, _newest(session, source_product_uid))


class OverseasCostOverrideStore:
    """The only writer of ``overseas_cost_override_revisions``."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def current(self, supplier_key: str, source_product_id: str) -> OverseasCostOverride:
        with self._db.read() as session:
            return overseas_cost_override_of(
                session, _source_uid(session, supplier_key, source_product_id)
            )

    def append(
        self,
        supplier_key: str,
        source_product_id: str,
        *,
        overseas_shipping_krw: int | None,
        duty_rate_bp: int | None,
        expected_revision_no: int | None,
        changed_by: str,
        correlation_id: str,
    ) -> OverseasCostOverride:
        """Append one revision stating both inputs, with its audit event, in one unit of work —
        or change nothing."""
        now = self._clock.now()
        with self._db.write() as session:
            uid = _source_uid(session, supplier_key, source_product_id)
            current = _newest(session, uid)
            if (None if current is None else current.revision_no) != expected_revision_no:
                raise OverseasCostsConflictError(
                    OVERSEAS_COSTS_REVISION_MOVED,
                    "the product's overseas cost inputs changed since they were read; reload them"
                    " before saving",
                    details={
                        "current_revision_no": None if current is None else current.revision_no
                    },
                )
            before = _override(uid, current)
            if (before.overseas_shipping_krw, before.duty_rate_bp) == (
                overseas_shipping_krw,
                duty_rate_bp,
            ):
                raise OverseasCostsConflictError(
                    OVERSEAS_COSTS_UNCHANGED,
                    "the saved overseas cost inputs are identical to the current ones",
                )
            fingerprint = content_fingerprint(uid, overseas_shipping_krw, duty_rate_bp)
            row = OverseasCostOverrideRevision(
                revision_id=str(uuid.uuid4()),
                source_product_uid=uid,
                revision_no=1 if current is None else current.revision_no + 1,
                overseas_shipping_krw=overseas_shipping_krw,
                duty_rate_bp=duty_rate_bp,
                fingerprint=fingerprint,
                changed_by=changed_by,
                correlation_id=correlation_id,
                changed_at=now,
            )
            session.add(row)
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.OVERSEAS_COST_OVERRIDE_REVISED,
                    action="OVERSEAS_COST_OVERRIDE_REVISION_APPENDED",
                    actor=changed_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=uid,
                    before={
                        "revision_no": before.revision_no,
                        "overseas_shipping_krw": before.overseas_shipping_krw,
                        "duty_rate_bp": before.duty_rate_bp,
                    },
                    after={
                        "revision_id": row.revision_id,
                        "revision_no": row.revision_no,
                        "overseas_shipping_krw": overseas_shipping_krw,
                        "duty_rate_bp": duty_rate_bp,
                        "fingerprint": fingerprint,
                    },
                    details={
                        "content_version": CONTENT_VERSION,
                        "supplier_key": supplier_key,
                        "source_product_id": source_product_id,
                    },
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return _override(uid, row)


def _source_uid(session: Session, supplier_key: str, source_product_id: str) -> str:
    """The identity of a source product the Product DB already holds; never created here."""
    uid = session.scalar(
        select(SourceProduct.source_product_uid).where(
            SourceProduct.supplier_key == supplier_key,
            SourceProduct.source_product_id == source_product_id,
        )
    )
    if uid is None:
        raise NotFoundError(
            OVERSEAS_COSTS_SOURCE_UNKNOWN, "overseas cost inputs belong to a known source product"
        )
    return uid


# ------------------------------------------------------------------ the application contract


class OverseasCostsView(BaseModel):
    """One source product's inputs as the server holds them. A ``null`` value means the settings
    default applies; ``revision_no`` is ``None`` while the product has no revision."""

    supplier_key: str
    source_product_id: str
    overseas_shipping_krw: int | None
    duty_rate_percent: str | None
    revision_no: int | None
    fingerprint: str | None
    changed_by: str | None
    changed_at: datetime | None


class SaveOverseasCostsRequest(BaseModel):
    """One save. Both inputs are required (``null`` = use the settings default): nothing is
    defaulted by the server. ``expected_revision_no`` is the revision the operator edited from."""

    model_config = ConfigDict(extra="forbid")

    expected_revision_no: StrictInt | None
    overseas_shipping_krw: Annotated[StrictInt, Field(ge=0, le=MAX_SHIPPING_DEFAULT_KRW)] | None
    duty_rate_percent: Percent | None

    @field_validator("duty_rate_percent")
    @classmethod
    def _valid_percent(cls, value: str | None) -> str | None:
        return None if value is None else valid_percent(value)


class OverseasCostsService:
    """What the overseas cost input of one source product reads and saves, and nothing else."""

    def __init__(self, store: OverseasCostOverrideStore) -> None:
        self._store = store

    def current(self, supplier_key: str, source_product_id: str) -> OverseasCostsView:
        return _view(
            supplier_key, source_product_id, self._store.current(supplier_key, source_product_id)
        )

    def save(
        self,
        supplier_key: str,
        source_product_id: str,
        request: SaveOverseasCostsRequest,
        *,
        actor: str,
        correlation_id: str,
    ) -> OverseasCostsView:
        saved = self._store.append(
            supplier_key,
            source_product_id,
            overseas_shipping_krw=request.overseas_shipping_krw,
            duty_rate_bp=(
                None
                if request.duty_rate_percent is None
                else percent_to_bp(request.duty_rate_percent)
            ),
            expected_revision_no=request.expected_revision_no,
            changed_by=actor,
            correlation_id=correlation_id,
        )
        return _view(supplier_key, source_product_id, saved)


def _view(
    supplier_key: str, source_product_id: str, override: OverseasCostOverride
) -> OverseasCostsView:
    return OverseasCostsView(
        supplier_key=supplier_key,
        source_product_id=source_product_id,
        overseas_shipping_krw=override.overseas_shipping_krw,
        duty_rate_percent=(
            None if override.duty_rate_bp is None else bp_to_percent(override.duty_rate_bp)
        ),
        revision_no=override.revision_no,
        fingerprint=override.fingerprint,
        changed_by=override.changed_by,
        changed_at=override.changed_at,
    )
