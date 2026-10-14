"""The embedded-data member of a capture, and what the server re-checks of it (ADR-0040 §3).

For a supplier whose reviewed ``BrowserCapturePolicy`` declares ``embedded_data`` (1688 only), the
browser parses one inline script's JSON argument as data, never runs it, and sends only its
projection onto the policy's allowlist: :class:`EmbeddedDataMember`. When the browser could not
make that projection it sends no data at all, only the closed code that says why.

The server trusts none of the browser's cut (ADR-0019 §6). :func:`accept_member` walks exactly
what arrived against the same allowlist the policy digests:

- every object key must be an allowlisted name, or a key of an allowlisted map (``*``);
- every array must be an allowlisted array (``[]``);
- every leaf must have its declared type (a JSON ``null`` is never sent and never accepted);
- the member is bounded in bytes, in entries and in the length of each key and string.

Any mismatch refuses the member, never the capture: the member is then absent, so every field it
would have supplied is ``REVIEW_REQUIRED`` (OC-04). A path outside the allowlist — the member's
receive address above all (OC-05) — is such a mismatch. The member is held in memory only, for the
one job that extracts from it, and is never logged, persisted or returned; a refusal is logged by
its code alone.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.stages.collect.extension.policy import EmbeddedDataRule

# What the browser sends when it could not make the projection (ui/extension/lib/capture.js).
BrowserRefusal = Literal[
    "EMBEDDED_SCRIPT_NOT_EXACTLY_ONE",
    "EMBEDDED_MARKER_NOT_EXACTLY_ONE",
    "EMBEDDED_TERMINATOR",
    "EMBEDDED_OVER_BYTES",
    "EMBEDDED_NOT_JSON",
    "EMBEDDED_OVER_DEPTH",
    "EMBEDDED_WRONG_SHAPE",
]
# Why the server refused a member it received, or found none where the policy declares one.
EMBEDDED_DATA_ABSENT = "EMBEDDED_DATA_ABSENT"
EMBEDDED_PATH_NOT_ALLOWED = "EMBEDDED_PATH_NOT_ALLOWED"
EMBEDDED_TYPE_MISMATCH = "EMBEDDED_TYPE_MISMATCH"
EMBEDDED_OVER_BOUND = "EMBEDDED_OVER_BOUND"

# The projection's own bounds. The 1688 projections observed were under 6 KB for 20 SKUs; these
# leave room for a product with many hundreds of SKUs and nothing more.
MAX_PROJECTION_BYTES = 256 * 1024
MAX_PROJECTION_ENTRIES = 4096
MAX_KEY_LENGTH = 256
MAX_STRING_LENGTH = 2048


class EmbeddedDataMember(BaseModel):
    """The ``embedded_data`` member of a capture envelope: a projection, or a browser refusal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projection: dict[str, JsonValue] | None = Field(default=None, repr=False)
    refused: BrowserRefusal | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "EmbeddedDataMember":
        if (self.projection is None) == (self.refused is None):
            raise ValueError("the member is a projection or a refusal, exactly one")
        return self


def member_bytes(member: EmbeddedDataMember) -> int:
    """The member's size as canonical UTF-8 JSON, for the ingest ceiling."""
    return len(
        json.dumps(
            member.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )


@dataclass
class _Node:
    keys: dict[str, "_Node"] = field(default_factory=dict)
    any: "_Node | None" = None
    each: bool = False
    type: str | None = None


def allowlist_tree(rule: EmbeddedDataRule) -> _Node:
    """The policy's allowlist as one tree, built exactly as the extension builds it."""
    root = _Node()
    for entry in rule.allowlist:
        node = root
        for segment in entry.segments:
            name = segment.removesuffix("[]")
            if name == "*":
                if node.any is None:
                    node.any = _Node()
                child = node.any
            else:
                child = node.keys.setdefault(name, _Node())
            child.each = segment.endswith("[]")
            node = child
        node.type = entry.type
    return root


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fits(value: object, kind: str) -> bool:
    if isinstance(value, bool):
        return kind == "boolean"
    if kind == "string":
        return isinstance(value, str) and len(value) <= MAX_STRING_LENGTH
    if kind == "integer":
        return isinstance(value, int) and abs(value) <= 2**53 - 1
    if kind == "number":
        return isinstance(value, int | float) and value == value and abs(value) != float("inf")
    return False


class _Walk:
    def __init__(self) -> None:
        self.entries = 0

    def _count(self) -> None:
        self.entries += 1
        if self.entries > MAX_PROJECTION_ENTRIES:
            raise _Refused(EMBEDDED_OVER_BOUND)

    def value(self, value: object, node: _Node) -> None:
        if node.each:
            if not isinstance(value, list):
                raise _Refused(EMBEDDED_TYPE_MISMATCH)
            for element in value:
                self._count()
                self.one(element, node)
            return
        self.one(value, node)

    def one(self, value: object, node: _Node) -> None:
        if node.type is not None:
            if not _fits(value, node.type):
                raise _Refused(EMBEDDED_TYPE_MISMATCH)
            return
        if not isinstance(value, dict):
            raise _Refused(EMBEDDED_TYPE_MISMATCH)
        for key, child_value in value.items():
            self._count()
            if not isinstance(key, str) or len(key) > MAX_KEY_LENGTH:
                raise _Refused(EMBEDDED_OVER_BOUND)
            child = node.keys.get(key, node.any)
            if child is None:
                raise _Refused(EMBEDDED_PATH_NOT_ALLOWED)
            self.value(child_value, child)


def projection_problem(rule: EmbeddedDataRule, projection: Mapping[str, Any]) -> str | None:
    """Why the server refuses this projection, or ``None`` when it is exactly allowlisted."""
    try:
        size = len(json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        return EMBEDDED_TYPE_MISMATCH
    if size > MAX_PROJECTION_BYTES:
        return EMBEDDED_OVER_BOUND
    try:
        _Walk().one(projection, allowlist_tree(rule))
    except _Refused as refused:
        return refused.code
    return None


def strings_of(rule: EmbeddedDataRule, projection: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every string of an accepted projection, key or value, with the allowlist path it sits at.

    A map key is data, so it is read like a value. The path is the allowlist's own (a map's key is
    ``*``), so a finding or a log line that names it never repeats anything the page wrote.
    """
    found: list[tuple[str, str]] = []
    pending: list[tuple[object, _Node, str]] = [(projection, allowlist_tree(rule), "")]
    while pending:
        item, node, at = pending.pop()
        if isinstance(item, list):
            pending.extend((element, node, at) for element in item)
        elif isinstance(item, str):
            found.append((at, item))
        elif isinstance(item, dict):
            for key, child in item.items():
                named = key if key in node.keys else "*"
                where = f"{at}.{named}" if at else named
                if named == "*":
                    found.append((where, key))
                    pending.append((child, node.any or _Node(), where))
                else:
                    pending.append((child, node.keys[key], where))
    return found
