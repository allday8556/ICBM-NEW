"""The SmartStore side of registration execution (M5 PR-E; CREATE adoption, ADR-0020 §4 slice 1).

Product search remains NOT_ADOPTED. IMAGE UPLOAD is separately adopted, but is not wired to this
registration execution module: it has its own one-call adapter, and its durable owner is the ASSET
upload-attempt owner of ADR-0018 §3.4 (``app.live.assets``, Gate 3 area 1), not anything here. The
production seams here are shaped by those boundaries:

* :class:`SmartStoreCreateSender` is the adopted CREATE contract over the registry-gated caller.
  **Adoption is not a session and not LIVE authority.** Production wires neither a caller nor a
  committed bearer (``app/container.py``), so it reports unavailable there and the REGISTER send
  gate refuses before anything is built; even with both, every send still passes the ADR-0018
  send-time safety stack, which refuses while ``M0_DRY_RUN_ONLY`` holds;
* :class:`SmartStoreReadback` is real: it calls the adopted origin read-back through the registry
  caller and returns only the retained, sanitized response (PR-D's profile);
* :class:`SmartStoreReconcileLookup` reports unavailable, because no product-search contract is
  adopted; an UNKNOWN CREATE therefore stays unresolved rather than being fabricated into an
  absence (ADR-0014 §10, §28.2).

**What a CREATE handoff may claim.** ``NOT_APPLIED_PROVEN`` only from evidence ERRORS.md §15 admits:
a local pre-submit refusal before any transport handoff, transmission-precluded transport evidence,
or the reviewed definitive provider rejection of ``classify.definitive_rejection``. Everything else
- a timeout, a lost or truncated response, an uncertain transmission, a 5xx, a redirect, a 2xx that
fails the success predicate - is ``UNKNOWN``, and an ``UNKNOWN`` is never resent (ADR-0014 §28.3,
M5-08, M5-33). The provider documents no CREATE idempotency, so this adapter has no retry loop at
all (``registry.CREATE_AUTOMATIC_RETRY_BUDGET``).

Nothing here decides retry, state or evidence: that is the domain owner's
(``app.register.execution``). This module only hands over what the provider contract allows.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

from app.connect.marketplace.capability import RemoteOutcome
from app.core.errors import AppError, ErrorClass
from app.register.provider import CreateHandoff
from integrations.marketplaces.smartstore.caller import (
    ProductCreateRequest,
    ProductCreateResult,
    ProductReadRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.product import WireContractError, create_document
from integrations.marketplaces.smartstore.registry import (
    ADOPTED,
    ADOPTION_GAPS,
    CREATE_AUTOMATIC_RETRY_BUDGET,
    CREATE_PROVIDER_IDEMPOTENCY,
    EndpointId,
)

MARKETPLACE_KEY: Final = "smartstore"
# The committed CONNECT session a read-back is made with, or None when there is none.
BearerSource = Callable[[], Any]


CREATE_ENDPOINT: Final = EndpointId.SMARTSTORE_PRODUCT_CREATE_V2


class CreateNotAdoptedError(AppError):
    """A CREATE was requested while the provider contract is not adopted.

    The CREATE adoption slice moved ``SMARTSTORE_PRODUCT_CREATE_V2`` into the adopted registry, so
    this is now the defensive refusal that fires only if the registry ever stops adopting it. The
    registry stays the single adoption fact; nothing here decides adoption.
    """

    def __init__(self) -> None:
        super().__init__(
            "SMARTSTORE_CREATE_NOT_ADOPTED",
            "SmartStore product CREATE is not adopted: "
            + ADOPTION_GAPS.get(CREATE_ENDPOINT, "the endpoint contract is not in the registry"),
            details={"endpoint_id": CREATE_ENDPOINT.value},
        )


class CreateSessionUnavailableError(AppError):
    """A CREATE was requested with no committed SmartStore session to send it under.

    Adoption is not a session (ADR-0020 §2.4). Production wires no bearer, so this is the honest
    refusal there - never an outcome, and never reached through :meth:`SmartStoreCreateSender.send`
    while the REGISTER send gate asks :meth:`SmartStoreCreateSender.available` first.
    """

    def __init__(self) -> None:
        super().__init__(
            "SMARTSTORE_CREATE_SESSION_UNAVAILABLE",
            "no committed SmartStore session can send a product CREATE",
            details={"endpoint_id": CREATE_ENDPOINT.value},
        )


class ReconcileLookupNotAdoptedError(AppError):
    """A reconcile lookup was requested while no lookup contract is adopted."""

    def __init__(self) -> None:
        super().__init__(
            "SMARTSTORE_RECONCILE_LOOKUP_NOT_ADOPTED",
            "SmartStore product search is not adopted: "
            + ADOPTION_GAPS[EndpointId.SMARTSTORE_PRODUCT_SEARCH],
            details={"endpoint_id": EndpointId.SMARTSTORE_PRODUCT_SEARCH.value},
        )


class SmartStoreCreateSender:
    """The CREATE seam over the adopted ``SMARTSTORE_PRODUCT_CREATE_V2`` contract.

    It owns no transport of its own: every request goes through the registry-gated caller, which
    composes the one wire URL, applies the endpoint timeouts, refuses every redirect and evaluates
    the success predicate. It also owns **no state, no retry and no reconcile**: one call, one
    handoff, and the domain owner decides what that means (ADR-0014 §9-§11, §28).
    """

    def __init__(
        self,
        caller: SmartStoreEndpointCaller | None = None,
        bearer: "BearerSource | None" = None,
    ) -> None:
        # Both default to absent so production can wire the seam without a session: adoption is a
        # contract, not a credential, and the CREATE stays unsendable until a session exists.
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        """Whether a CREATE could actually be handed off now.

        All three must hold: the registry adopts the endpoint, a registry-gated caller exists, and
        a committed session answers. Production has no session, so this is ``False`` there and the
        REGISTER send gate refuses before any document is built.
        """
        if CREATE_ENDPOINT not in ADOPTED or self._caller is None or self._bearer is None:
            return False
        return self._bearer() is not None

    def send(
        self, *, payload: Mapping[str, Any], idempotency_key: str, listing_identity: str
    ) -> CreateHandoff:
        """One CREATE of one frozen Snapshot payload.

        ``idempotency_key`` is ICBM's own durable Intent identity (ADR-0014 §8). It is **not** put
        on the wire: review 5768247290 proves the provider offers no idempotency key, no
        request-correlation key and no replay rule, and inventing a header would claim a guarantee
        that does not exist. The safety against a double listing is ICBM's: one Intent, one
        Snapshot, never a resend of an ``UNKNOWN`` (§28.3).
        """
        if CREATE_ENDPOINT not in ADOPTED:
            raise CreateNotAdoptedError()
        bearer = self._bearer() if self._bearer is not None else None
        if self._caller is None or bearer is None:
            raise CreateSessionUnavailableError()
        try:
            document = create_document(payload)
        except WireContractError as refused:
            # ERRORS.md §15.1 item 1: a local pre-submit rejection, before any transport handoff.
            # Nothing reached the provider, so the outcome is proven, and the cause is FATAL:
            # resending the same unsendable Snapshot cannot succeed (§10.7; ADR-0014 §9).
            return self._refused_locally(listing_identity, refused.code, refused.detail)
        except (LookupError, TypeError, ValueError) as broken:
            # A Snapshot payload this projection cannot even read is the same boundary: it is a
            # local defect, it never left the machine, and it is never allowed to escape as an
            # unhandled error after the domain owner has already opened an Attempt.
            return self._refused_locally(
                listing_identity, "WIRE_SNAPSHOT_PAYLOAD_MALFORMED", type(broken).__name__
            )
        request = ProductCreateRequest(
            access_token=bearer.access_token,
            credential_generation=bearer.credential_generation,
            session_generation=bearer.session_generation,
            document=document,
        )
        try:
            result = self._caller.call(CREATE_ENDPOINT, request)
        except SmartStoreCallError as failure:
            # The caller already separated the cause from the mutation outcome, and the outcome it
            # carries is the only one this adapter reports: it never strengthens UNKNOWN, and it
            # never weakens a proven non-application into one.
            return CreateHandoff(
                remote_outcome=failure.remote_outcome,
                sanitized_request=document.canonical(),
                response_status=failure.http_status,
                error_class=failure.error_class,
                error_code=failure.code,
                details=self._details(
                    failure_layer=failure.classification.layer.value,
                    classification_basis=failure.classification.basis.value,
                    transmission_phase=failure.phase.value,
                    provider_code=failure.classification.provider_code,
                ),
            )
        assert isinstance(result, ProductCreateResult)
        # A 200 that carries the documented identifier proves the mutation happened - and nothing
        # more. ADR-0014 §11: the domain owner now reads the product back and compares it with the
        # immutable Snapshot; only that comparison confirms a registration.
        return CreateHandoff(
            remote_outcome=RemoteOutcome.APPLIED_PROVEN,
            sanitized_request=document.canonical(),
            marketplace_product_id=result.origin_product_no,
            response_status=result.http_status,
            sanitized_response=dict(result.retained),
            details=self._details(channel_product_nos=list(result.channel_product_nos)),
        )

    def _refused_locally(self, listing_identity: str, code: str, detail: str) -> CreateHandoff:
        """One local pre-submit refusal, recorded as the proven non-application it is."""
        return CreateHandoff(
            remote_outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            sanitized_request={
                "listing_identity": listing_identity,
                "refused_before_transport": code,
            },
            error_class=ErrorClass.FATAL,
            error_code=code,
            details=self._details(gap=detail),
        )

    @staticmethod
    def _details(**extra: Any) -> dict[str, Any]:
        """The sanitized facts every handoff carries: which contract, and on what replay terms."""
        return {
            "endpoint_id": CREATE_ENDPOINT.value,
            "provider_idempotency": CREATE_PROVIDER_IDEMPOTENCY,
            "automatic_retry_budget": CREATE_AUTOMATIC_RETRY_BUDGET,
            **{key: value for key, value in extra.items() if value is not None},
        }


class SmartStoreReadback:
    """The adopted origin-product read-back (PR-D), returning the retained response only."""

    def __init__(self, caller: SmartStoreEndpointCaller, bearer: "BearerSource") -> None:
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        """Whether a committed session exists to read with. Production has none wired yet, so a
        read-back is unavailable rather than unsafe; PR-F wires the real session."""
        return self._bearer() is not None

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]:
        bearer = self._bearer()
        if bearer is None:
            raise AppError(
                "SMARTSTORE_SESSION_UNAVAILABLE",
                "no committed SmartStore session can read a product back",
            )
        result = self._caller.call(
            EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2,
            ProductReadRequest(
                bearer.access_token,
                bearer.credential_generation,
                bearer.session_generation,
                marketplace_product_id,
            ),
        )
        return dict(result.retained)


class SmartStoreReconcileLookup:
    """The reconcile lookup seam. Unavailable while product search is not adopted."""

    def available(self) -> bool:
        return False

    def find(self, *, marketplace_account_id: str, listing_identity: str) -> Mapping[str, Any]:
        raise ReconcileLookupNotAdoptedError()


__all__ = [
    "CREATE_ENDPOINT",
    "MARKETPLACE_KEY",
    "BearerSource",
    "CreateNotAdoptedError",
    "CreateSessionUnavailableError",
    "ReconcileLookupNotAdoptedError",
    "SmartStoreCreateSender",
    "SmartStoreReadback",
    "SmartStoreReconcileLookup",
]
