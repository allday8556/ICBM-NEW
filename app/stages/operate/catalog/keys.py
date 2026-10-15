"""The keys and signals that link a store listing to a common product (ADR-0042 §7): pure values.

**Strong keys** (owner ``6190978708`` Q3) link automatically, with a notification and undo:

- the seller code (``sellerManagementCode``), compared exactly after trim and case, and only when
  it has at least :data:`MIN_SELLER_CODE` characters;
- the barcode (``sellerBarcode``), only when it is a valid GTIN (8, 12, 13 or 14 digits whose check
  digit holds), compared as its 14-digit form, so a UPC-A and its EAN-13 are one key.

**Weak signals** only propose (SCP-10): a similar listing name. :func:`name_similarity` is the
share of name tokens two names have in common; it never links anything by itself.

**A conflict** stops a strong key from linking (SCP-11): the two sides state a different capacity,
count or pack in their names or options (``500ml`` against ``1L``, ``2개`` against ``3개``). It is
shown, never resolved here.
"""

import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Final

SELLER_CODE: Final = "SELLER_CODE"
GTIN: Final = "GTIN"
MIN_SELLER_CODE: Final = 4
GTIN_LENGTHS: Final = frozenset({8, 12, 13, 14})

# The units a capacity, count or pack is stated in, each mapped to one comparable dimension and
# the factor that brings it to that dimension's base unit.
_UNITS: Final[dict[str, tuple[str, Decimal]]] = {
    "ml": ("volume", Decimal(1)),
    "l": ("volume", Decimal(1000)),
    "리터": ("volume", Decimal(1000)),
    "mg": ("weight", Decimal("0.001")),
    "g": ("weight", Decimal(1)),
    "kg": ("weight", Decimal(1000)),
    "그램": ("weight", Decimal(1)),
    "개입": ("count", Decimal(1)),
    "개": ("count", Decimal(1)),
    "ea": ("count", Decimal(1)),
    "pcs": ("count", Decimal(1)),
    "입": ("count", Decimal(1)),
    "팩": ("pack", Decimal(1)),
    "봉": ("pack", Decimal(1)),
    "박스": ("box", Decimal(1)),
    "box": ("box", Decimal(1)),
    "세트": ("set", Decimal(1)),
    "set": ("set", Decimal(1)),
    "병": ("bottle", Decimal(1)),
    "캔": ("can", Decimal(1)),
    "정": ("tablet", Decimal(1)),
    "캡슐": ("capsule", Decimal(1)),
    "포": ("sachet", Decimal(1)),
    "매": ("sheet", Decimal(1)),
    "롤": ("roll", Decimal(1)),
}
# Longest first, so ``개입`` is read before ``개`` and ``kg`` before ``g``.
_UNIT_PATTERN: Final = "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True))
_AMOUNT: Final = re.compile(
    rf"(\d+(?:[.,]\d+)?)\s*({_UNIT_PATTERN})(?![a-z])",
    re.IGNORECASE,
)
_TOKEN: Final = re.compile(r"[0-9a-z가-힣]+")


def seller_code_key(value: str | None) -> str | None:
    """A seller code as a strong key: trimmed and case-folded, at least four characters."""
    if value is None:
        return None
    text = value.strip().casefold()
    return text if len(text) >= MIN_SELLER_CODE else None


def gtin_key(value: str | None) -> str | None:
    """A barcode as a strong key: a valid GTIN, as its 14-digit form; anything else is none."""
    if value is None:
        return None
    digits = value.strip()
    if not digits.isascii() or not digits.isdigit() or len(digits) not in GTIN_LENGTHS:
        return None
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    if (10 - total % 10) % 10 != check:
        return None
    return digits.rjust(14, "0")


def strong_keys(
    seller_codes: Iterable[str | None], barcodes: Iterable[str | None]
) -> frozenset[tuple[str, str]]:
    """Every strong key one listing states, as ``(kind, normalized value)``."""
    keys = {(SELLER_CODE, code) for code in map(seller_code_key, seller_codes) if code}
    keys |= {(GTIN, gtin) for gtin in map(gtin_key, barcodes) if gtin}
    return frozenset(keys)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def name_tokens(name: str | None) -> frozenset[str]:
    """The comparable words of a listing name: NFKC, case-folded, letters and digits only."""
    if not name:
        return frozenset()
    return frozenset(token for token in _TOKEN.findall(_fold(name)) if len(token) >= 2)


def name_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """The share of tokens two names have in common (Jaccard). A proposal only, never a link."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def amounts(texts: Iterable[str | None]) -> dict[str, frozenset[Decimal]]:
    """Each dimension the texts state an amount in, with every amount stated, in base units."""
    found: dict[str, set[Decimal]] = {}
    for text in texts:
        if not text:
            continue
        for number, unit in _AMOUNT.findall(_fold(text)):
            dimension, factor = _UNITS[unit.casefold()]
            try:
                value = Decimal(number.replace(",", ".")) * factor
            except InvalidOperation:  # pragma: no cover - the pattern admits digits only
                continue
            found.setdefault(dimension, set()).add(value.normalize())
    return {dimension: frozenset(values) for dimension, values in found.items()}


def conflicts(
    left: dict[str, frozenset[Decimal]], right: dict[str, frozenset[Decimal]]
) -> tuple[str, ...]:
    """The dimensions both sides state with no amount in common: a capacity, count or pack
    conflict (ADR-0013 §4, ADR-0042 §7). A dimension only one side states is no conflict."""
    return tuple(
        sorted(
            dimension
            for dimension in left.keys() & right.keys()
            if not left[dimension] & right[dimension]
        )
    )


__all__ = [
    "GTIN",
    "MIN_SELLER_CODE",
    "SELLER_CODE",
    "amounts",
    "conflicts",
    "gtin_key",
    "name_similarity",
    "name_tokens",
    "seller_code_key",
    "strong_keys",
]
