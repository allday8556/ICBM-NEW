"""Pure Common Sales Option authoring contract (ADR-0013 owner amendment A–D).

This module owns no Product Fact mapping and creates no Atomic SKU. It only validates and signs
one provider-neutral option structure that later slices may map from facts and materialize into
source-proven configurations.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

COMMON_OPTION_SIGNATURE_VERSION: Final = "common-sales-option-signature/v1"

_SEMANTIC_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_UNIT_CODE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class CommonSalesOptionError(ValueError):
    """An option structure cannot be canonicalized without guessing or losing identity."""


@dataclass(frozen=True)
class CommonSalesOptionValueSpec:
    canonical_value: str
    display_value: str
    unit_code: str | None = None


@dataclass(frozen=True)
class CommonSalesOptionAxisSpec:
    semantic_key: str
    display_name: str
    values: tuple[CommonSalesOptionValueSpec, ...]


@dataclass(frozen=True)
class CanonicalCommonSalesOptionValue:
    canonical_value: str
    display_value: str
    unit_code: str | None


@dataclass(frozen=True)
class CanonicalCommonSalesOptionAxis:
    semantic_key: str
    display_name: str
    values: tuple[CanonicalCommonSalesOptionValue, ...]


def _text(name: str, value: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise CommonSalesOptionError(f"{name} must be present")
    if len(normalized) > maximum:
        raise CommonSalesOptionError(f"{name} exceeds {maximum} characters")
    return normalized


def canonical_common_sales_options(
    axes: tuple[CommonSalesOptionAxisSpec, ...],
) -> tuple[CanonicalCommonSalesOptionAxis, ...]:
    """Validate and normalize one authored option structure while preserving authored order."""
    if not axes:
        raise CommonSalesOptionError("a common sales option revision needs at least one axis")
    seen_axes: set[str] = set()
    normalized_axes: list[CanonicalCommonSalesOptionAxis] = []
    for axis in axes:
        semantic_key = _text("semantic_key", axis.semantic_key, maximum=64)
        if _SEMANTIC_KEY.fullmatch(semantic_key) is None:
            raise CommonSalesOptionError("semantic_key must be a lowercase canonical token")
        if semantic_key in seen_axes:
            raise CommonSalesOptionError(f"duplicate semantic axis: {semantic_key}")
        seen_axes.add(semantic_key)
        display_name = _text("display_name", axis.display_name, maximum=100)
        if not axis.values:
            raise CommonSalesOptionError(f"axis {semantic_key} needs at least one value")
        seen_values: set[tuple[str, str | None]] = set()
        normalized_values: list[CanonicalCommonSalesOptionValue] = []
        for value in axis.values:
            canonical_value = _text("canonical_value", value.canonical_value, maximum=200)
            display_value = _text("display_value", value.display_value, maximum=200)
            unit_code = None if value.unit_code is None else value.unit_code.strip()
            if unit_code == "":
                unit_code = None
            if unit_code is not None and _UNIT_CODE.fullmatch(unit_code) is None:
                raise CommonSalesOptionError("unit_code must be a lowercase canonical token")
            identity = (canonical_value, unit_code)
            if identity in seen_values:
                raise CommonSalesOptionError(
                    f"axis {semantic_key} repeats canonical value {canonical_value!r}"
                )
            seen_values.add(identity)
            normalized_values.append(
                CanonicalCommonSalesOptionValue(canonical_value, display_value, unit_code)
            )
        normalized_axes.append(
            CanonicalCommonSalesOptionAxis(semantic_key, display_name, tuple(normalized_values))
        )
    return tuple(normalized_axes)


def common_sales_option_signature(
    axes: tuple[CanonicalCommonSalesOptionAxis, ...],
) -> str:
    payload = {
        "version": COMMON_OPTION_SIGNATURE_VERSION,
        "axes": [
            {
                "semantic_key": axis.semantic_key,
                "display_name": axis.display_name,
                "values": [
                    {
                        "canonical_value": value.canonical_value,
                        "display_value": value.display_value,
                        "unit_code": value.unit_code,
                    }
                    for value in axis.values
                ],
            }
            for axis in axes
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


__all__ = [
    "COMMON_OPTION_SIGNATURE_VERSION",
    "CanonicalCommonSalesOptionAxis",
    "CanonicalCommonSalesOptionValue",
    "CommonSalesOptionAxisSpec",
    "CommonSalesOptionError",
    "CommonSalesOptionValueSpec",
    "canonical_common_sales_options",
    "common_sales_option_signature",
]
