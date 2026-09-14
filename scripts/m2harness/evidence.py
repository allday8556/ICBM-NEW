"""Sanitized, schema-bound M2 acceptance evidence (Issue #46 §2.2; docs/acceptance/M2.md §7).

What may be written is defined by ``docs/acceptance/evidence/m2-campaign-evidence.schema.json``.
Every object there is closed (``additionalProperties: false``), and every string is an enum, a
fixed pattern (ids, labels, timestamps, SHAs, hex fingerprints) or bounded text. The writer then
refuses the document if a known secret appears in the rendered bytes, in any encoding the
artifact scanner knows: the client secret, the full client id, a bearer token, an account uid or
an account id. It also refuses any string that carries a bearer credential. A refused document is
not written at all, and the refusal names neither the value nor where it was.

Fingerprints are HMAC-SHA256 under a per-campaign key kept in the campaign's secret store, never
in evidence. They are non-reversible, and cannot be correlated across campaigns or with anything
outside this one. No token value is ever fingerprinted.
"""

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.system.secret_scan import variants

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "docs" / "acceptance" / "evidence" / "m2-campaign-evidence.schema.json"
SCHEMA_VERSION = "m2-campaign-evidence/v1"
_BEARER = re.compile(rb"(?i)bearer[ \t]+[\x21-\x7e]{8,}")
_MIN_KEY_BYTES = 32


class EvidenceRejected(ValueError):
    """The document was not written: it breaks the schema or would disclose a secret."""


class Fingerprinter:
    def __init__(self, key: bytes) -> None:
        if len(key) < _MIN_KEY_BYTES:
            raise ValueError("the campaign fingerprint key must be 256-bit")
        self._key = key

    def account(self, account_uid: str) -> str:
        return self._mac("account", account_uid)

    def application(self, client_id: str) -> str:
        return self._mac("application", client_id)

    def _mac(self, domain: str, value: str) -> str:
        material = f"icbm-m2-acceptance/{domain}\x00{value}".encode()
        return hmac.new(self._key, material, hashlib.sha256).hexdigest()


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return schema


# ---------------------------------------------------------------- the schema subset it uses


def validate(document: object, schema: Mapping[str, Any] | None = None) -> list[str]:
    """Violations of the evidence schema, by JSON path. Values are never echoed."""
    root = schema if schema is not None else load_schema()
    errors: list[str] = []
    _check(document, root, "$", root, errors)
    return errors


def _resolve(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported schema reference {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return dict(node)


def _is(value: object, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    raise ValueError(f"unsupported schema type {kind}")


def _check(
    value: object, schema: Mapping[str, Any], path: str, root: Mapping[str, Any], errors: list[str]
) -> None:
    if "$ref" in schema:
        schema = _resolve(root, schema["$ref"])
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        errors.append(f"{path}: not the fixed value")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not an allowed value")
        return
    if "type" in schema:
        kinds = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
        if not any(_is(value, kind) for kind in kinds):
            errors.append(f"{path}: wrong type")
            return
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: does not match its pattern")
        if len(value) > schema.get("maxLength", 10_000):
            errors.append(f"{path}: too long")
    elif isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below its minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above its maximum")
    elif isinstance(value, dict):
        _check_object(value, schema, path, root, errors)
    elif isinstance(value, list):
        if len(value) > schema.get("maxItems", 100_000):
            errors.append(f"{path}: too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                _check(item, schema["items"], f"{path}[{index}]", root, errors)


def _check_object(
    value: dict[Any, Any],
    schema: Mapping[str, Any],
    path: str,
    root: Mapping[str, Any],
    errors: list[str],
) -> None:
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in value:
            errors.append(f"{path}: missing {name}")
    extra = schema.get("additionalProperties", True)
    names = schema.get("propertyNames")
    for name, item in value.items():
        if names is not None and not (isinstance(name, str) and re.search(names["pattern"], name)):
            errors.append(f"{path}: a field name does not match its pattern")
        elif name in properties:
            _check(item, properties[name], f"{path}.{name}", root, errors)
        elif extra is False:
            errors.append(f"{path}: an unexpected field")
        elif isinstance(extra, dict):
            _check(item, extra, f"{path}.*", root, errors)


# ---------------------------------------------------------------- the writer


class EvidenceWriter:
    def __init__(
        self, secrets: Mapping[str, str] | None = None, *, schema: Mapping[str, Any] | None = None
    ) -> None:
        # Labels are neutral names ("client_secret"); the values exist only in memory.
        self._secrets = {label: value for label, value in (secrets or {}).items() if value}
        self._schema = schema

    def render(self, document: object) -> bytes:
        errors = validate(document, self._schema)
        if errors:
            raise EvidenceRejected("evidence breaks its schema: " + "; ".join(errors[:8]))
        data = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.guard(data)
        return data

    def guard(self, data: bytes) -> None:
        for label, secret in self._secrets.items():
            if any(form in data for family in variants(secret).values() for form in family):
                raise EvidenceRejected(f"evidence would disclose {label}")
        if _BEARER.search(data):
            raise EvidenceRejected("evidence would disclose a bearer credential")

    def write(self, path: Path, document: object) -> None:
        write_bytes(path, self.render(document))


def write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` atomically with ``data``, flushed to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f"{path.name}.tmp")
    with staging.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)
