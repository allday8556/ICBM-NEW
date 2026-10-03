"""Reviewed Product Fact -> Common Sales Option correspondence.

The mapping names exact immutable ProductFactsRevision JSON leaves.  It does not infer an axis,
value, unit, or option combination; a reviewer explicitly binds each fact leaf to one target in
an already-authored Common Sales Option revision.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Final

FACT_MAPPING_SIGNATURE_VERSION: Final = "common-option-fact-mapping-signature/v1"

_FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_JSON_PATH = re.compile(r"^\$(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[[0-9]+\]))+$")


class CommonOptionFactMappingError(ValueError):
    """A reviewed correspondence is incomplete, ambiguous, or not an exact fact reference."""


@dataclass(frozen=True)
class ProductFactPointer:
    revision_id: str
    field_key: str
    json_path: str


@dataclass(frozen=True)
class CommonOptionValueFactMappingSpec:
    value_id: str
    source: ProductFactPointer


@dataclass(frozen=True)
class CommonOptionAxisFactMappingSpec:
    axis_id: str
    source: ProductFactPointer
    values: tuple[CommonOptionValueFactMappingSpec, ...]


def validate_fact_pointer(pointer: ProductFactPointer) -> ProductFactPointer:
    revision_id = pointer.revision_id.strip()
    field_key = pointer.field_key.strip()
    json_path = pointer.json_path.strip()
    if not revision_id or len(revision_id) > 36:
        raise CommonOptionFactMappingError("revision_id must contain 1 to 36 characters")
    if _FIELD_KEY.fullmatch(field_key) is None:
        raise CommonOptionFactMappingError("field_key must be a lowercase canonical token")
    if len(json_path) > 300 or _JSON_PATH.fullmatch(json_path) is None:
        raise CommonOptionFactMappingError("json_path must name a bounded object key/index path")
    return ProductFactPointer(revision_id, field_key, json_path)


def fact_leaf(value_json: str, json_path: str) -> str | int:
    """Resolve the deliberately small SQLite-compatible JSON path grammar.

    Common option evidence is a source-authored textual or integer leaf.  Objects, arrays, null,
    booleans and floating-point values do not silently become option labels.
    """
    current: object = json.loads(value_json)
    cursor = 1
    while cursor < len(json_path):
        if json_path[cursor] == ".":
            end = cursor + 1
            while end < len(json_path) and json_path[end] not in ".[":
                end += 1
            key = json_path[cursor + 1 : end]
            if not isinstance(current, dict) or key not in current:
                raise CommonOptionFactMappingError(f"json_path has no object key {key!r}")
            current = current[key]
            cursor = end
            continue
        end = json_path.find("]", cursor)
        index = int(json_path[cursor + 1 : end])
        if not isinstance(current, list) or index >= len(current):
            raise CommonOptionFactMappingError(f"json_path has no array index {index}")
        current = current[index]
        cursor = end + 1
    if isinstance(current, bool) or not isinstance(current, (str, int)):
        raise CommonOptionFactMappingError("a mapped Product Fact leaf must be text or an integer")
    if isinstance(current, str) and not current.strip():
        raise CommonOptionFactMappingError("a mapped Product Fact text leaf must be present")
    return current


def canonical_fact_leaf_json(value: str | int) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def common_option_fact_mapping_signature(payload: list[dict[str, object]]) -> str:
    encoded = json.dumps(
        {"version": FACT_MAPPING_SIGNATURE_VERSION, "axes": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


__all__ = [
    "FACT_MAPPING_SIGNATURE_VERSION",
    "CommonOptionAxisFactMappingSpec",
    "CommonOptionFactMappingError",
    "CommonOptionValueFactMappingSpec",
    "ProductFactPointer",
    "canonical_fact_leaf_json",
    "common_option_fact_mapping_signature",
    "fact_leaf",
    "validate_fact_pointer",
]
