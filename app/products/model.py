"""The PRODUCT DB vocabulary of ADR-0013, and the canonical composition signature.

Pure: no database, no clock, no I/O. The persisted tables (``app.products.models``) repeat these
vocabularies as frozen CHECK literals, so no write path can store a value outside them.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final, Literal

SIGNATURE_VERSION: Final = "composition-signature/v1"
SignatureVersion = Literal["composition-signature/v1"]

# The source product fields a BASE_PRODUCT binding relies on (ADR-0013 §6): the revision must
# state no options and no tiers, and pricing reads its explicit base product price, shipping and
# minimum sale price.
BASE_PRODUCT_ABSENT_FIELDS: Final = ("options", "quantity_tiers")
BASE_PRODUCT_PROVENANCE_FIELDS: Final = (
    "prices",
    "shipping",
    "minimum_sale_price",
    *BASE_PRODUCT_ABSENT_FIELDS,
)


class GroupStatus(StrEnum):
    """A canonical product (ProductGroup) is ACTIVE until a MERGE or SPLIT retires it."""

    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class MemberStatus(StrEnum):
    """Only CONFIRMED membership is canonical; a CANDIDATE or REJECTED member never is."""

    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


# Every status change a member may make. REJECTED is final: a rejected candidate returns to an
# independent group rather than being revived (Canonical v3.1 §6.4).
MEMBER_TRANSITIONS: Final = frozenset(
    {
        (MemberStatus.CANDIDATE, MemberStatus.CONFIRMED),
        (MemberStatus.CANDIDATE, MemberStatus.REJECTED),
        (MemberStatus.CONFIRMED, MemberStatus.REJECTED),
    }
)


class MoveReason(StrEnum):
    """Why the current source revision pointer moved (ADR-0013 §3).

    ``INITIAL`` opens a source product's history. ``NEWER_REVISION`` is the ordinary advance under
    the same extractor. ``EXTRACTOR_CHANGED`` is an advance across an ``extractor_revision``
    change, where fingerprints prove nothing about drift. ``EXPLICIT_DECISION`` is a recorded
    decision, such as moving backwards, which never happens silently.
    """

    INITIAL = "INITIAL"
    NEWER_REVISION = "NEWER_REVISION"
    EXTRACTOR_CHANGED = "EXTRACTOR_CHANGED"
    EXPLICIT_DECISION = "EXPLICIT_DECISION"


class ChangeEventType(StrEnum):
    MERGE = "MERGE"
    SPLIT = "SPLIT"


class BindingKind(StrEnum):
    """ADR-0013 §6. ``SOURCE_OFFER`` is reserved: it needs referential ``SourceSKU`` and
    ``QuantityOffer`` entities, which no accepted scope provides yet, so it cannot be stored."""

    SOURCE_OFFER = "SOURCE_OFFER"
    BASE_PRODUCT = "BASE_PRODUCT"


class CompositionError(ValueError):
    """A composition that cannot be given a canonical structural signature."""


_UNIT_CODE = re.compile(r"^[a-z][a-z0-9]{0,15}$")


@dataclass(frozen=True)
class CompositionSpec:
    """The structure of a seller-chosen sellable multiplicity (ADR-0013 §5).

    It is never a supplier quantity tier. A field the source does not state stays ``None``
    ("unknown"), and "unknown" is part of the signature: no capacity is guessed.
    """

    quantity: int
    unit_amount: str | None = None
    unit_code: str | None = None
    pack_count: int | None = None
    units_per_pack: int | None = None
    total_amount: str | None = None

    @classmethod
    def default_single_unit(cls) -> "CompositionSpec":
        """One of the base product as the source sells it; every unit field unknown."""
        return cls(quantity=1)


def _amount(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation as refused:
        raise CompositionError(f"{name} is not a decimal amount") from refused
    if not number.is_finite() or number <= 0:
        raise CompositionError(f"{name} must be a positive finite amount")
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _count(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CompositionError(f"{name} must be a positive whole number")
    return value


def canonical_structure(spec: CompositionSpec) -> dict[str, object]:
    """The normalized structure the signature is computed over. Display text never enters it."""
    quantity = _count("quantity", spec.quantity)
    unit_amount = _amount("unit_amount", spec.unit_amount)
    unit_code = spec.unit_code
    if unit_code is not None and not _UNIT_CODE.match(unit_code):
        raise CompositionError("unit_code is a lowercase unit token, never free text")
    if (unit_amount is None) != (unit_code is None):
        raise CompositionError("unit_amount and unit_code are stated together or not at all")
    total_amount = _amount("total_amount", spec.total_amount)
    if total_amount is not None and unit_code is None:
        raise CompositionError("total_amount needs the unit it is measured in")
    return {
        "version": SIGNATURE_VERSION,
        "quantity": quantity,
        "unit_amount": unit_amount,
        "unit_code": unit_code,
        "pack_count": _count("pack_count", spec.pack_count),
        "units_per_pack": _count("units_per_pack", spec.units_per_pack),
        "total_amount": total_amount,
    }


def composition_signature(spec: CompositionSpec) -> str:
    """``composition-signature/v1``: SHA-256 over the canonical JSON of the normalized structure.

    Two specs with the same structure, however they were written, get one signature. Count, pack
    and unit differences stay distinct signatures even at equal total weight.
    """
    text = json.dumps(
        canonical_structure(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(text.encode("ascii")).hexdigest()
