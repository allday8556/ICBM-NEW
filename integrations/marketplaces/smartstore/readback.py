"""Versioned read-back normalization and comparison (ADR-0014 §11, §15; M5 PR-D).

A provider read is normalized into one canonical form and compared with the **immutable
RegistrationSnapshot** that the Intent named — never with the current Draft or Item, which may
have moved on (kickoff §13). The comparison is identity-first: every Snapshot Item must correspond
to exactly one provider option unit through its ``registration_item_key``, so a listing that
carries only part of a SINGLE unit is one whole-listing MISMATCH, never a missing piece to send
separately (ADR-0014 R3).

**Recognizer, not a claim.** The packet proves the leaf names ``name``, ``salePrice``,
``stockQuantity``, ``sellerManagementCode``, ``sellerManagerCode`` and image ``url``, but no
envelope: an origin read and a channel read may nest the product differently. The normalizer
therefore walks the retained response and recognizes those leaves wherever they sit — an option
unit is an object carrying ``sellerManagerCode`` — and if the required leaves are absent it
returns ``UNREADABLE`` instead of guessing a shape. A 2xx read that cannot be normalized is never
a confirmation. (The stock quantity is the exception since normalizer v3: it is read at exactly
``originProduct.stockQuantity``, below.)

**The published state is read at its documented paths, not recognized.** The origin-product read
documents its 200 response envelope (Commerce API 2.90.0; Issue #89 ``5911962320``): the sale
status is ``originProduct.statusType`` and the SmartStore display status is
``smartstoreChannelProduct.channelProductDisplayStatusType``. Both are read there and nowhere else
— a ``windowChannelProduct`` display status or a status nested elsewhere is never taken for them —
and a value outside the documented enumerations is unreadable.

**A published state is proven only against an explicit expectation** (ADR-0014 §11): the one the
Snapshot's own CREATE projection writes. The sale status is ``SALE``, the only CREATE input; the
display status is ``ON``, the first-vertical publication decision of architect resolution
``5915900049`` D1. So the expected published state is exactly ``SALE/ON``. The comparison reads
both halves, refuses a half that differs, and states ``published_state`` only when both were read
and both equal the expectation. ``SALE/SUSPENSION``, another sale status, a missing half or an
unreadable half never proves it, and the execution owner then refuses to confirm
(``REGISTER_PUBLISHED_STATE_UNPROVEN``). This proves the two documented values only — not buyer
visibility, and not any read-after-write timing.

**The registration stock seed is compared exactly** (architect resolution ``5915900049`` D2.2).
The Snapshot's projection registers ``originProduct.stockQuantity = 1``; the read-back reads the
origin product's own ``stockQuantity`` at exactly that path and a different or missing value is a
mismatch (``STOCK_QUANTITY_MISMATCH``). The seed is not a supplier quantity, and a later sale that
moves the provider's stock before the read-back is a mismatch for review, never a confirmation.
``naverShoppingRegistration`` is not read back: the origin read retains no such member, and the
provider documents storing ``false`` for a non-advertiser, so a read value could not prove ICBM's
intent either way.

Only retained, sanitized values reach this module (``retention.py``), so nothing it returns can
carry credential, session or signed material into a durable comparison digest.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from app.stages.register.sanitize import safe_provider_reference
from integrations.marketplaces.smartstore.product import (
    CREATE_STATUS_TYPE,
    FIELD_CHANNEL_DISPLAY_STATUS,
    FIELD_CHANNEL_PRODUCT,
    FIELD_NAME,
    FIELD_OPTION_SELLER_CODE,
    FIELD_ORIGIN_PRODUCT,
    FIELD_SALE_PRICE,
    FIELD_SELLER_MANAGEMENT_CODE,
    FIELD_STATUS_TYPE,
    FIELD_STOCK_QUANTITY,
    MAX_SALE_PRICE,
    MAX_STOCK_QUANTITY,
    REGISTRATION_DISPLAY_STATUS,
    WireContractError,
    project,
    seller_codes,
)

# v3: the stock quantity is read at exactly originProduct.stockQuantity (5915900049 D2.2).
NORMALIZER_VERSION: Final = "smartstore-readback-normalizer/v3"
# v3: the display status is expected (ON) and compared exactly (architect resolution 5915900049 D1).
# v4: the registration stock seed is expected (1) and compared exactly (D2.2).
COMPARISON_CONTRACT_VERSION: Final = "smartstore-readback-comparison/v4"

# The documented enumerations of the two published-state members (Issue #89 5911962320).
SALE_STATUSES: Final = frozenset(
    {
        "WAIT",
        "SALE",
        "OUTOFSTOCK",
        "UNADMISSION",
        "REJECTION",
        "SUSPENSION",
        "CLOSE",
        "PROHIBITION",
        "DELETE",
    }
)
DISPLAY_STATUSES: Final = frozenset({"WAIT", "ON", "SUSPENSION"})

_URL_FIELD: Final = "url"


class ReadbackVerdict(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    # The response passed the endpoint predicate but carries no recognizable product: fail closed.
    UNREADABLE = "UNREADABLE"


@dataclass(frozen=True)
class NormalizedListing:
    """The canonical form of one provider listing: only proven leaves, sorted and typed."""

    normalizer_version: str
    seller_management_code: str | None
    name: str | None
    sale_price: int | None
    stock_quantity: int | None
    option_codes: tuple[str, ...]
    image_references: tuple[str, ...]
    unsafe_image_references: int
    # The two halves of the published state, each read at its documented path, or ``None``.
    sale_status: str | None = None
    display_status: str | None = None

    def canonical(self) -> dict[str, Any]:
        """The durable, sanitized representation a comparison digest is taken over. It carries
        what was read; ``published_state`` is added by the comparison, only when it is proven."""
        return {
            "normalizer_version": self.normalizer_version,
            "seller_management_code": self.seller_management_code,
            "name": self.name,
            "sale_price": self.sale_price,
            "stock_quantity": self.stock_quantity,
            "option_codes": list(self.option_codes),
            "image_references": list(self.image_references),
            "unsafe_image_references": self.unsafe_image_references,
            "sale_status": self.sale_status,
            "display_status": self.display_status,
        }

    @property
    def readable(self) -> bool:
        """A listing is readable when the provider's own seller code came back: without it no
        correspondence with the Snapshot can be proven at all."""
        return self.seller_management_code is not None


@dataclass(frozen=True)
class Comparison:
    """The result of comparing a normalized read-back with the Snapshot it should reflect."""

    comparison_contract_version: str
    normalizer_version: str
    verdict: ReadbackVerdict
    reasons: tuple[str, ...] = ()
    missing_option_codes: tuple[str, ...] = ()
    unexpected_option_codes: tuple[str, ...] = ()
    normalized: Mapping[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        """The sanitized comparison evidence for ``record_mismatch`` / ``confirm_registration``."""
        return {
            "comparison_contract_version": self.comparison_contract_version,
            "normalizer_version": self.normalizer_version,
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "missing_option_codes": list(self.missing_option_codes),
            "unexpected_option_codes": list(self.unexpected_option_codes),
            "normalized": dict(self.normalized),
        }


def _walk(value: Any) -> list[Mapping[str, Any]]:
    """Every mapping inside a retained response, outermost first."""
    found: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        found.append(value)
        for child in value.values():
            found.extend(_walk(child))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            found.extend(_walk(item))
    return found


def _first(nodes: Sequence[Mapping[str, Any]], key: str, kind: type) -> Any | None:
    for node in nodes:
        value = node.get(key)
        if isinstance(value, kind) and not isinstance(value, bool):
            return value
    return None


def _at(retained: Mapping[str, Any], container: str, leaf: str, allowed: frozenset[str]) -> Any:
    """The documented enumeration value at exactly ``container.leaf``, or ``None``."""
    node = retained.get(container)
    value = node.get(leaf) if isinstance(node, Mapping) else None
    return value if isinstance(value, str) and value in allowed else None


def normalize(retained: Mapping[str, Any]) -> NormalizedListing:
    """Recognize one provider listing in a retained read-back response."""
    nodes = _walk(retained)
    name = _first(nodes, FIELD_NAME, str)
    price = _first(nodes, FIELD_SALE_PRICE, int)
    origin = retained.get(FIELD_ORIGIN_PRODUCT)
    stock = origin.get(FIELD_STOCK_QUANTITY) if isinstance(origin, Mapping) else None
    if isinstance(stock, bool) or not isinstance(stock, int):
        stock = None
    codes = sorted(
        {
            node[FIELD_OPTION_SELLER_CODE]
            for node in nodes
            if isinstance(node.get(FIELD_OPTION_SELLER_CODE), str)
        }
    )
    references, unsafe = [], 0
    for node in nodes:
        reference = node.get(_URL_FIELD)
        if not isinstance(reference, str):
            continue
        # A provider image reference that cannot survive sanitation is counted, never kept.
        if safe_provider_reference(reference):
            if reference not in references:
                references.append(reference)
        else:
            unsafe += 1
    return NormalizedListing(
        normalizer_version=NORMALIZER_VERSION,
        seller_management_code=_first(nodes, FIELD_SELLER_MANAGEMENT_CODE, str),
        name=name,
        # A value outside a proven bound is not a number this contract understands.
        sale_price=price if price is not None and 0 < price <= MAX_SALE_PRICE else None,
        stock_quantity=stock if stock is not None and 0 <= stock <= MAX_STOCK_QUANTITY else None,
        option_codes=tuple(codes),
        image_references=tuple(references),
        unsafe_image_references=unsafe,
        sale_status=_at(retained, FIELD_ORIGIN_PRODUCT, FIELD_STATUS_TYPE, SALE_STATUSES),
        display_status=_at(
            retained, FIELD_CHANNEL_PRODUCT, FIELD_CHANNEL_DISPLAY_STATUS, DISPLAY_STATUSES
        ),
    )


@dataclass(frozen=True)
class ExpectedPublishedState:
    """The published state a Snapshot explicitly expects (ADR-0014 §11). A half that no owner
    states is ``None``: it is never assumed."""

    sale_status: str | None
    display_status: str | None

    @property
    def complete(self) -> bool:
        return self.sale_status is not None and self.display_status is not None


def expected_published_state(snapshot_payload: Mapping[str, Any]) -> ExpectedPublishedState:
    """What this Snapshot expects the listing's published state to be: exactly the two values its
    own frozen CREATE projection writes — ``SALE`` and ``ON`` (5915900049 D1).

    Read from the projected document, never assumed: a Snapshot that cannot be projected expects
    nothing, so no published state can be proven for it.
    """
    try:
        body = project(snapshot_payload).document.mapping()
    except WireContractError:
        return ExpectedPublishedState(sale_status=None, display_status=None)
    origin = body.get(FIELD_ORIGIN_PRODUCT) or {}
    channel = body.get(FIELD_CHANNEL_PRODUCT) or {}
    sale = origin.get(FIELD_STATUS_TYPE)
    display = channel.get(FIELD_CHANNEL_DISPLAY_STATUS)
    return ExpectedPublishedState(
        sale_status=sale if sale in SALE_STATUSES else None,
        display_status=display if display in DISPLAY_STATUSES else None,
    )


def expected_stock_quantity(snapshot_payload: Mapping[str, Any]) -> int | None:
    """The registration stock seed this Snapshot's own projection writes (5915900049 D2.2), or
    ``None`` for a Snapshot that cannot be projected."""
    try:
        body = project(snapshot_payload).document.mapping()
    except WireContractError:
        return None
    origin = body.get(FIELD_ORIGIN_PRODUCT) or {}
    stock = origin.get(FIELD_STOCK_QUANTITY)
    return stock if isinstance(stock, int) and not isinstance(stock, bool) else None


def published_state(listing: NormalizedListing, expected: ExpectedPublishedState) -> str | None:
    """The proven published state, ``<sale status>/<display status>``, or ``None``.

    It exists only when both halves were read at their documented paths, both are explicitly
    expected, and each equals its expectation.
    """
    if (
        not expected.complete
        or listing.sale_status is None
        or listing.display_status is None
        or (listing.sale_status, listing.display_status)
        != (expected.sale_status, expected.display_status)
    ):
        return None
    return f"{listing.sale_status}/{listing.display_status}"


def compare(snapshot_payload: Mapping[str, Any], retained: Mapping[str, Any]) -> Comparison:
    """Compare a retained provider read-back with the frozen Snapshot payload it should reflect.

    The Snapshot is the only expectation: its listing identity is the provider's management code,
    its Items are the option units, its name and pinned price are the values. A single Item listing
    has no option unit of its own, so its own key is the unit.
    """
    listing = normalize(retained)
    expected = seller_codes(snapshot_payload)
    if not listing.readable:
        return Comparison(
            COMPARISON_CONTRACT_VERSION,
            NORMALIZER_VERSION,
            ReadbackVerdict.UNREADABLE,
            reasons=("READBACK_NO_SELLER_MANAGEMENT_CODE",),
            normalized=listing.canonical(),
        )
    reasons: list[str] = []
    if listing.seller_management_code != expected.seller_management_code:
        reasons.append("SELLER_MANAGEMENT_CODE_MISMATCH")
    name = snapshot_payload["name"].get("value")
    if listing.name is None or listing.name != name:
        reasons.append("NAME_MISMATCH")
    price = {int(item["sale_price_krw"]) for item in snapshot_payload["items"]}
    if len(price) == 1:
        if listing.sale_price is None or listing.sale_price != price.pop():
            reasons.append("SALE_PRICE_MISMATCH")
    # With several prices in one listing the comparison stays on the identities: whether an
    # option combination's price is absolute or a difference is not proven, so no price
    # expectation can be stated for a multi-price unit without inventing that semantics.
    elif listing.sale_price is None:
        reasons.append("SALE_PRICE_MISSING")
    if listing.unsafe_image_references:
        # Proof that cannot survive sanitation is not proof (ADR-0014 §15).
        reasons.append("IMAGE_REFERENCE_UNSAFE")
    # §11 "published state: EXACT against the explicitly expected state" — SALE/ON (5915900049 D1).
    # A half that was read and differs from its expectation is a mismatch of the listing. A half
    # that was not read is not a mismatch: no published state is stated, and the execution owner
    # refuses to confirm (REGISTER_PUBLISHED_STATE_UNPROVEN).
    state = expected_published_state(snapshot_payload)
    if (
        state.sale_status is not None
        and listing.sale_status is not None
        and listing.sale_status != state.sale_status
    ):
        reasons.append("SALE_STATUS_MISMATCH")
    if (
        state.display_status is not None
        and listing.display_status is not None
        and listing.display_status != state.display_status
    ):
        reasons.append("DISPLAY_STATUS_MISMATCH")
    # D2.2: the registration seed, EXACT. Unlike a published-state half, a stock quantity that was
    # not read is a mismatch too: the seed is a value this Snapshot sent, like the price.
    seed = expected_stock_quantity(snapshot_payload)
    if seed is not None and listing.stock_quantity != seed:
        reasons.append("STOCK_QUANTITY_MISMATCH")
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    if len(expected.option_codes) > 1 or listing.option_codes:
        missing = tuple(sorted(set(expected.option_codes) - set(listing.option_codes)))
        unexpected = tuple(sorted(set(listing.option_codes) - set(expected.option_codes)))
        if missing:
            # R3: a listing carrying only part of the unit is one whole-listing mismatch.
            reasons.append("OPTION_UNITS_MISSING")
        if unexpected:
            reasons.append("OPTION_UNITS_UNEXPECTED")
    verdict = ReadbackVerdict.MISMATCH if reasons else ReadbackVerdict.MATCH
    normalized = listing.canonical()
    proven = published_state(listing, state)
    if verdict is ReadbackVerdict.MATCH and proven is not None:
        normalized["published_state"] = proven
    return Comparison(
        COMPARISON_CONTRACT_VERSION,
        NORMALIZER_VERSION,
        verdict,
        reasons=tuple(sorted(reasons)),
        missing_option_codes=missing,
        unexpected_option_codes=unexpected,
        normalized=normalized,
    )


def reads_published_state() -> bool:
    """Whether the adopted origin read can carry both halves of a published state: yes — they are
    retained and read at their documented paths."""
    return True


def proves_published_state() -> bool:
    """Whether a read-back comparison can prove a published state at all (ADR-0014 §11).

    It can: the adopted origin read carries both halves at their documented paths
    (:func:`reads_published_state`), and the Snapshot's own projection states both expected
    values — the sale status ``SALE`` and the owned display status ``ON`` (5915900049 D1). Whether
    a given registration *is* proven stays a per-read-back fact: only ``SALE/ON`` read back proves
    it.
    """
    return (
        reads_published_state()
        and CREATE_STATUS_TYPE in SALE_STATUSES
        and REGISTRATION_DISPLAY_STATUS in DISPLAY_STATUSES
    )
