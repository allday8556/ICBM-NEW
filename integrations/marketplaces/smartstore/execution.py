"""The SmartStore side of registration execution (M5 PR-E; CREATE and SEARCH adoption slices,
ADR-0020 §4 orders 1 and 2).

IMAGE UPLOAD is separately adopted, but is not wired to this
registration execution module: it has its own one-call adapter, and its durable owner is the ASSET
upload-attempt owner of ADR-0018 §3.4 (``app.capabilities.live_safety.assets``, Gate 3 area 1),
not anything here.

* :class:`SmartStoreCreateSender` implements the adopted ``POST /v2/products`` handoff through the
  registry-gated caller and returns a sanitized
  :class:`~app.stages.register.provider.CreateHandoff`;
* :class:`SmartStoreReadback` calls the adopted origin read-back through the same caller and
  returns only the retained, sanitized response (PR-D's profile);
* :class:`SmartStoreReconcileLookup` implements the adopted positive-only reconcile lookup
  (``POST /v1/products/search``): it enumerates the seller-code search within a bounded read
  budget and names only the exact ``STOREFARM`` candidates of this listing identity's provider
  code. It never proves remote absence and never authorizes a CREATE; an exact candidate still
  needs the origin read-back to carry the same code (ADR-0014 §10, §17.2, §28.2).

**Adoption is not permission.** Nothing here decides retry, state or evidence: that is the domain
owner's (``app.stages.register.execution``), and the ADR-0018 §4.3 send-time safety stack stands in
front of it. Production stays ``DRY_RUN`` / ``M0_DRY_RUN_ONLY``, so every CREATE is refused before a
handoff can happen, and the wire projection is not sendable while a required value stays uncaptured
by the official evidence or unowned by ICBM (``product.py``).

**The outcome rules this seam must never soften** (ADR-0014 §9–§10, §28; ADR-0018 §6.1):

* ``error_class`` (the cause) and ``remote_outcome`` (whether the mutation happened) are
  independent axes;
* ``NOT_APPLIED_PROVEN`` is whitelist-only — a local pre-handoff refusal or transmission-precluded
  evidence of this one request (``transmission.TRANSMISSION_PRECLUDED``);
* a timeout, a lost connection or response, a ``5xx`` after a possible handoff, an ordinary
  post-handoff ``4xx`` and an unsafe redirect are all ``UNKNOWN`` — and so is a success whose
  documented top-level ``integer<int64>`` identifiers cannot be read (``create.py``);
* ``APPLIED_PROVEN`` is a success whose documented identifiers *are* readable. It is provider-side
  application evidence and hands on ``originProductNo`` as the read-back identity; it is never a
  registration success — read-back and Snapshot comparison stay the execution owner's separate
  proof (ADR-0014 §11);
* an ``UNKNOWN`` is **never** resent from here: this seam retries nothing and reopens nothing.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

from app.platform.core.errors import AppError, ErrorClass
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.provider import CreateHandoff, LookupStatus
from app.stages.register.sanitize import SECRET_MATERIAL, problems
from integrations.marketplaces.smartstore import create, product, readback, search
from integrations.marketplaces.smartstore.caller import (
    ProductCreateRequest,
    ProductReadRequest,
    ProductSearchRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import ADOPTED, EndpointId
from integrations.marketplaces.smartstore.transmission import Phase

MARKETPLACE_KEY: Final = "smartstore"
# The committed CONNECT session a call is made with, or None when there is none.
BearerSource = Callable[[], Any]


class SmartStoreCreateSender:
    """The adopted CREATE handoff (``POST /v2/products``), through the registry-gated caller."""

    def __init__(self, caller: SmartStoreEndpointCaller, bearer: "BearerSource") -> None:
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        """Whether the provider CREATE contract is adopted at all.

        This is the endpoint-adoption fact the ADR-0018 §10 send-time stack reads, so it answers
        adoption and nothing else. Every other condition — a committed session, a sendable
        projection, the execution mode, the grant, the brake — is its own refusal, each of which
        still stands in front of a real handoff.
        """
        return EndpointId.SMARTSTORE_PRODUCT_CREATE_V2 in ADOPTED

    def send(
        self, *, payload: Mapping[str, Any], idempotency_key: str, listing_identity: str
    ) -> CreateHandoff:
        """Hand one frozen Snapshot payload to the provider, or refuse before any transport.

        ``idempotency_key`` is ICBM's own durable Intent identity. It is **not** put on the wire:
        SmartStore documents no idempotency key, no request-correlation key and no replay rule, so
        inventing a header for one would claim a guarantee the provider does not give
        (ADR-0014 §17.2, §28).
        """
        refusal, document = self._document(payload, listing_identity)
        if refusal is not None:
            return refusal
        assert document is not None
        sanitized_request = document.mapping()
        bearer = self._bearer()
        if bearer is None:
            return _local_refusal(
                "SMARTSTORE_SESSION_UNAVAILABLE",
                sanitized_request,
                reason="no committed SmartStore session can register a product",
            )
        try:
            response = self._caller.call(
                EndpointId.SMARTSTORE_PRODUCT_CREATE_V2,
                ProductCreateRequest(
                    bearer.access_token,
                    bearer.credential_generation,
                    bearer.session_generation,
                    document,
                ),
            )
        except SmartStoreCallError as failure:
            # The caller already decided the remote outcome from this request's own transmission
            # evidence: NOT_APPLIED_PROVEN only for the whitelist, UNKNOWN for everything else —
            # a timeout, a lost response, a 5xx, an ordinary 4xx, an unexpected redirect.
            return CreateHandoff(
                remote_outcome=failure.remote_outcome,
                sanitized_request=sanitized_request,
                response_status=failure.http_status,
                error_class=failure.error_class,
                error_code=failure.code,
                details={
                    "endpoint_id": EndpointId.SMARTSTORE_PRODUCT_CREATE_V2.value,
                    "transmission_phase": failure.phase.value,
                    "failure_layer": failure.classification.layer.value,
                    "provider_code": failure.classification.provider_code,
                },
            )
        reading = create.read(response.retained)
        sanitized_response = {
            "retained": dict(response.retained),
            "response_contract": reading.canonical(),
        }
        if reading.readable:
            # The response passed the endpoint success predicate and carries the documented
            # top-level integer<int64> identifiers (E3, create.py): the mutation was applied, and
            # originProductNo is the identity the read-back is made by. This is provider-side
            # application evidence only — never a registration success. The execution owner
            # continues to read-back and Snapshot comparison, which alone confirm (ADR-0014 §11).
            return CreateHandoff(
                remote_outcome=RemoteOutcome.APPLIED_PROVEN,
                sanitized_request=sanitized_request,
                marketplace_product_id=reading.marketplace_product_id,
                marketplace_channel_product_id=reading.marketplace_channel_product_id,
                response_status=response.http_status,
                sanitized_response=sanitized_response,
                details={"endpoint_id": EndpointId.SMARTSTORE_PRODUCT_CREATE_V2.value},
            )
        # A success whose documented identifiers cannot be read — missing, nested, a numeric string,
        # a bool, out of the int64 range. That proves nothing either way — the product may exist —
        # so it is UNKNOWN, never NOT_APPLIED_PROVEN, never a failure and never a resend
        # (ADR-0014 §28.3).
        return CreateHandoff(
            remote_outcome=RemoteOutcome.UNKNOWN,
            sanitized_request=sanitized_request,
            response_status=response.http_status,
            sanitized_response=sanitized_response,
            error_class=ErrorClass.UNKNOWN,
            error_code=create.RESPONSE_UNREADABLE,
            details={"endpoint_id": EndpointId.SMARTSTORE_PRODUCT_CREATE_V2.value},
        )

    def _projection(self, payload: Mapping[str, Any]) -> object:
        """The one projection a CREATE is ever sent from: the adopted wire projection of the frozen
        Snapshot itself (``product.project``).

        Nothing can hand this sender a projection: whether a request is complete is decided only by
        the gaps ``product.project`` records from the Snapshot, never by a caller's declaration. At
        this adoption every projection carries gaps, so every CREATE is refused here.
        """
        return product.project(payload)

    def _completeness(self, document: product.CreateDocument) -> tuple[str, ...]:
        """The provider-required registration parts the document body lacks
        (``product.completeness_gaps``) — recomputed here, never trusted from the projection."""
        return product.completeness_gaps(document)

    def _document(
        self, payload: Mapping[str, Any], listing_identity: str
    ) -> tuple[CreateHandoff | None, product.CreateDocument | None]:
        """The typed CREATE request of this Snapshot, or the local refusal that replaces it."""
        try:
            projection = self._projection(payload)
        except product.WireContractError as refused:
            # The Snapshot violates a documented provider rule, or is not a registration payload
            # at all. Either way no request exists and nothing left the machine.
            return (
                _local_refusal(
                    f"SMARTSTORE_CREATE_{refused.code}",
                    {},
                    reason=refused.detail,
                ),
                None,
            )
        if not isinstance(projection, product.WireProjection):
            return (
                _local_refusal(
                    "SMARTSTORE_CREATE_WIRE_CONTRACT_VIOLATION",
                    {},
                    reason="the projection is not the adopted wire projection",
                ),
                None,
            )
        if projection.gaps:
            return (
                _local_refusal(
                    "SMARTSTORE_CREATE_WIRE_NOT_SENDABLE",
                    {},
                    reason="the adopted CREATE request cannot be projected from this Snapshot",
                    gaps=list(projection.gaps)[:4],
                ),
                None,
            )
        try:
            # Only the wire projection may build a request, and only through the validated, frozen
            # document type — re-proven here, because the type's constructor proves nothing.
            # Anything else is a broken projection, not a request.
            document = product.verified(projection.document)
        except product.WireContractError:
            return (
                _local_refusal(
                    "SMARTSTORE_CREATE_WIRE_CONTRACT_VIOLATION",
                    {},
                    reason="the projection produced no validated, frozen CREATE document",
                ),
                None,
            )
        missing = self._completeness(document)
        if missing:
            # Completeness is read from the document body itself, not taken from the projection:
            # a CREATE lacking a part the provider requires on registration never leaves here.
            return (
                _local_refusal(
                    "SMARTSTORE_CREATE_WIRE_NOT_SENDABLE",
                    {},
                    reason="the CREATE document lacks a part the provider requires on registration",
                    gaps=list(missing)[:4],
                ),
                None,
            )
        if document.listing_identity != listing_identity:
            # The frozen document is bound to the listing identity its Snapshot projected; the
            # execution owner hands the Intent's own identity. A document of another listing is
            # never sent under this Intent, whatever its body says (ADR-0014 §7, §15).
            return (
                _local_refusal(
                    "SMARTSTORE_CREATE_IDENTITY_MISMATCH",
                    {},
                    reason="the projected CREATE document belongs to another listing identity",
                ),
                None,
            )
        secrets = [where for code, where in problems(document.mapping()) if code == SECRET_MATERIAL]
        if secrets:
            # The payload builder already refuses secret-bearing values; this is the last fence
            # before wire bytes exist, because the same mapping is the durable digest source
            # (ADR-0014 §15, B4). Provider-hosted https image references are legitimate here and
            # were proven safe when the Snapshot froze them, so only secret material is refused.
            return (
                _local_refusal(
                    "SMARTSTORE_CREATE_REQUEST_UNSANITIZED",
                    {},
                    reason="the projected CREATE request carries secret-bearing material",
                    fields=secrets[:4],
                ),
                None,
            )
        return None, document


def _local_refusal(code: str, document: Mapping[str, Any], **details: Any) -> CreateHandoff:
    """One CREATE that was refused locally, before any transport existed.

    ``LOCAL_PREFLIGHT`` is on the transmission-precluded whitelist (ERRORS.md §15.1), so nothing
    can have been applied and the outcome is ``NOT_APPLIED_PROVEN`` — the one place this seam may
    say so. The cause is ``FATAL``: a request ICBM itself could not build is never automatically
    retried (ADR-0014 §9). The refusal is returned rather than raised, so the open Attempt is
    settled with the truth instead of dead-lettering into an ``UNKNOWN`` nothing produced.
    """
    return CreateHandoff(
        remote_outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
        sanitized_request=dict(document),
        error_class=ErrorClass.FATAL,
        error_code=code,
        details={
            "endpoint_id": EndpointId.SMARTSTORE_PRODUCT_CREATE_V2.value,
            "transmission_phase": Phase.LOCAL_PREFLIGHT.value,
            **details,
        },
    )


class SmartStoreReadback:
    """The adopted origin-product read-back (PR-D), returning the retained response only."""

    def __init__(self, caller: SmartStoreEndpointCaller, bearer: "BearerSource") -> None:
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        """Whether a committed session exists to read with. Production has none wired yet, so a
        read-back is unavailable rather than unsafe."""
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
    """The adopted positive-only reconcile lookup (``POST /v1/products/search``; ADR-0014 §28.2).

    It searches by the ``smartstore-seller-management-code/v1`` projection of the listing identity
    and enumerates every page within a bounded read budget (``search.MAX_PAGES_PER_CHECK``). Only a
    complete, consistent enumeration is a trustworthy count, and from it only the exact
    ``STOREFARM`` matches are candidates. It never raises for a session, provider or response
    failure: each becomes ``UNAVAILABLE`` or ``ERROR``, which prove nothing. It never proves
    remote absence and never authorizes a CREATE — the REGISTER owner reads its answer under
    ADR-0014 §28.2–§28.4 only.
    """

    def __init__(self, caller: SmartStoreEndpointCaller, bearer: "BearerSource") -> None:
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        """Whether the lookup contract is adopted — the endpoint-adoption fact the ADR-0018 §10
        stack reads, and nothing else. A missing session is the lookup's own ``UNAVAILABLE``."""
        return EndpointId.SMARTSTORE_PRODUCT_SEARCH in ADOPTED

    def find(self, *, marketplace_account_id: str, listing_identity: str) -> Mapping[str, Any]:
        try:
            code = product.seller_management_code(listing_identity)
        except product.WireContractError:
            return self._evidence(LookupStatus.ERROR, "SMARTSTORE_SEARCH_IDENTITY_INVALID", None)
        bearer = self._bearer()
        if bearer is None:
            return self._evidence(LookupStatus.UNAVAILABLE, "SMARTSTORE_SESSION_UNAVAILABLE", code)
        pages: list[search.SearchPage] = []
        for number in range(search.FIRST_PAGE, search.FIRST_PAGE + search.MAX_PAGES_PER_CHECK):
            try:
                answer = self._caller.call(
                    EndpointId.SMARTSTORE_PRODUCT_SEARCH,
                    ProductSearchRequest(
                        bearer.access_token,
                        bearer.credential_generation,
                        bearer.session_generation,
                        code,
                        number,
                        search.MAX_PAGE_SIZE,
                    ),
                )
                page = search.read_page(answer.retained)
            except SmartStoreCallError as failure:
                # A rate or quota refusal defers the check (§28.4); anything else is an error.
                # Neither proves anything about the listing.
                status = (
                    LookupStatus.UNAVAILABLE
                    if failure.error_class is ErrorClass.RATE_LIMITED
                    else LookupStatus.ERROR
                )
                return self._evidence(status, failure.code, code, pages=len(pages))
            except search.SearchContractError as unreadable:
                return self._evidence(LookupStatus.ERROR, unreadable.code, code, pages=len(pages))
            pages.append(page)
            if page.last:
                break
        else:
            # The result needs more pages than one check may read: a partial enumeration is never
            # a count, let alone a unique positive result.
            return self._evidence(
                LookupStatus.UNAVAILABLE,
                "SMARTSTORE_SEARCH_READ_BUDGET_EXCEEDED",
                code,
                pages=len(pages),
            )
        try:
            search.check_enumeration(pages)
        except search.SearchContractError as inconsistent:
            return self._evidence(LookupStatus.ERROR, inconsistent.code, code, pages=len(pages))
        candidates = search.exact_candidates(pages, code)
        return self._evidence(
            LookupStatus.COMPLETE,
            None,
            code,
            pages=len(pages),
            total_elements=pages[0].total_elements,
            candidates=[candidate.canonical() for candidate in candidates],
        )

    def confirms_candidate(
        self, *, listing_identity: str, retained_readback: Mapping[str, Any]
    ) -> bool:
        """Whether the candidate's origin read-back carries exactly the projected code (§28.2)."""
        try:
            expected = product.seller_management_code(listing_identity)
        except product.WireContractError:
            return False
        listing = readback.normalize(retained_readback)
        return listing.readable and listing.seller_management_code == expected

    @staticmethod
    def _evidence(
        status: LookupStatus,
        code: str | None,
        seller_management_code: str | None,
        *,
        pages: int = 0,
        total_elements: int | None = None,
        candidates: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """The sanitized evidence of one lookup: identities, counts and versions only."""
        return {
            "lookup_contract_version": search.SEARCH_CONTRACT_VERSION,
            "status": status.value,
            "code": code,
            "seller_management_code": seller_management_code,
            "pages_read": pages,
            "total_elements": total_elements,
            "candidates": list(candidates or []),
        }


__all__ = [
    "MARKETPLACE_KEY",
    "BearerSource",
    "SmartStoreCreateSender",
    "SmartStoreReadback",
    "SmartStoreReconcileLookup",
]
