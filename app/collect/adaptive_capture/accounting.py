"""The Phase C send accounting owner (Issue #110 C1 PREP-0 `5841947773`).

An ordinary operator collection becomes Phase-C-accounted only when its frozen capture request
binds it to a campaign that registered its read ceilings for the stage. ``bind`` runs right after
the run's first-reservation unit and before any send:

- a run frozen ``OFF``, or first read before the capture seam, is not accounted: ``None``, and it
  runs exactly as before;
- a run frozen ``REQUESTED`` whose request is unreadable, names another supplier or target, or
  whose campaign registered no ceilings is refused before any send (fail-closed).

For an accounted run it answers a ``SendGuard`` for this one attempt. The collection sets it, and
every guarded send point calls ``reserve`` immediately before it transmits:

- ``PRODUCT_READ`` and ``IMAGE_REQUEST`` at the collection transport's request budget;
- ``CONNECT_CONTROL_READ`` and ``CONNECT_PROTECTED_READ`` at the CONNECT fetch;
- ``CONNECT_AUTHENTICATE`` before any login. Its ceiling is zero, so it is always refused.

``reserve`` commits one read row in its own write unit and only then returns, so the send happens
only after its reservation is durable. The database admits the row only under the campaign's
frozen ceiling (``CAMPAIGN`` over the stage, ``ATTEMPT`` over the run's attempt), and only for a run
bound to that campaign. A refused or failed reservation records a refusal and raises; nothing is
sent. A retry, a restart and a second run of the campaign all consume the same ceilings.

The existing per-image (2 MiB) and per-attempt new-byte (24 MiB) limits of the collection profile
remain the underlying byte enforcement; this owner counts sends and never replaces them.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select

from app.collect.adaptive_capture.models import (
    READ_CLASSES,
    CaptureRequest,
    PhaseCRead,
    PhaseCReadBudget,
    PhaseCReadRefusal,
)
from app.collect.adaptive_capture.store import target_digest
from app.collect.shadow import FrozenCapture
from app.core.clock import Clock
from app.core.errors import PolicyBlockedError
from app.db.database import Database

logger = logging.getLogger(__name__)

PHASE_C_SEND_REFUSED = "PHASE_C_SEND_REFUSED"
# The stage a capture request's collections are accounted in: C1 captures the samples.
CAPTURE_STAGE = "C1"


class PhaseCSendRefused(PolicyBlockedError):
    """A Phase C send refused before transmission."""


def _send_refused(reason: str, message: str) -> PhaseCSendRefused:
    return PhaseCSendRefused(PHASE_C_SEND_REFUSED, message, details={"reason": reason})


def _digest(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Budget:
    """One class's frozen ceiling and what it bounds."""

    ceiling: int
    scope: str


@dataclass(frozen=True)
class Binding:
    campaign_id: str
    stage: str
    collection_run_id: str
    attempt_no: int


class PhaseCReadAccount:
    """The ``SendGuard`` of one Phase-C-accounted collection attempt."""

    def __init__(self, owner: "PhaseCReadAccounting", binding: Binding) -> None:
        self._owner = owner
        self.binding = binding

    def reserve(self, request_class: str, subject: str) -> None:
        self._owner.reserve(self.binding, request_class, subject)


class PhaseCReadAccounting:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # -------------------------------------------------------------- budgets

    def budget(self, campaign_id: str, stage: str) -> dict[str, Budget]:
        with self._db.read() as session:
            rows = session.scalars(
                select(PhaseCReadBudget).where(
                    PhaseCReadBudget.campaign_id == campaign_id, PhaseCReadBudget.stage == stage
                )
            ).all()
            return {row.request_class: Budget(row.ceiling, row.scope) for row in rows}

    # -------------------------------------------------------------- the collection's seam

    def bind(
        self,
        *,
        collection_run_id: str,
        supplier_key: str,
        target: str,
        capture: FrozenCapture | None,
        attempt_no: int,
    ) -> PhaseCReadAccount | None:
        if capture is None or capture.decision != "REQUESTED":
            return None
        try:
            with self._db.read() as session:
                request = session.get(CaptureRequest, capture.request_id)
                if request is None:
                    raise _send_refused("UNBOUND", "the run's capture request cannot be read")
                if request.supplier_key != supplier_key or request.target_digest != target_digest(
                    supplier_key, target
                ):
                    raise _send_refused(
                        "TARGET_MISMATCH", "the run's capture request names another target"
                    )
                classes = session.scalar(
                    select(func.count())
                    .select_from(PhaseCReadBudget)
                    .where(
                        PhaseCReadBudget.campaign_id == request.campaign_id,
                        PhaseCReadBudget.stage == CAPTURE_STAGE,
                    )
                )
                if classes != len(READ_CLASSES):
                    raise _send_refused(
                        "NOT_BUDGETED", "the run's campaign registered no Phase C read ceilings"
                    )
                campaign_id = request.campaign_id
        except PhaseCSendRefused:
            raise
        except Exception:
            raise _send_refused(
                "OWNER_UNREADABLE", "the Phase C accounting owner cannot be read"
            ) from None
        return PhaseCReadAccount(
            self, Binding(campaign_id, CAPTURE_STAGE, collection_run_id, attempt_no)
        )

    # -------------------------------------------------------------- one send

    def reserve(self, binding: Binding, request_class: str, subject: str) -> None:
        """Durably reserve one send, committed before this returns, or refuse it."""
        if request_class not in READ_CLASSES:
            self._refusal(binding, request_class, "UNKNOWN_CLASS")
            raise _send_refused("UNKNOWN_CLASS", f"{request_class} is not a Phase C request class")
        try:
            # Committed with synchronous=FULL: the reservation survives even a power loss before the
            # send it permits.
            with self._db.write(durable=True) as session:
                budget = session.get(
                    PhaseCReadBudget, (binding.campaign_id, binding.stage, request_class)
                )
                if budget is None:
                    reason = "NOT_BUDGETED"
                else:
                    counted = (
                        select(func.count())
                        .select_from(PhaseCRead)
                        .where(
                            PhaseCRead.campaign_id == binding.campaign_id,
                            PhaseCRead.stage == binding.stage,
                            PhaseCRead.request_class == request_class,
                        )
                    )
                    if budget.scope == "ATTEMPT":
                        counted = counted.where(
                            PhaseCRead.collection_run_id == binding.collection_run_id,
                            PhaseCRead.attempt_no == binding.attempt_no,
                        )
                    used = session.scalar(counted) or 0
                    reason = "CEILING" if used >= budget.ceiling else ""
                if not reason:
                    # The database trigger admits the row again, under the same ceiling.
                    session.add(
                        PhaseCRead(
                            read_id=str(uuid.uuid4()),
                            campaign_id=binding.campaign_id,
                            stage=binding.stage,
                            request_class=request_class,
                            collection_run_id=binding.collection_run_id,
                            attempt_no=binding.attempt_no,
                            subject_digest=_digest(subject),
                            reserved_at=self._clock.now(),
                        )
                    )
        except Exception:
            reason = "OWNER_REFUSED"
        if reason:
            self._refusal(binding, request_class, reason)
            raise _send_refused(reason, f"Phase C refused a {request_class} before it was sent")

    def _refusal(self, binding: Binding, request_class: str, reason: str) -> None:
        try:
            with self._db.write() as session:
                session.add(
                    PhaseCReadRefusal(
                        refusal_id=str(uuid.uuid4()),
                        campaign_id=binding.campaign_id,
                        stage=binding.stage,
                        request_class=request_class[:24],
                        collection_run_id=binding.collection_run_id,
                        attempt_no=binding.attempt_no,
                        reason=reason,
                        refused_at=self._clock.now(),
                    )
                )
        except Exception:
            # The send is refused either way; a refusal that cannot even be recorded is logged.
            logger.warning(
                "phase_c.refusal_unrecorded",
                extra={"campaign_id": binding.campaign_id, "reason": reason},
            )

    # -------------------------------------------------------------- reads

    def counts(self, campaign_id: str, stage: str) -> dict[str, int]:
        """The sends reserved per class over the whole stage."""
        with self._db.read() as session:
            rows = session.execute(
                select(PhaseCRead.request_class, func.count())
                .where(PhaseCRead.campaign_id == campaign_id, PhaseCRead.stage == stage)
                .group_by(PhaseCRead.request_class)
            ).all()
        found = {name: int(count) for name, count in rows}
        return {name: found.get(name, 0) for name in READ_CLASSES}

    def refusals(self, campaign_id: str) -> list[dict[str, object]]:
        with self._db.read() as session:
            rows = session.scalars(
                select(PhaseCReadRefusal)
                .where(PhaseCReadRefusal.campaign_id == campaign_id)
                .order_by(PhaseCReadRefusal.refused_at, PhaseCReadRefusal.refusal_id)
            ).all()
            return [
                {
                    "request_class": r.request_class,
                    "collection_run_id": r.collection_run_id,
                    "attempt_no": r.attempt_no,
                    "reason": r.reason,
                }
                for r in rows
            ]
