"""Source-proven Atomic SKU specifications and canonical signatures."""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

ATOMIC_SKU_SIGNATURE_VERSION: Final = "atomic-sku-set-signature/v2"
type AtomicSKUSemanticSelection = tuple[str, str, str]

_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_JSON_PATH = re.compile(r"^\$(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[[0-9]+\]))+$")


class AtomicSKUError(ValueError):
    """A source configuration is incomplete, ambiguous, or fabricated."""


@dataclass(frozen=True)
class AtomicSKUSelectionSpec:
    axis_id: str
    value_id: str
    source_json_path: str


@dataclass(frozen=True)
class AtomicSKUConfigurationSpec:
    source_revision_id: str
    source_field_key: str
    source_configuration_path: str
    selections: tuple[AtomicSKUSelectionSpec, ...]


@dataclass(frozen=True)
class SourceConfiguration:
    canonical_json: str
    selections: tuple[str, ...]
    supplier_sku_id: str | None


def validate_configuration_spec(spec: AtomicSKUConfigurationSpec) -> AtomicSKUConfigurationSpec:
    revision_id = spec.source_revision_id.strip()
    field_key = spec.source_field_key.strip()
    path = spec.source_configuration_path.strip()
    if not revision_id or len(revision_id) > 36:
        raise AtomicSKUError("source_revision_id must contain 1 to 36 characters")
    if _FIELD_KEY.fullmatch(field_key) is None:
        raise AtomicSKUError("source_field_key must be a lowercase canonical token")
    if len(path) > 300 or _JSON_PATH.fullmatch(path) is None:
        raise AtomicSKUError("source_configuration_path must be a bounded JSON path")
    if not spec.selections:
        raise AtomicSKUError("a source configuration must select at least one value")
    normalized: list[AtomicSKUSelectionSpec] = []
    for selection in spec.selections:
        source_path = selection.source_json_path.strip()
        if not selection.axis_id.strip() or not selection.value_id.strip():
            raise AtomicSKUError("every selection must name an axis and value")
        if len(source_path) > 300 or _JSON_PATH.fullmatch(source_path) is None:
            raise AtomicSKUError("selection source_json_path must be a bounded JSON path")
        normalized.append(
            AtomicSKUSelectionSpec(
                selection.axis_id.strip(), selection.value_id.strip(), source_path
            )
        )
    return AtomicSKUConfigurationSpec(revision_id, field_key, path, tuple(normalized))


def _json_path_value(value: object, path: str) -> object:
    current = value
    cursor = 1
    while cursor < len(path):
        if path[cursor] == ".":
            end = cursor + 1
            while end < len(path) and path[end] not in ".[":
                end += 1
            key = path[cursor + 1 : end]
            if not isinstance(current, dict) or key not in current:
                raise AtomicSKUError(f"JSON path has no object key {key!r}")
            current = current[key]
            cursor = end
            continue
        end = path.find("]", cursor)
        index = int(path[cursor + 1 : end])
        if not isinstance(current, list) or index >= len(current):
            raise AtomicSKUError(f"JSON path has no array index {index}")
        current = current[index]
        cursor = end + 1
    return current


def source_configuration(value_json: str, path: str) -> SourceConfiguration:
    value = _json_path_value(json.loads(value_json), path)
    if not isinstance(value, dict):
        raise AtomicSKUError("source_configuration_path must name one configuration object")
    selections = value.get("selections")
    if (
        not isinstance(selections, list)
        or not selections
        or any(not isinstance(item, str) or not item.strip() for item in selections)
    ):
        raise AtomicSKUError("a source configuration needs present textual selections")
    supplier_sku_id = value.get("supplier_sku_id")
    if supplier_sku_id is not None and (
        not isinstance(supplier_sku_id, str)
        or not supplier_sku_id.strip()
        or len(supplier_sku_id) > 200
    ):
        raise AtomicSKUError("supplier_sku_id must be absent or 1 to 200 characters")
    return SourceConfiguration(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        tuple(selections),
        supplier_sku_id,
    )


def source_selection(value_json: str, path: str) -> str:
    value = _json_path_value(json.loads(value_json), path)
    if not isinstance(value, str) or not value.strip():
        raise AtomicSKUError("an Atomic SKU selection must be present source text")
    return value


def canonical_json_text(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def atomic_sku_selection_signature(
    selections: tuple[AtomicSKUSemanticSelection, ...],
) -> str:
    canonical = tuple(sorted(selections))
    encoded = json.dumps(
        {"version": ATOMIC_SKU_SIGNATURE_VERSION, "selections": canonical},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def atomic_sku_set_signature(payload: list[dict[str, object]]) -> str:
    encoded = json.dumps(
        {"version": ATOMIC_SKU_SIGNATURE_VERSION, "atomic_skus": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


__all__ = [
    "ATOMIC_SKU_SIGNATURE_VERSION",
    "AtomicSKUConfigurationSpec",
    "AtomicSKUError",
    "AtomicSKUSelectionSpec",
    "AtomicSKUSemanticSelection",
    "SourceConfiguration",
    "atomic_sku_selection_signature",
    "atomic_sku_set_signature",
    "canonical_json_text",
    "source_configuration",
    "source_selection",
    "validate_configuration_spec",
]
