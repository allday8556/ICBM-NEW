"""The adopted SmartStore CREATE response contract and its outcome classification
(ADR-0014 §9–§11, §28; ADR-0018 §6.1; CREATE adoption slice, ADR-0020 §4 order 1).

Two things live here, and they are deliberately independent axes (ADR-0014 §9):

* **the response contract** — which provider identifiers a successful CREATE may yield, and how a
  success that carries none is refused rather than guessed;
* **the outcome classification** — whether the marketplace mutation happened.

**The response.** The official evidence (``docs/evidence/marketplace-apis/PRODUCT_CREATE.md``
§ SmartStore) proves HTTP ``200`` with ``application/json;charset=UTF-8`` and the identifier
*names* ``originProductNo``, ``smartstoreChannelProductNo`` and ``windowChannelProductNo``. It does
**not** capture their JSON nesting inside the body, and it does not capture their value type. So
this module recognizes them the way the adopted read-back normalizer recognizes a product — by
name, wherever the endpoint's deny-by-default retention profile left them — and fails closed when
the origin number is absent or unusable. No identity is ever invented, and no nesting is asserted.

Because the nesting is uncaptured, the recognizer never *chooses* a reading of the body either. An
identifier name is recognized only when the whole retained response resolves it **unambiguously**:
every occurrence of that name normalizes to one and the same usable value. A name that occurs twice
with different values, or that occurs anywhere with a value this contract does not understand, is
not recognized at all — no occurrence is preferred over another, and no first hit wins. For
``originProductNo`` that is the ``UNKNOWN`` of ``RESPONSE_UNREADABLE``: an ambiguous body is never
evidence of an applied mutation, and never the identity a read-back is then made by.

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
    of non-application), an unsafe redirect, and a ``200`` whose body carries no usable identifier.

``APPLIED_PROVEN`` is reached only through a response that passed the endpoint success predicate
*and* yielded a usable ``originProductNo``. Even then it is not registration success: ADR-0014 §11
confirms a registration only after read-back and Snapshot comparison.

An ``UNKNOWN`` is never resent (ADR-0014 §28, M5-08, G3-07). Nothing in this module retries,
schedules or reopens anything: it classifies one handoff and hands the verdict to the REGISTER
execution owner, which keeps the Intent ``UNKNOWN`` with its conflict scope closed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

CREATE_RESPONSE_CONTRACT_VERSION: Final = "smartstore-create-response/v1"

FIELD_ORIGIN_PRODUCT_NO: Final = "originProductNo"
FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO: Final = "smartstoreChannelProductNo"
FIELD_WINDOW_CHANNEL_PRODUCT_NO: Final = "windowChannelProductNo"

# Every provider identifier the success response may carry. ``windowChannelProductNo`` is kept
# although ICBM never emits ``windowChannelProduct``: a provider identity that did come back is
# never dropped (ADR-0014 §28.2), and a number is safe evidence.
IDENTIFIER_FIELDS: Final[tuple[str, ...]] = (
    FIELD_ORIGIN_PRODUCT_NO,
    FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO,
    FIELD_WINDOW_CHANNEL_PRODUCT_NO,
)

# A provider product number as ICBM will address it again: decimal digits only, bounded. The value
# type is not captured, so both a JSON number and a digit string are accepted and normalized to
# one canonical text; anything else is not an identifier this contract understands.
_MAX_IDENTIFIER_DIGITS: Final = 32

# The refusal an otherwise-successful response earns when it carries no usable origin number.
RESPONSE_UNREADABLE: Final = "SMARTSTORE_CREATE_RESPONSE_UNREADABLE"


@dataclass(frozen=True)
class CreateIdentifiers:
    """The provider identities one CREATE response yielded. ``None`` means "not recognized", never
    "absent at the provider"."""

    origin_product_no: str | None
    smartstore_channel_product_no: str | None
    window_channel_product_no: str | None
    # Identifier names the response did carry but this contract refused to resolve: an unusable
    # value, or two occurrences that disagree. Evidence of why a name is ``None``, never a value.
    unresolved: tuple[str, ...] = ()

    @property
    def readable(self) -> bool:
        """Whether the origin product number — the identity a read-back is made by — came back."""
        return self.origin_product_no is not None

    def canonical(self) -> dict[str, Any]:
        """The sanitized canonical evidence representation of the recognized identities."""
        return {
            "response_contract_version": CREATE_RESPONSE_CONTRACT_VERSION,
            FIELD_ORIGIN_PRODUCT_NO: self.origin_product_no,
            FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO: self.smartstore_channel_product_no,
            FIELD_WINDOW_CHANNEL_PRODUCT_NO: self.window_channel_product_no,
            "unresolved_identifiers": list(self.unresolved),
        }


def _identifier(value: Any) -> str | None:
    """One provider product number, normalized, or ``None`` when the value is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text.isdigit() or len(text) > _MAX_IDENTIFIER_DIGITS:
        return None
    return text


def _nodes(value: Any) -> list[Mapping[str, Any]]:
    """Every mapping inside a retained response, outermost first."""
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        found.append(value)
        for child in value.values():
            found.extend(_nodes(child))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            found.extend(_nodes(item))
    return found


def _resolve(nodes: Sequence[Mapping[str, Any]], name: str) -> tuple[str | None, bool]:
    """The one value ``name`` unambiguously has in the retained response, and whether it occurred.

    Fail-closed, because the nesting is uncaptured: the name is looked for everywhere, but nothing
    is ever selected. Every occurrence must normalize to the same usable value; one unusable
    occurrence, or one disagreement, and the name is not recognized at all.
    """
    occurred = False
    values: set[str] = set()
    for node in nodes:
        if name not in node:
            continue
        occurred = True
        value = _identifier(node[name])
        if value is None:
            return None, True
        values.add(value)
    if len(values) != 1:
        return None, occurred
    return values.pop(), True


def identifiers(retained: Mapping[str, Any]) -> CreateIdentifiers:
    """Recognize the provider identities in one retained CREATE response.

    The retained mapping has already passed the endpoint's deny-by-default retention profile, so
    only allow-listed leaves can be here at all. Each identifier name must resolve to exactly one
    usable value across the whole body; a name that is unusable or contradicted anywhere is not
    recognized, never coerced and never chosen between.
    """
    nodes = _nodes(retained)
    found: dict[str, str | None] = {}
    unresolved: list[str] = []
    for name in IDENTIFIER_FIELDS:
        value, occurred = _resolve(nodes, name)
        found[name] = value
        if value is None and occurred:
            unresolved.append(name)
    return CreateIdentifiers(
        origin_product_no=found[FIELD_ORIGIN_PRODUCT_NO],
        smartstore_channel_product_no=found[FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO],
        window_channel_product_no=found[FIELD_WINDOW_CHANNEL_PRODUCT_NO],
        unresolved=tuple(unresolved),
    )


__all__ = [
    "CREATE_RESPONSE_CONTRACT_VERSION",
    "FIELD_ORIGIN_PRODUCT_NO",
    "FIELD_SMARTSTORE_CHANNEL_PRODUCT_NO",
    "FIELD_WINDOW_CHANNEL_PRODUCT_NO",
    "IDENTIFIER_FIELDS",
    "RESPONSE_UNREADABLE",
    "CreateIdentifiers",
    "identifiers",
]
