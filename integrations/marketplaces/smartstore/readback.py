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
a confirmation.

Only retained, sanitized values reach this module (``retention.py``), so nothing it returns can
carry credential, session or signed material into a durable comparison digest.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from app.register.sanitize import safe_provider_reference
from integrations.marketplaces.smartstore.product import (
    FIELD_NAME,
    FIELD_OPTION_SELLER_CODE,
    FIELD_SALE_PRICE,
    FIELD_SELLER_MANAGEMENT_CODE,
    MAX_SALE_PRICE,
    MAX_STOCK_QUANTITY,
    seller_codes,
)

NORMALIZER_VERSION: Final = "smartstore-readback-normalizer/v1"
COMPARISON_CONTRACT_VERSION: Final = "smartstore-readback-comparison/v1"

_URL_FIELD: Final = "url"
_STOCK_FIELD: Final = "stockQuantity"


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

    def canonical(self) -> dict[str, Any]:
        """The durable, sanitized representation a comparison digest is taken over."""
        return {
            "normalizer_version": self.normalizer_version,
            "seller_management_code": self.seller_management_code,
            "name": self.name,
            "sale_price": self.sale_price,
            "stock_quantity": self.stock_quantity,
            "option_codes": list(self.option_codes),
            "image_references": list(self.image_references),
            "unsafe_image_references": self.unsafe_image_references,
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


def normalize(retained: Mapping[str, Any]) -> NormalizedListing:
    """Recognize one provider listing in a retained read-back response."""
    nodes = _walk(retained)
    name = _first(nodes, FIELD_NAME, str)
    price = _first(nodes, FIELD_SALE_PRICE, int)
    stock = _first(nodes, _STOCK_FIELD, int)
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
    )


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
    return Comparison(
        COMPARISON_CONTRACT_VERSION,
        NORMALIZER_VERSION,
        verdict,
        reasons=tuple(sorted(reasons)),
        missing_option_codes=missing,
        unexpected_option_codes=unexpected,
        normalized=listing.canonical(),
    )
