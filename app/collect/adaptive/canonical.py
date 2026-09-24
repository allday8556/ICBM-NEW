"""Canonical JSON and digests for the Adaptive core (ADR-0017 §3, §5.2).

One serialization is used for every content digest: sorted keys, no insignificant whitespace,
UTF-8 text, and **no non-finite number**. ``NaN``, ``Infinity`` and an overflow such as ``1e999``
are refused both when JSON is read and when it is written, so a digest is never computed over a
value JSON cannot represent exactly.
"""

import hashlib
import json
import math
import struct
from collections.abc import Iterable
from typing import Any


class NonFiniteValue(ValueError):
    """A NaN or infinite number: never canonical, never digested."""


def _refuse_constant(name: str) -> Any:
    raise NonFiniteValue(f"non-finite JSON constant {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise NonFiniteValue(f"non-finite JSON number {text[:20]}")
    return value


def parse_json(text: str) -> Any:
    """Strict JSON: non-finite constants and overflowing numbers are refused."""
    return json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except ValueError as error:
        raise NonFiniteValue(str(error)) from None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(scheme: str, payload: Any) -> str:
    """A domain-separated content digest: the scheme is part of what is hashed."""
    return sha256_text(canonical_json({"scheme": scheme, "payload": payload}))


def length_prefixed(parts: Iterable[str]) -> bytes:
    """Each part as a 4-byte big-endian UTF-8 length and then its bytes (ADR-0017 §5.2)."""
    encoded = b""
    for part in parts:
        data = part.encode("utf-8")
        encoded += struct.pack(">I", len(data)) + data
    return encoded
