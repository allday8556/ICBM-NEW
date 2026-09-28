"""The adopted SmartStore CREATE response contract and its outcome classification
(ADR-0014 §9–§11, §28; ADR-0018 §6.1; CREATE adoption slice, ADR-0020 §4 order 1).

Two things live here, and they are deliberately independent axes (ADR-0014 §9):

* **the response contract** — what a successful CREATE response yields ICBM;
* **the outcome classification** — whether the marketplace mutation happened.

**The response.** The official evidence (``docs/evidence/marketplace-apis/PRODUCT_CREATE.md``
§ SmartStore) proves HTTP ``200`` and ``application/json;charset=UTF-8``, and the value-level packet
``NAVER-P0-VALUES-CREATE-289`` (Issue #89 comment ``5868542027``, E3) proves where the success
identifiers sit and what they are: ``originProductNo``, ``smartstoreChannelProductNo`` and
``windowChannelProductNo`` are **direct top-level members** of the success object, each an
``integer<int64>``. The read is written against exactly that shape and nothing wider:

* only the **top-level** key is read — nested objects are never searched, so an ``originProductNo``
  echoed inside ``originProduct`` or wrapped in any envelope is never an identity;
* a value is an identifier only when it is a JSON integer (a Python ``int``) inside the signed
  64-bit range — a ``bool`` is refused explicitly (Python counts it as an ``int``), and a numeric
  string, a float or any other type is refused, because NAVER documents no such representation;
* ``originProductNo`` and ``smartstoreChannelProductNo`` are both required for a readable response;
  ``windowChannelProductNo`` belongs to the Shopping Window channel ICBM never emits, so its
  **absence alone** never makes the documented SmartStore success unreadable — but a value that is
  present and is not an ``integer<int64>`` is a response the evidence does not describe, and fails
  closed like any other malformed identifier.

A response that misses a required identifier, or carries any identifier in another type, is
``RESPONSE_UNREADABLE``: its retained body is kept as evidence, and nothing further is claimed.

**The outcome.** SmartStore documents no idempotency key, no request-correlation key, no replay
rule and no duplicate-prevention guarantee, and it states nowhere that a timeout, a lost response
or a ``5xx`` proves the mutation was not applied (ADR-0014 §17.2, re-confirmed by Gate 3 area 4).
The classification therefore separates exactly three things:

``APPLIED_PROVEN``
    a ``200`` that passed the endpoint success predicate **and** whose documented identifiers are
    readable under this contract. It is provider-side evidence that the mutation was applied and
    names the identity a read-back is made by — and **nothing more**: it is never a registration
    success. ADR-0014 §11 confirms a registration only after read-back and Snapshot comparison,
    which the REGISTER execution owner performs separately.

``NOT_APPLIED_PROVEN``
    only where this request's own transmission evidence shows it could not have reached the
    provider — the whitelist of ``transmission.TRANSMISSION_PRECLUDED``: a local pre-handoff
    refusal, ICBM's egress policy, or a new connection that failed in DNS, TCP connect or the TLS
    handshake before a request byte was written. An unreadable success is **never** here.

``UNKNOWN``
    everything else, with no exception: a read timeout, a lost connection or response, a ``5xx``
    after a possible handoff, an ordinary post-handoff ``4xx`` (architect ruling R2, Issue #89
    comment ``5861607665`` — a definitive-looking rejection received after handoff is **not** proof
    of non-application), an unsafe redirect, and a ``200`` whose identifiers are unreadable. An
    unreadable success is not a failure: the product may well exist.

An ``UNKNOWN`` is never resent (ADR-0014 §28, M5-08, G3-07). Nothing in this module retries,
schedules or reopens anything: it classifies one handoff and hands the verdict to the REGISTER
execution owner, which keeps the Intent ``UNKNOWN`` with its conflict scope closed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

CREATE_RESPONSE_CONTRACT_VERSION: Final = "smartstore-create-response/v2"

FIELD_ORIGIN_PRODUCT_NO: Final = "originProductNo"
FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO: Final = "smartstoreChannelProductNo"
FIELD_WINDOW_CHANNEL_PRODUCT_NO: Final = "windowChannelProductNo"

# Every provider identifier the official evidence records for the success response, each a
# top-level integer<int64> (NAVER-P0-VALUES-CREATE-289, E3).
IDENTIFIER_FIELDS: Final[tuple[str, ...]] = (
    FIELD_ORIGIN_PRODUCT_NO,
    FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO,
    FIELD_WINDOW_CHANNEL_PRODUCT_NO,
)
# The two a documented SmartStore-only success must carry to be readable. The Shopping Window
# identifier may be absent for a SmartStore-only CREATE, so it is never required.
REQUIRED_IDENTIFIER_FIELDS: Final[tuple[str, ...]] = (
    FIELD_ORIGIN_PRODUCT_NO,
    FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO,
)

# integer<int64>: the signed 64-bit range.
INT64_MIN: Final = -(2**63)
INT64_MAX: Final = 2**63 - 1

# Why one identifier could not be read. Neither ever means "absent at the provider".
IDENTIFIER_MISSING: Final = "MISSING"
IDENTIFIER_NOT_INT64: Final = "NOT_INT64"

# The refusal a success earns when its documented identifiers cannot be read.
RESPONSE_UNREADABLE: Final = "SMARTSTORE_CREATE_RESPONSE_UNREADABLE"


def _int64(value: object) -> int | None:
    """``value`` when it is a JSON integer inside the signed 64-bit range, else ``None``.

    ``bool`` is refused explicitly because Python counts it as an ``int``; a numeric string, a
    float or any other type is refused because NAVER documents no representation but the integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if INT64_MIN <= value <= INT64_MAX else None


@dataclass(frozen=True)
class CreateResponse:
    """The adopted reading of one CREATE success.

    ``problems`` names every documented identifier that could not be read, as
    ``"<field>: <reason>"``; the response is ``readable`` only when there is none and both required
    identifiers were read.
    """

    origin_product_no: int | None
    smartstore_channel_product_no: int | None
    window_channel_product_no: int | None
    problems: tuple[str, ...]

    @property
    def readable(self) -> bool:
        """Whether the documented provider identity was read from this response.

        ``False`` never means "absent at the provider": the mutation may have been applied. That is
        why an unreadable success is ``UNKNOWN`` and never a failure or a resend.
        """
        return (
            not self.problems
            and self.origin_product_no is not None
            and self.smartstore_channel_product_no is not None
        )

    @property
    def marketplace_product_id(self) -> str | None:
        """The identity a read-back is made by: ``originProductNo``, only when readable.

        The adopted origin read-back addresses ``/v2/products/origin-products/{originProductNo}``,
        so this is the one identifier REGISTER carries forward. It proves application, never
        registration success (ADR-0014 §11).
        """
        return str(self.origin_product_no) if self.readable else None

    def canonical(self) -> dict[str, Any]:
        """The sanitized canonical evidence of what the contract read, and what it could not."""
        read = {
            name: value
            for name, value in (
                (FIELD_ORIGIN_PRODUCT_NO, self.origin_product_no),
                (FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO, self.smartstore_channel_product_no),
                (FIELD_WINDOW_CHANNEL_PRODUCT_NO, self.window_channel_product_no),
            )
            if value is not None
        }
        return {
            "response_contract_version": CREATE_RESPONSE_CONTRACT_VERSION,
            "identifier_fields": list(IDENTIFIER_FIELDS),
            "identifier_read": "READ" if self.readable else "UNREADABLE",
            "identifiers": read,
            "problems": list(self.problems),
        }


def read(retained: Mapping[str, Any]) -> CreateResponse:
    """The adopted reading of one retained CREATE success response.

    ``retained`` has already passed the endpoint's deny-by-default retention profile, so it is safe
    to keep as durable evidence — and it is kept, by the execution seam. Only its **top-level**
    members are consulted; nothing nested is ever searched (E3).
    """
    top: Mapping[str, Any] = retained if isinstance(retained, Mapping) else {}
    values: dict[str, int | None] = {}
    problems: list[str] = []
    for name in IDENTIFIER_FIELDS:
        if name not in top:
            values[name] = None
            if name in REQUIRED_IDENTIFIER_FIELDS:
                problems.append(f"{name}: {IDENTIFIER_MISSING}")
            continue
        value = _int64(top[name])
        values[name] = value
        if value is None:
            problems.append(f"{name}: {IDENTIFIER_NOT_INT64}")
    return CreateResponse(
        origin_product_no=values[FIELD_ORIGIN_PRODUCT_NO],
        smartstore_channel_product_no=values[FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO],
        window_channel_product_no=values[FIELD_WINDOW_CHANNEL_PRODUCT_NO],
        problems=tuple(problems),
    )


__all__ = [
    "CREATE_RESPONSE_CONTRACT_VERSION",
    "FIELD_ORIGIN_PRODUCT_NO",
    "FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO",
    "FIELD_WINDOW_CHANNEL_PRODUCT_NO",
    "IDENTIFIER_FIELDS",
    "IDENTIFIER_MISSING",
    "IDENTIFIER_NOT_INT64",
    "INT64_MAX",
    "INT64_MIN",
    "REQUIRED_IDENTIFIER_FIELDS",
    "RESPONSE_UNREADABLE",
    "CreateResponse",
    "read",
]
