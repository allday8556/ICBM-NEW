"""The deletion of one ICBM-confirmed registration (ADR-0018 §3.5).

A deletion removes a listing ICBM itself registered and confirmed by read-back, and nothing else:
the registration must be ``ACTIVE`` and its provider identity is the one its confirmed Intent
recorded. It is a marketplace mutation, so it runs only through the send-time safety stack: the
attempt is opened, with its exact DELETE grant spent, in the one unit that admits it and before any
byte is sent; a refusal is audited and leaves nothing behind.

**Outcomes.** ``APPLIED_PROVEN`` only on the documented success; ``NOT_APPLIED_PROVEN`` only when
transmission was provably precluded; everything else ``UNKNOWN``. **An unknown deletion is never
resent**: while an attempt is in flight, applied, or unknown without a read-back that shows the
listing still there, no new grant is issued and no new attempt starts.

**Verification.** After a possibly applied attempt an origin-product read-back is taken when a
session exists. The documented sale status ``DELETE`` confirms the deletion; any other documented
sale status shows the listing still there; a failed or unreadable read-back records nothing. For an
unknown attempt, only that read-back can resolve it, and only it can open the way to a new grant.

**A 404 after a proven deletion** (owner decision 2026-10-07, Issue #219 ``6031580064``; ADR-0018
§3.5 amendment). Once the provider removes the product, the origin-product read answers HTTP 404
instead of a ``DELETE`` sale status. After an ``APPLIED_PROVEN`` deletion that 404 confirms it.
After an ``UNKNOWN`` attempt it still proves nothing, because a 404 then has another possible
cause.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from app.capabilities.live_safety.model import MutationRefused, MutationStage
from app.platform.core.errors import AppError, NotFoundError
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.model import (
    DeletionState,
    DeletionVerification,
    RegistrationConflictError,
    RegistrationLifecycle,
)
from app.stages.register.provider import ReadbackSource
from app.stages.register.store import DeletionRecord, RegistrationRecord, RegistrationStore

# The one sale status that confirms a deletion (the documented ``statusType`` enumeration).
DELETED_SALE_STATUS = "DELETE"
# The read-back status that confirms a proven deletion (owner decision 2026-10-07).
NOT_FOUND_STATUS = 404


@dataclass(frozen=True)
class DeleteHandoff:
    """What one DELETE handoff proved. ``remote_outcome`` is never inferred from an exception
    type: only the transmission-precluded whitelist is ``NOT_APPLIED_PROVEN``."""

    remote_outcome: RemoteOutcome
    response_status: int | None = None
    error_code: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class DeleteSender(Protocol):
    """The provider DELETE handoff of one origin product."""

    def endpoint_adopted(self) -> bool:
        """Whether the DELETE contract is adopted — the stack's endpoint-adoption layer."""
        ...

    def send(self, *, marketplace_product_id: str) -> DeleteHandoff:
        """Hand one deletion to the provider, or refuse locally before any transport."""
        ...


class DeleteAuthority(Protocol):
    """The send-time safety stack's DELETE admission (``SafetyStack``)."""

    def admit_delete(
        self,
        session: Session,
        *,
        registration: RegistrationRecord,
        endpoint_adopted: bool,
        actor: str,
        correlation_id: str,
    ) -> Any: ...

    def record_refusal(
        self,
        refusal: MutationRefused,
        *,
        stage: MutationStage,
        target_ref: str,
        actor: str,
        correlation_id: str,
    ) -> None: ...


# The sale status a retained read-back carries, or None — the marketplace normalizer, wired by the
# composition root so REGISTER never imports a marketplace adapter.
SaleStatusReader = Callable[[Mapping[str, Any]], str | None]

_STATES = {
    RemoteOutcome.APPLIED_PROVEN: DeletionState.APPLIED_PROVEN,
    RemoteOutcome.NOT_APPLIED_PROVEN: DeletionState.NOT_APPLIED_PROVEN,
    RemoteOutcome.UNKNOWN: DeletionState.UNKNOWN,
}


class RegistrationDeletionService:
    def __init__(
        self,
        *,
        registrations: RegistrationStore,
        sender: DeleteSender,
        readback: ReadbackSource,
        sale_status: SaleStatusReader,
        authority: DeleteAuthority,
    ) -> None:
        self._registrations = registrations
        self._sender = sender
        self._readback = readback
        self._sale_status = sale_status
        self._authority = authority

    def deletions(self, registration_id: str) -> tuple[DeletionRecord, ...]:
        return self._registrations.deletions(registration_id)

    def delete(self, registration_id: str, *, actor: str, correlation_id: str) -> DeletionRecord:
        """Delete one ACTIVE confirmed registration at the provider, exactly once."""
        registration = self._registration(registration_id)
        if registration.lifecycle_state is not RegistrationLifecycle.ACTIVE:
            raise RegistrationConflictError(
                "REGISTER_DELETE_NOT_ACTIVE", "only an ACTIVE confirmed registration is deleted"
            )
        if self._registrations.deletion_open(registration_id):
            raise RegistrationConflictError(
                "REGISTER_DELETE_OPEN",
                "a deletion of this registration is in flight, applied or unresolved; it is never"
                " resent",
            )
        try:
            with self._registrations.transaction() as unit:
                # ADR-0018 §4.3, §3.5: the send-time stack, deny by default, in the very unit that
                # opens the attempt and before it writes anything; the DELETE grant is spent here.
                grant = self._authority.admit_delete(
                    unit.session,
                    registration=registration,
                    endpoint_adopted=self._sender.endpoint_adopted(),
                    actor=actor,
                    correlation_id=correlation_id,
                )
                started = unit.start_deletion(
                    registration,
                    grant_id=grant.grant_id,
                    actor=actor,
                    correlation_id=correlation_id,
                )
        except MutationRefused as refusal:
            # The unit rolled back: no attempt, no spent grant, nothing sent.
            self._authority.record_refusal(
                refusal,
                stage=MutationStage.DELETE,
                target_ref=f"marketplace_registration:{registration_id}",
                actor=actor,
                correlation_id=correlation_id,
            )
            raise
        handoff = self._sender.send(marketplace_product_id=registration.marketplace_product_id)
        with self._registrations.transaction() as unit:
            finished = unit.finish_deletion(
                started.deletion_id,
                state=_STATES[handoff.remote_outcome],
                response_status=handoff.response_status,
                error_code=handoff.error_code,
                correlation_id=correlation_id,
            )
        if finished.state in (DeletionState.APPLIED_PROVEN, DeletionState.UNKNOWN):
            return self._verify(finished, correlation_id=correlation_id) or finished
        return finished

    def verify(self, registration_id: str, *, correlation_id: str) -> DeletionRecord:
        """Read the listing back after a possibly applied deletion and record what it shows."""
        self._registration(registration_id)
        latest = next(iter(reversed(self._registrations.deletions(registration_id))), None)
        if latest is None or latest.state not in (
            DeletionState.APPLIED_PROVEN,
            DeletionState.UNKNOWN,
        ):
            raise RegistrationConflictError(
                "REGISTER_DELETE_NOT_VERIFIABLE",
                "only a deletion that may have been applied is read back",
            )
        if not self._readback.available():
            raise RegistrationConflictError(
                "REGISTER_READBACK_UNAVAILABLE", "no committed session can read the listing back"
            )
        return self._verify(latest, correlation_id=correlation_id) or latest

    def _verify(self, record: DeletionRecord, *, correlation_id: str) -> DeletionRecord | None:
        if record.verification is DeletionVerification.DELETE_CONFIRMED:
            return record
        if record.verification is not None and record.state is DeletionState.UNKNOWN:
            return record
        if not self._readback.available():
            return None
        try:
            retained = self._readback.read(marketplace_product_id=record.marketplace_product_id)
        except AppError as failed:
            if record.state is DeletionState.APPLIED_PROVEN and _not_found(failed):
                # The proven deletion's own product is gone at the provider (owner decision
                # 6031580064): the read answers 404 instead of a DELETE sale status.
                verification = DeletionVerification.DELETE_CONFIRMED
                with self._registrations.transaction() as unit:
                    return unit.record_deletion_verification(
                        record.deletion_id, verification=verification, correlation_id=correlation_id
                    )
            # Any other failed read-back proves nothing either way; the attempt stays as it is.
            return None
        status = self._sale_status(retained)
        if status is None:
            return None
        verification = (
            DeletionVerification.DELETE_CONFIRMED
            if status == DELETED_SALE_STATUS
            else DeletionVerification.STILL_PRESENT
        )
        with self._registrations.transaction() as unit:
            return unit.record_deletion_verification(
                record.deletion_id, verification=verification, correlation_id=correlation_id
            )

    def _registration(self, registration_id: str) -> RegistrationRecord:
        registration = self._registrations.registration(registration_id)
        if registration is None:
            raise NotFoundError("REGISTER_REGISTRATION_NOT_FOUND", "no such registration")
        return registration


def _not_found(failed: AppError) -> bool:
    """Whether a failed read-back was the provider's HTTP 404 for the product."""
    return failed.details.get("http_status") == NOT_FOUND_STATUS


__all__ = [
    "DELETED_SALE_STATUS",
    "DeleteAuthority",
    "DeleteHandoff",
    "DeleteSender",
    "RegistrationDeletionService",
]
