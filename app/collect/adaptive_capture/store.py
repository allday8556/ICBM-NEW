"""The capture owner: requests, the reservation-time freeze, candidates and sample finalization.

- ``request`` is the Phase C harness's only way to ask for a capture (a repository rule pins the
  caller). A request names one campaign, one supplier and one target digest, and lapses after at
  most 24 hours; it is consumed by at most one run (a unique index on the run record).
- ``freeze`` is the run store's ``CaptureFreezer``: read-only, inside the first-reservation unit.
- ``record`` / ``refuse`` keep what the capture step produced for a requested run, in the capture
  owner's own write unit, after the canonical revision committed.
- ``finalize`` cuts a new immutable ValidationSample from a candidate with the operator's scope and
  expected facts and stores it through the P2 owner. A correction is a new sample.
- Every candidate belongs to the campaign whose request its run consumed. ``finalize`` refuses a
  candidate of any other campaign (review ``5313663701`` B3): a campaign never finalizes, and so
  never validates, another campaign's capture.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.collect.adaptive.canonical import digest
from app.collect.adaptive.capture import (
    CaptureCandidate,
    CaptureRefused,
    OperatorScope,
    ValidationSample,
    candidate_digest,
    sample_from_candidate,
)
from app.collect.adaptive_capture.models import (
    BUDGET_SCOPES,
    READ_CLASSES,
    REQUEST_MAX_HOURS,
    CaptureCandidateRecord,
    CaptureRequest,
    PhaseCReadBudget,
)
from app.collect.adaptive_store.gate import SupplierGate
from app.collect.adaptive_store.store import AdaptiveValidationStore
from app.collect.models import CollectionRun, ProductFactsRevision
from app.collect.shadow import CAPTURE_OFF, FrozenCapture
from app.core.clock import Clock
from app.core.errors import AppError, ErrorClass, InputValidationError, NotFoundError
from app.db.database import Database

ADAPTIVE_CAPTURE_REFUSED = "ADAPTIVE_CAPTURE_REFUSED"
ADAPTIVE_CAPTURE_NOT_FOUND = "ADAPTIVE_CAPTURE_NOT_FOUND"
ADAPTIVE_CAPTURE_TAMPERED = "ADAPTIVE_CAPTURE_TAMPERED"
TARGET_DIGEST_SCHEME = "icbm-capture-target/v1"
REFUSAL_MAX_CHARS = 500


class CaptureTampered(AppError):
    error_class = ErrorClass.FATAL


def _capture_refused(message: str) -> InputValidationError:
    return InputValidationError(ADAPTIVE_CAPTURE_REFUSED, message)


def _validate_read_budget(read_budget: Mapping[str, tuple[str, int]]) -> None:
    if set(read_budget) != set(READ_CLASSES):
        raise _capture_refused("a Phase C read budget names every request class")
    for scope, ceiling in read_budget.values():
        if scope not in BUDGET_SCOPES or isinstance(ceiling, bool) or ceiling < 0:
            raise _capture_refused("a Phase C ceiling is a scope and a non-negative count")
    if read_budget["CONNECT_AUTHENTICATE"][1] != 0:
        raise _capture_refused("CONNECT_AUTHENTICATE is a hard zero for a Phase C run")


def target_digest(supplier_key: str, target: str) -> str:
    """A target's identity: the supplier and its normalized in-scope product URL, as the pacing key
    already normalizes it. The URL itself is never stored by the capture owner."""
    return digest(TARGET_DIGEST_SCHEME, {"supplier": supplier_key, "target": target})


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    campaign_id: str
    supplier_key: str
    target_digest: str
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    consumed_by: str | None
    correlation_id: str


@dataclass(frozen=True)
class CandidateView:
    collection_run_id: str
    request_id: str
    campaign_id: str
    supplier_key: str
    revision_id: str
    status: str
    candidate: CaptureCandidate | None
    refusal: str | None
    captured_at: datetime


class CaptureStore:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        *,
        supplier_gate: SupplierGate,
        validation: AdaptiveValidationStore,
    ) -> None:
        self._db = db
        self._clock = clock
        self._admits = supplier_gate
        self._validation = validation

    # -------------------------------------------------------------- harness: requests

    def request(
        self,
        *,
        campaign_id: str,
        supplier_key: str,
        target: str,
        lifetime: timedelta,
        requested_by: str,
        correlation_id: str,
        read_budget: Mapping[str, tuple[str, int]] | None = None,
        stage: str = "C1",
    ) -> str:
        """One capture request. With ``read_budget`` (class -> (scope, ceiling)) it also registers
        the campaign's frozen Phase C read ceilings for ``stage`` in the same unit, once: a later
        request of the campaign must name exactly the same ceilings (C1 PREP-0)."""
        if not self._admits(supplier_key):
            raise _capture_refused("a capture is requested only for a registered supplier")
        if not timedelta(0) < lifetime <= timedelta(hours=REQUEST_MAX_HOURS):
            raise _capture_refused(f"a capture request lapses within {REQUEST_MAX_HOURS} hours")
        if read_budget is not None:
            _validate_read_budget(read_budget)
        now = self._clock.now()
        request_id = str(uuid.uuid4())
        with self._db.write() as session:
            if read_budget is not None:
                self._register_budget(session, campaign_id, stage, read_budget, now)
            session.add(
                CaptureRequest(
                    request_id=request_id,
                    campaign_id=campaign_id,
                    supplier_key=supplier_key,
                    target_digest=target_digest(supplier_key, target),
                    requested_by=requested_by,
                    correlation_id=correlation_id,
                    requested_at=now,
                    expires_at=now + lifetime,
                )
            )
        return request_id

    @staticmethod
    def _register_budget(
        session: Session,
        campaign_id: str,
        stage: str,
        read_budget: Mapping[str, tuple[str, int]],
        now: datetime,
    ) -> None:
        stored = {
            row.request_class: (row.scope, row.ceiling)
            for row in session.scalars(
                select(PhaseCReadBudget).where(
                    PhaseCReadBudget.campaign_id == campaign_id, PhaseCReadBudget.stage == stage
                )
            )
        }
        if stored:
            if stored != dict(read_budget):
                raise _capture_refused("a campaign's Phase C read ceilings are frozen once")
            return
        for request_class, (scope, ceiling) in sorted(read_budget.items()):
            session.add(
                PhaseCReadBudget(
                    campaign_id=campaign_id,
                    stage=stage,
                    request_class=request_class,
                    scope=scope,
                    ceiling=ceiling,
                    registered_at=now,
                )
            )

    def requests(self, campaign_id: str) -> tuple[RequestRecord, ...]:
        return self._requests(CaptureRequest.campaign_id == campaign_id)

    def request_by_correlation(self, correlation_id: str) -> RequestRecord | None:
        """The request one harness command wrote, if it wrote one: owner truth for its
        reconciliation."""
        found = self._requests(CaptureRequest.correlation_id == correlation_id)
        return found[0] if found else None

    def _requests(self, where: object) -> tuple[RequestRecord, ...]:
        with self._db.read() as session:
            rows = session.scalars(
                select(CaptureRequest)
                .where(where)  # type: ignore[arg-type]
                .order_by(CaptureRequest.requested_at, CaptureRequest.request_id)
            ).all()
            out = []
            for row in rows:
                consumer = session.scalar(
                    select(CollectionRun.collection_run_id).where(
                        CollectionRun.capture_request_id == row.request_id
                    )
                )
                out.append(
                    RequestRecord(
                        row.request_id,
                        row.campaign_id,
                        row.supplier_key,
                        row.target_digest,
                        row.requested_by,
                        row.requested_at,
                        row.expires_at,
                        consumer,
                        row.correlation_id,
                    )
                )
            return tuple(out)

    # -------------------------------------------------------------- the run store's freezer

    def freeze(self, session: Session, supplier_key: str, target: str) -> FrozenCapture:
        """Read-only, in the reservation's own unit: consume the oldest live request for this
        target, or answer OFF."""
        now = self._clock.now()
        consumed = exists().where(CollectionRun.capture_request_id == CaptureRequest.request_id)
        request_id = session.scalar(
            select(CaptureRequest.request_id)
            .where(
                CaptureRequest.supplier_key == supplier_key,
                CaptureRequest.target_digest == target_digest(supplier_key, target),
                CaptureRequest.expires_at > now,
                ~consumed,
            )
            .order_by(CaptureRequest.requested_at, CaptureRequest.request_id)
            .limit(1)
        )
        return CAPTURE_OFF if request_id is None else FrozenCapture("REQUESTED", request_id)

    # -------------------------------------------------------------- the capture step's write

    def _bound(self, session: Session, run_id: str, request_id: str, revision_id: str) -> str:
        run = session.get(CollectionRun, run_id)
        revision = session.get(ProductFactsRevision, revision_id)
        if (
            run is None
            or run.capture_decision != "REQUESTED"
            or run.capture_request_id != request_id
            or revision is None
            or revision.collection_run_id != run_id
        ):
            raise _capture_refused(
                "a candidate belongs to its requested run and that run's own revision"
            )
        return run.supplier_key

    def record(
        self,
        *,
        collection_run_id: str,
        request_id: str,
        revision_id: str,
        candidate: CaptureCandidate,
    ) -> None:
        with self._db.write() as session:
            supplier_key = self._bound(session, collection_run_id, request_id, revision_id)
            session.add(
                CaptureCandidateRecord(
                    collection_run_id=collection_run_id,
                    request_id=request_id,
                    supplier_key=supplier_key,
                    revision_id=revision_id,
                    status="CAPTURED",
                    structure_json=candidate.structure_json,
                    excluded_json=candidate.excluded_json,
                    removals_json=candidate.removals_json,
                    candidate_digest=candidate.digest,
                    refusal=None,
                    captured_at=self._clock.now(),
                )
            )

    def refuse(
        self, *, collection_run_id: str, request_id: str, revision_id: str, refusal: str
    ) -> None:
        """Why a requested run has no candidate: kinds and boundaries only, never a value."""
        with self._db.write() as session:
            supplier_key = self._bound(session, collection_run_id, request_id, revision_id)
            session.add(
                CaptureCandidateRecord(
                    collection_run_id=collection_run_id,
                    request_id=request_id,
                    supplier_key=supplier_key,
                    revision_id=revision_id,
                    status="REFUSED",
                    structure_json=None,
                    excluded_json=None,
                    removals_json=None,
                    candidate_digest=None,
                    refusal=(refusal or "CAPTURE_REFUSED")[:REFUSAL_MAX_CHARS],
                    captured_at=self._clock.now(),
                )
            )

    # -------------------------------------------------------------- reads

    def candidate(self, collection_run_id: str) -> CandidateView:
        with self._db.read() as session:
            row = session.get(CaptureCandidateRecord, collection_run_id)
            if row is None:
                raise NotFoundError(ADAPTIVE_CAPTURE_NOT_FOUND, "no capture for that run")
            request = session.get(CaptureRequest, row.request_id)
            if request is None:
                raise CaptureTampered(
                    ADAPTIVE_CAPTURE_TAMPERED, "a stored capture candidate has no request"
                )
            campaign_id = request.campaign_id
            session.expunge(row)
        candidate = None
        if row.status == "CAPTURED":
            assert row.structure_json and row.excluded_json and row.removals_json
            assert row.candidate_digest is not None
            candidate = CaptureCandidate(
                row.structure_json, row.excluded_json, row.removals_json, row.candidate_digest
            )
            if (
                candidate_digest(candidate.structure, candidate.excluded, candidate.removals)
                != candidate.digest
            ):
                raise CaptureTampered(
                    ADAPTIVE_CAPTURE_TAMPERED, "a stored capture candidate does not recompute"
                )
        return CandidateView(
            row.collection_run_id,
            row.request_id,
            campaign_id,
            row.supplier_key,
            row.revision_id,
            row.status,
            candidate,
            row.refusal,
            row.captured_at,
        )

    def candidates(self, campaign_id: str) -> tuple[CandidateView, ...]:
        with self._db.read() as session:
            ids = session.scalars(
                select(CaptureCandidateRecord.collection_run_id)
                .join(
                    CaptureRequest, CaptureRequest.request_id == CaptureCandidateRecord.request_id
                )
                .where(CaptureRequest.campaign_id == campaign_id)
                .order_by(
                    CaptureCandidateRecord.captured_at, CaptureCandidateRecord.collection_run_id
                )
            ).all()
        return tuple(self.candidate(run_id) for run_id in ids)

    # -------------------------------------------------------------- harness: finalize

    def finalize(
        self,
        collection_run_id: str,
        *,
        campaign_id: str,
        scope: OperatorScope,
        expected: dict[str, object],
        stored_by: str,
        correlation_id: str,
    ) -> ValidationSample:
        """A new immutable ValidationSample from one captured candidate of ``campaign_id``,
        stored through P2."""
        sample = self.prepare_sample(
            collection_run_id, campaign_id=campaign_id, scope=scope, expected=expected
        )
        self.store_sample(
            collection_run_id,
            sample,
            campaign_id=campaign_id,
            stored_by=stored_by,
            correlation_id=correlation_id,
        )
        return sample

    def prepare_sample(
        self,
        collection_run_id: str,
        *,
        campaign_id: str,
        scope: OperatorScope,
        expected: dict[str, object],
    ) -> ValidationSample:
        """Cut the sample in memory and store nothing: its digest is known before the harness
        reserves the command that stores it."""
        view = self.candidate(collection_run_id)
        if view.campaign_id != campaign_id:
            raise _capture_refused("a campaign finalizes only its own capture candidates")
        if view.candidate is None:
            raise _capture_refused("only a captured candidate becomes a sample")
        try:
            return sample_from_candidate(view.candidate, scope, expected)
        except CaptureRefused as refused:
            raise _capture_refused(str(refused)) from None

    def store_sample(
        self,
        collection_run_id: str,
        sample: ValidationSample,
        *,
        campaign_id: str,
        stored_by: str,
        correlation_id: str,
    ) -> None:
        """Store a prepared sample through P2, only for its own candidate and campaign."""
        view = self.candidate(collection_run_id)
        if view.campaign_id != campaign_id:
            raise _capture_refused("a campaign finalizes only its own capture candidates")
        if view.candidate is None or sample.provenance.get("candidate") != view.candidate.digest:
            raise _capture_refused("a sample is stored only for the candidate it was cut from")
        self._validation.save_sample(
            sample,
            supplier_key=view.supplier_key,
            stored_by=stored_by,
            correlation_id=correlation_id,
        )
