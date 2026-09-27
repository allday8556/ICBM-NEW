"""SmartStore failure classification (docs/platforms/smartstore/ERRORS.md; M2 PR-A).

The layer is identified before a class is chosen (ERRORS §5). A class comes from the endpoint
contract, the provider code and the request/response evidence, never from a bare HTTP-status
table (§8, Issue #39 §6).

- A provider 404 or any ``*_NOT_FOUND`` code is never ``NOT_FOUND``. That class is ICBM-local only
  (ADR-0008 C).
- Weak evidence is never promoted; when the cause is not established, the class is ``UNKNOWN``
  (§8 Step 5, §18).
- A class never authorizes a replay (§14). PR-A retries nothing: retry budgets are policy-pending
  (§25 Q6).

**The mutation outcome is a separate axis** (ERRORS §2.1 vs §2.2). :func:`definitive_rejection`
answers only "did this response prove the mutation did not happen?", and never selects a cause.
It is the reviewed, endpoint-specific extension of the `NOT_APPLIED_PROVEN` whitelist that the
CREATE adoption slice needs (ERRORS §15; ADR-0014 §10, §28.3; ADR-0018 §6.1).
"""

from dataclasses import dataclass
from enum import StrEnum

from app.core.errors import ErrorClass
from integrations.marketplaces.smartstore.registry import EndpointId
from integrations.marketplaces.smartstore.transmission import Phase


class FailureLayer(StrEnum):
    """ERRORS §5."""

    TRANSPORT = "TRANSPORT"
    GATEWAY = "GATEWAY"
    API_SERVER_STANDARD = "API_SERVER_STANDARD"
    API_SERVER_DOMAIN = "API_SERVER_DOMAIN"
    OPERATION_RESULT = "OPERATION_RESULT"
    LOCAL_CONTRACT = "LOCAL_CONTRACT"


class Basis(StrEnum):
    """ERRORS §7."""

    DOCUMENTED_EXACT = "DOCUMENTED_EXACT"
    DOCUMENTED_CONTEXTUAL = "DOCUMENTED_CONTEXTUAL"
    ENDPOINT_SPECIFIC = "ENDPOINT_SPECIFIC"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Classification:
    error_class: ErrorClass
    layer: FailureLayer
    basis: Basis
    # ICBM's diagnostic code; the provider code is carried separately and never selects state.
    code: str
    provider_code: str | None = None


# ERRORS §9.6: gateway codes whose documented causes are service, network or maintenance failures.
_TRANSIENT_GATEWAY_PREFIXES = ("GW.PROXY", "GW.BLOCK", "GW.TIMEOUT")
_RATE_LIMIT_GATEWAY_CODES = frozenset({"GW.RATE_LIMIT", "GW.QUOTA_LIMIT"})


# ---------------------------------------------------------------- local contract


def local_contract(code: str) -> Classification:
    """A defect in ICBM's own request (ERRORS §5.6): a NOT_ADOPTED endpoint, a malformed
    request, or a call without the session it requires. Retrying cannot fix it (§10.7)."""
    return Classification(
        ErrorClass.FATAL, FailureLayer.LOCAL_CONTRACT, Basis.ENDPOINT_SPECIFIC, code
    )


# ---------------------------------------------------------------- transport


def transport(phase: Phase, *, known_transport_failure: bool) -> Classification:
    """No trustworthy provider response was obtained (ERRORS §5.1, §9.6, §18)."""
    if phase is Phase.EGRESS_BLOCKED:
        # ICBM's own egress policy refused the connection before it left the machine.
        return Classification(
            ErrorClass.POLICY_BLOCKED,
            FailureLayer.TRANSPORT,
            Basis.DOCUMENTED_EXACT,
            "SMARTSTORE_EGRESS_BLOCKED",
        )
    if not known_transport_failure:
        # An unfamiliar exception: the cause is not established.
        return Classification(
            ErrorClass.UNKNOWN,
            FailureLayer.TRANSPORT,
            Basis.UNKNOWN,
            "SMARTSTORE_TRANSPORT_UNKNOWN",
        )
    code = (
        "SMARTSTORE_CONNECT_FAILED"
        if phase in (Phase.DNS_FAILURE, Phase.TCP_CONNECT_FAILURE, Phase.TLS_HANDSHAKE_FAILURE)
        else "SMARTSTORE_EXCHANGE_FAILED"
    )
    return Classification(
        ErrorClass.TRANSIENT, FailureLayer.TRANSPORT, Basis.DOCUMENTED_CONTEXTUAL, code
    )


# ---------------------------------------------------------------- responses


def response(status: int, provider_code: str | None) -> Classification:
    """A provider response that did not pass the endpoint's success predicate (ERRORS §8)."""
    if 300 <= status < 400:
        # EM §6, §11: never followed; kept as contract/error evidence.
        return Classification(
            ErrorClass.UNKNOWN,
            FailureLayer.LOCAL_CONTRACT,
            Basis.ENDPOINT_SPECIFIC,
            "SMARTSTORE_UNEXPECTED_REDIRECT",
            provider_code,
        )
    if 200 <= status < 300:
        # EM §6, §9: a 2xx that fails the predicate is schema/contract drift. It fails closed.
        return Classification(
            ErrorClass.UNKNOWN,
            FailureLayer.OPERATION_RESULT,
            Basis.ENDPOINT_SPECIFIC,
            "SMARTSTORE_SUCCESS_PREDICATE_FAILED",
            provider_code,
        )
    if provider_code is not None and provider_code.startswith("GW."):
        return _gateway(provider_code)
    layer = FailureLayer.API_SERVER_DOMAIN if provider_code else FailureLayer.API_SERVER_STANDARD
    if status == 429:
        return Classification(
            ErrorClass.RATE_LIMITED,
            layer,
            Basis.DOCUMENTED_EXACT,
            "SMARTSTORE_RATE_LIMITED",
            provider_code,
        )
    if status >= 500:
        # ERRORS §10.5: TRANSIENT, bounded by the retry budget.
        return Classification(
            ErrorClass.TRANSIENT,
            layer,
            Basis.DOCUMENTED_CONTEXTUAL,
            "SMARTSTORE_SERVER_ERROR",
            provider_code,
        )
    # 400, 401, 403, 404 and any other 4xx. Without corroborating evidence the cause is not
    # established (ERRORS §10.1-§10.4; EM §6: a token 400 is not automatic credential-invalid
    # truth). A 404 or *_NOT_FOUND code never becomes the ICBM-local NOT_FOUND.
    return Classification(
        ErrorClass.UNKNOWN, layer, Basis.UNKNOWN, f"SMARTSTORE_HTTP_{status}", provider_code
    )


def _gateway(provider_code: str) -> Classification:
    if provider_code in _RATE_LIMIT_GATEWAY_CODES:
        # ERRORS §9.4, §9.5.
        return Classification(
            ErrorClass.RATE_LIMITED,
            FailureLayer.GATEWAY,
            Basis.DOCUMENTED_EXACT,
            "SMARTSTORE_RATE_LIMITED",
            provider_code,
        )
    if provider_code == "GW.INTERNAL_SERVER_ERROR" or provider_code.startswith(
        _TRANSIENT_GATEWAY_PREFIXES
    ):
        return Classification(
            ErrorClass.TRANSIENT,
            FailureLayer.GATEWAY,
            Basis.DOCUMENTED_CONTEXTUAL,
            "SMARTSTORE_GATEWAY_TRANSIENT",
            provider_code,
        )
    # GW.AUTHN (§9.1), GW.IP_NOT_ALLOWED (§9.2), GW.NOT_FOUND on the documented route (§9.3) and
    # any other gateway code: UNKNOWN until the cause is corroborated.
    return Classification(
        ErrorClass.UNKNOWN,
        FailureLayer.GATEWAY,
        Basis.UNKNOWN,
        "SMARTSTORE_GATEWAY_REJECTED",
        provider_code,
    )


# ---------------------------------------------------------------- definitive provider rejection

# ERRORS §15 admits `NOT_APPLIED_PROVEN` only through an explicitly reviewed whitelist, and §15.1's
# baseline covers pre-transmission evidence only. ADR-0014 §10's resolution table also admits
# "another explicitly reviewed machine or provider proof", and §28.3 names one for CREATE: an
# ordinary **definitive provider rejection**, recorded `NOT_APPLIED_PROVEN` on its own Attempt and
# never passing through `UNKNOWN`. This is that review, and it is deliberately narrow.
#
# The reviewed statuses are exactly the API-server request-rejection outcomes ERRORS.md §10
# enumerates for the product API — `BAD_REQUEST` (§10.1), `UNAUTHORIZED` (§10.2), `FORBIDDEN`
# (§10.3), `NOT_FOUND` (§10.4), the 405/415 request-contract failures (§10.7) and 409 (§10.8). In
# each the provider's own complete response says it refused the request, so no product was created.
DEFINITIVE_REJECTION_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 415})
# The review is endpoint-specific, as §15 requires. Only the adopted CREATE contract carries it;
# IMAGE_UPLOAD keeps the ambiguity contract of ADR-0014 §17.1 unchanged.
DEFINITIVE_REJECTION_ENDPOINTS = frozenset({EndpointId.SMARTSTORE_PRODUCT_CREATE_V2})


def definitive_rejection(endpoint_id: EndpointId, status: int, provider_code: str | None) -> bool:
    """Whether one complete provider response proves this request applied nothing.

    Everything outside the reviewed set stays ambiguous, because application cannot be excluded:
    a 2xx that fails the success predicate, a malformed or truncated body, a 3xx (never followed
    for a mutation, ERRORS §10.6), a 408/425/429 and every 5xx (§14.3, §15.2). So does **any**
    gateway-attributed code: ERRORS §25 Q2 — whether a pre-service gateway rejection guarantees no
    mutation reached the target service — is still open, so a `GW.` code proves nothing here.
    """
    if endpoint_id not in DEFINITIVE_REJECTION_ENDPOINTS:
        return False
    if provider_code is not None and provider_code.startswith("GW."):
        return False
    return status in DEFINITIVE_REJECTION_STATUSES
