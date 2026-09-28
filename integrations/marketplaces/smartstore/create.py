"""The adopted SmartStore CREATE response contract and its outcome classification
(ADR-0014 §9–§11, §28; ADR-0018 §6.1; CREATE adoption slice, ADR-0020 §4 order 1).

Two things live here, and they are deliberately independent axes (ADR-0014 §9):

* **the response contract** — what a successful CREATE response yields ICBM;
* **the outcome classification** — whether the marketplace mutation happened.

**The response.** The official evidence (``docs/evidence/marketplace-apis/PRODUCT_CREATE.md``
§ SmartStore) proves HTTP ``200``, ``application/json;charset=UTF-8`` and the identifier *names*
``originProductNo``, ``smartstoreChannelProductNo`` and ``windowChannelProductNo``. It captures
neither their JSON nesting inside the body nor their value type, and that record's Coverage section
lists both as **not captured**, under a rule this slice does not get to bend: an item that is not
captured is never invented, and anything that would need it stays fail-closed until a later slice
records it from the cited schema.

Reading an identity out of such a body would be exactly that invention — whether by asserting one
nesting, by searching every nesting, or by accepting more than one value type for the same field.
So the adopted contract reads **no** identity out of a CREATE response. The identifier read is a
named gap (:data:`GAP_RESPONSE_IDENTIFIER_SHAPE`): a response that passed the endpoint success
predicate is ``RESPONSE_UNREADABLE``, its retained body is kept as evidence, and nothing further is
claimed about it. The day a slice captures the nesting and the value type from the cited schema,
the gap closes and the read is written then — against the captured shape, never against a guess.

**The outcome.** SmartStore documents no idempotency key, no request-correlation key, no replay
rule and no duplicate-prevention guarantee, and it states nowhere that a timeout, a lost response
or a ``5xx`` proves the mutation was not applied (ADR-0014 §17.2, re-confirmed by Gate 3 area 4).
The classification therefore separates exactly two things:

``NOT_APPLIED_PROVEN``
    only where this request's own transmission evidence shows it could not have reached the
    provider — the whitelist of ``transmission.TRANSMISSION_PRECLUDED``: a local pre-handoff
    refusal, ICBM's egress policy, or a new connection that failed in DNS, TCP connect or the TLS
    handshake before a request byte was written.

``UNKNOWN``
    everything else, with no exception: a read timeout, a lost connection or response, a ``5xx``
    after a possible handoff, an ordinary post-handoff ``4xx`` (architect ruling R2, Issue #89
    comment ``5861607665`` — a definitive-looking rejection received after handoff is **not** proof
    of non-application), an unsafe redirect, and **every** ``200``, because no identity may be read
    from one. An unreadable success is not a failure: the product may well exist.

``APPLIED_PROVEN`` needs a readable provider identity, so it is unreachable while the identifier
read is a gap. That is the conservative direction, and it costs nothing here: the adopted request
is not sendable either while the evidence leaves required values uncaptured (``product.py``). Even
once both close, an applied mutation is still not registration success: ADR-0014 §11 confirms a
registration only after read-back and Snapshot comparison.

An ``UNKNOWN`` is never resent (ADR-0014 §28, M5-08, G3-07). Nothing in this module retries,
schedules or reopens anything: it classifies one handoff and hands the verdict to the REGISTER
execution owner, which keeps the Intent ``UNKNOWN`` with its conflict scope closed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

CREATE_RESPONSE_CONTRACT_VERSION: Final = "smartstore-create-response/v1"

FIELD_ORIGIN_PRODUCT_NO: Final = "originProductNo"
FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO: Final = "smartstoreChannelProductNo"
FIELD_WINDOW_CHANNEL_PRODUCT_NO: Final = "windowChannelProductNo"

# Every provider identifier name the official evidence records for the success response. The names
# are captured; where they sit and what type they carry are not, which is the gap below.
IDENTIFIER_FIELDS: Final[tuple[str, ...]] = (
    FIELD_ORIGIN_PRODUCT_NO,
    FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO,
    FIELD_WINDOW_CHANNEL_PRODUCT_NO,
)

# The gap the captured official evidence leaves in the response, named the way product.py names the
# request gaps. It is never filled with a default, a preferred nesting or a coerced value type.
GAP_RESPONSE_IDENTIFIER_SHAPE: Final = (
    "the success body's JSON nesting of originProductNo, smartstoreChannelProductNo and"
    " windowChannelProductNo, and their value type, are not captured by the official evidence"
    " (PRODUCT_CREATE.md § SmartStore, Coverage), so no provider identity may be read from a"
    " CREATE response"
)

# The refusal every otherwise-successful response earns while that gap stands.
RESPONSE_UNREADABLE: Final = "SMARTSTORE_CREATE_RESPONSE_UNREADABLE"


@dataclass(frozen=True)
class CreateResponse:
    """The adopted reading of one CREATE success: the gaps that keep it unreadable as an identity.

    ``readable`` is ``False`` while any gap stands. It is an invariant of the contract, not of one
    response: no body shape can make it true, because nothing about the shape is captured.
    """

    gaps: tuple[str, ...]

    @property
    def readable(self) -> bool:
        """Whether a provider identity may be read from this response at all.

        ``False`` never means "absent at the provider": the mutation may have been applied. That is
        why an unreadable success is ``UNKNOWN`` and never a failure or a resend.
        """
        return not self.gaps

    def canonical(self) -> dict[str, Any]:
        """The sanitized canonical evidence of what the contract did, and did not, read."""
        return {
            "response_contract_version": CREATE_RESPONSE_CONTRACT_VERSION,
            "identifier_fields": list(IDENTIFIER_FIELDS),
            "identifier_read": "NOT_PROJECTABLE",
            "gaps": list(self.gaps),
        }


def read(retained: Mapping[str, Any]) -> CreateResponse:
    """The adopted reading of one retained CREATE success response.

    ``retained`` has already passed the endpoint's deny-by-default retention profile, so it is safe
    to keep as durable evidence — and it is kept, by the execution seam. It is deliberately **not**
    inspected for an identity here: every way of locating one inside a body whose nesting and value
    type the official evidence does not carry would invent a response semantic (ADR-0014 §17.3;
    ``ENDPOINT_MATRIX.md`` §4.1.1).
    """
    del retained  # nothing may be read from it while GAP_RESPONSE_IDENTIFIER_SHAPE stands
    return CreateResponse(gaps=(GAP_RESPONSE_IDENTIFIER_SHAPE,))


__all__ = [
    "CREATE_RESPONSE_CONTRACT_VERSION",
    "FIELD_ORIGIN_PRODUCT_NO",
    "FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO",
    "FIELD_WINDOW_CHANNEL_PRODUCT_NO",
    "GAP_RESPONSE_IDENTIFIER_SHAPE",
    "IDENTIFIER_FIELDS",
    "RESPONSE_UNREADABLE",
    "CreateResponse",
    "read",
]
