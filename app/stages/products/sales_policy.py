"""The per-supplier sales policy: whether ICBM enforces a supplier's own sales policy (ADR-0039).

One policy per ``supplier_key``, two switches (ADR-0039 §1):

- ``apply_channel_restrictions`` (ADR-0031 §4): when false, a forbidden or unread sales-channel
  statement is a non-blocking warning at REGISTER instead of a refusal
  (``app.stages.products.source_gates``);
- ``apply_minimum_price`` (ADR-0030 §2, ADR-0013 §7): when false, pricing does not use the source's
  minimum resale price; the snapshot records that it did not, and the minimum the source stated
  (``app.stages.products.pricing``).

Both default to **apply** (SP-02): a supplier with no revision is read as the defaults, and nothing
is seeded. The switches change enforcement only, never a fact (SP-01): the sales-channel and
minimum-price fields are read exactly as before.

**Revisions are append-only and server-created.** A save appends one revision — its identity, its
number, the fingerprint of its canonical content and its audit event — in one unit of work, or
changes nothing. It names the revision it was edited from (``expected_revision_no``, ``None`` for a
supplier with none); a save against a moved policy is refused, never merged, exactly as the
registration target policy's save contract refuses one (``TARGET_POLICY_CURRENT_MOVED``). A save
identical to the current policy is refused too: the history only records changes.

**Pins** (SP-03). :meth:`SalesPolicy.pin` is what a registration preparation pins: the supplier,
the revision number and its fingerprint. A supplier with no revision pins nothing, so every
preparation and snapshot fingerprint taken before this owner existed is unchanged.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import AppError, ErrorClass, NotFoundError
from app.platform.db.database import Database
from app.stages.products.sales_policy_models import SupplierSalesPolicyRevision

CONTENT_VERSION: Final = "supplier-sales-policy/v1"
# The Settings surface name the server lists as editable (``EditableSurface``).
SALES_POLICY_SURFACE: Final = "SUPPLIER_SALES_POLICY"

SALES_POLICY_SUPPLIER_UNKNOWN: Final = "SALES_POLICY_SUPPLIER_UNKNOWN"
SALES_POLICY_REVISION_MOVED: Final = "SALES_POLICY_REVISION_MOVED"
SALES_POLICY_UNCHANGED: Final = "SALES_POLICY_UNCHANGED"


class SalesPolicyConflictError(AppError):
    """A save the current policy refuses: it moved since it was read, or nothing changes."""

    error_class = ErrorClass.CONFLICT


@dataclass(frozen=True)
class SalesPolicy:
    """One supplier's sales policy as it holds now. ``revision_no`` is ``None`` for a supplier
    with no revision: the defaults, both applied."""

    supplier_key: str
    apply_channel_restrictions: bool = True
    apply_minimum_price: bool = True
    revision_id: str | None = None
    revision_no: int | None = None
    fingerprint: str | None = None
    changed_by: str | None = None
    changed_at: datetime | None = None

    def pin(self) -> tuple[str, int, str] | None:
        """What a preparation pins (SP-03): the supplier, its revision number and fingerprint.
        ``None`` for the defaults, so a supplier never configured changes no fingerprint."""
        if self.revision_no is None or self.fingerprint is None:
            return None
        return (self.supplier_key, self.revision_no, self.fingerprint)


def content_fingerprint(
    supplier_key: str, apply_channel_restrictions: bool, apply_minimum_price: bool
) -> str:
    """SHA-256 of the canonical content of one revision."""
    content = {
        "content_version": CONTENT_VERSION,
        "supplier_key": supplier_key,
        "apply_channel_restrictions": apply_channel_restrictions,
        "apply_minimum_price": apply_minimum_price,
    }
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _newest(session: Session, supplier_key: str) -> SupplierSalesPolicyRevision | None:
    return session.scalars(
        select(SupplierSalesPolicyRevision)
        .where(SupplierSalesPolicyRevision.supplier_key == supplier_key)
        .order_by(SupplierSalesPolicyRevision.revision_no.desc())
        .limit(1)
    ).first()


def _policy(supplier_key: str, row: SupplierSalesPolicyRevision | None) -> SalesPolicy:
    if row is None:
        return SalesPolicy(supplier_key)
    return SalesPolicy(
        supplier_key=supplier_key,
        apply_channel_restrictions=bool(row.apply_channel_restrictions),
        apply_minimum_price=bool(row.apply_minimum_price),
        revision_id=row.revision_id,
        revision_no=row.revision_no,
        fingerprint=row.fingerprint,
        changed_by=row.changed_by,
        changed_at=row.changed_at,
    )


def sales_policy_of(session: Session, supplier_key: str) -> SalesPolicy:
    """The supplier's policy now, read inside a caller's unit of work: its newest revision, or the
    defaults when it has none. The one read every enforcing owner uses."""
    return _policy(supplier_key, _newest(session, supplier_key))


class SalesPolicyStore:
    """The only writer of ``supplier_sales_policy_revisions``."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def policy(self, supplier_key: str) -> SalesPolicy:
        with self._db.read() as session:
            return sales_policy_of(session, supplier_key)

    def append(
        self,
        supplier_key: str,
        *,
        apply_channel_restrictions: bool,
        apply_minimum_price: bool,
        expected_revision_no: int | None,
        changed_by: str,
        correlation_id: str,
    ) -> SalesPolicy:
        """Append one revision and make it the supplier's policy, with its audit event, in one
        unit of work — or change nothing."""
        now = self._clock.now()
        with self._db.write() as session:
            current = _newest(session, supplier_key)
            if (None if current is None else current.revision_no) != expected_revision_no:
                raise SalesPolicyConflictError(
                    SALES_POLICY_REVISION_MOVED,
                    "the sales policy changed since it was read; reload it before saving",
                    details={
                        "current_revision_no": None if current is None else current.revision_no
                    },
                )
            before = _policy(supplier_key, current)
            if (
                before.apply_channel_restrictions == apply_channel_restrictions
                and before.apply_minimum_price == apply_minimum_price
            ):
                raise SalesPolicyConflictError(
                    SALES_POLICY_UNCHANGED, "the saved sales policy is identical to the current one"
                )
            fingerprint = content_fingerprint(
                supplier_key, apply_channel_restrictions, apply_minimum_price
            )
            row = SupplierSalesPolicyRevision(
                revision_id=str(uuid.uuid4()),
                supplier_key=supplier_key,
                revision_no=1 if current is None else current.revision_no + 1,
                apply_channel_restrictions=apply_channel_restrictions,
                apply_minimum_price=apply_minimum_price,
                fingerprint=fingerprint,
                changed_by=changed_by,
                correlation_id=correlation_id,
                changed_at=now,
            )
            session.add(row)
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.SUPPLIER_SALES_POLICY_REVISED,
                    action="SUPPLIER_SALES_POLICY_REVISION_APPENDED",
                    actor=changed_by,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=supplier_key,
                    before={
                        "revision_no": before.revision_no,
                        "apply_channel_restrictions": before.apply_channel_restrictions,
                        "apply_minimum_price": before.apply_minimum_price,
                    },
                    after={
                        "revision_id": row.revision_id,
                        "revision_no": row.revision_no,
                        "apply_channel_restrictions": apply_channel_restrictions,
                        "apply_minimum_price": apply_minimum_price,
                        "fingerprint": fingerprint,
                    },
                    details={"content_version": CONTENT_VERSION},
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return _policy(supplier_key, row)


# ------------------------------------------------------------------ the application contract


class SalesPolicyView(BaseModel):
    """One supplier's sales policy as the server holds it. ``editable`` is the server's answer;
    ``revision_no`` is ``None`` while the supplier uses the defaults."""

    supplier_key: str
    surface: str
    editable: bool
    apply_channel_restrictions: bool
    apply_minimum_price: bool
    revision_no: int | None
    fingerprint: str | None
    changed_by: str | None
    changed_at: datetime | None


class SaveSalesPolicyRequest(BaseModel):
    """One save. ``expected_revision_no`` is the revision the operator edited from (``null`` for a
    supplier with none). Both switches are required: nothing is defaulted by the server."""

    model_config = ConfigDict(extra="forbid")

    expected_revision_no: StrictInt | None
    apply_channel_restrictions: StrictBool
    apply_minimum_price: StrictBool


class SalesPolicyService:
    """What the supplier card reads and saves for one supplier's sales policy, and nothing else."""

    def __init__(
        self, store: SalesPolicyStore, known_suppliers: Callable[[], Iterable[str]]
    ) -> None:
        self._store = store
        # The suppliers CONNECT and COLLECT are configured for: the supplier cards' keys.
        self._known = known_suppliers

    def policy(self, supplier_key: str) -> SalesPolicyView:
        self._require_known(supplier_key)
        return _view(self._store.policy(supplier_key))

    def save(
        self,
        supplier_key: str,
        request: SaveSalesPolicyRequest,
        *,
        actor: str,
        correlation_id: str,
    ) -> SalesPolicyView:
        self._require_known(supplier_key)
        return _view(
            self._store.append(
                supplier_key,
                apply_channel_restrictions=request.apply_channel_restrictions,
                apply_minimum_price=request.apply_minimum_price,
                expected_revision_no=request.expected_revision_no,
                changed_by=actor,
                correlation_id=correlation_id,
            )
        )

    def _require_known(self, supplier_key: str) -> None:
        if supplier_key not in set(self._known()):
            raise NotFoundError(
                SALES_POLICY_SUPPLIER_UNKNOWN, "a sales policy belongs to a configured supplier"
            )


def _view(policy: SalesPolicy) -> SalesPolicyView:
    return SalesPolicyView(
        supplier_key=policy.supplier_key,
        surface=SALES_POLICY_SURFACE,
        editable=True,
        apply_channel_restrictions=policy.apply_channel_restrictions,
        apply_minimum_price=policy.apply_minimum_price,
        revision_no=policy.revision_no,
        fingerprint=policy.fingerprint,
        changed_by=policy.changed_by,
        changed_at=policy.changed_at,
    )
