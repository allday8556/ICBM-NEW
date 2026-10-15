"""The ``BrowserCapturePolicy`` owner (ADR-0019 §5; ruling ``5906290729`` B-5, B-10).

A policy is supplier-scoped, repository-reviewed data: one JSON file beside the supplier's other
site knowledge, ``integrations/suppliers/<supplier_key>/browser_capture_policy.json``. It holds
capture **topology** only — the product root, the allowed and excluded regions, the allowed
attributes, the head allowance and the node and byte bounds — and an explicit revision. It holds
no source fact and no extraction rule, and it never overlaps ``CollectionProfile``, which keeps
host, path, query, pacing and transport.

There is no second copy. The extension receives exactly the canonical bytes served from this file
(:class:`CapturePolicySource`), and every ingest names the revision and digest it was cut with.

The digest is the SHA-256 of one canonical UTF-8 JSON serialization: sorted keys, no insignificant
whitespace. It is recomputed from the file on every load and never cached, so a policy that changed
since a capture was cut can never be mistaken for the one it was cut with.

**Embedded data** (ADR-0040 §3, 1688 only). A policy may declare one optional ``embedded_data``
entry: the inline script's prefix, argument marker and terminator, whether bare integer object keys
are accepted, the byte and depth bounds, and the allowlist of JSON paths with their types. It is
part of the canonical bytes, so it is digested with everything else. A policy without the entry is
exactly what it was before, byte for byte.

The path grammar is small and explicit: dot-separated segments, each a key name
(``[A-Za-z_][A-Za-z0-9_]*``) or ``*`` (every key of a map), optionally followed by ``[]`` (the value
is an array, and the rest of the path applies to each element). Every path starts at
``result.data`` and ends at a leaf of type ``string``, ``integer``, ``number`` or ``boolean``. A
path never names a member's receive address, identity or account (ADR-0040 OC-05): such a segment
refuses the whole policy.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.platform.core.errors import PolicyBlockedError

POLICY_FILE = "browser_capture_policy.json"
POLICY_SCHEMA = "icbm-browser-capture-policy/v1"
# Ruling 5906290729 B-4: the E1 single-click ceilings. A policy may bound a capture more tightly,
# never more loosely.
MAX_HTML_BYTES = 512 * 1024
MAX_NODES = 20_000
MAX_IMAGE_REFS = 40

_SUPPLIER_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_REVISION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)
_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_VALUE = re.compile(r"^[A-Za-z0-9:_.-]{1,80}$")
_KEYS = frozenset(
    {
        "schema",
        "supplier_key",
        "revision",
        "host",
        "product_root",
        "allowed_regions",
        "excluded_regions",
        "excluded_tags",
        "allowed_attributes",
        "head_allowance",
        "bounds",
    }
)
_BOUND_KEYS = frozenset({"max_html_bytes", "max_nodes", "max_image_refs"})
# ADR-0040 §3: the optional embedded-data entry, and its exact keys.
EMBEDDED_DATA_KEY = "embedded_data"
_EMBEDDED_KEYS = frozenset(
    {
        "prefix",
        "argument_marker",
        "terminator",
        "bare_integer_keys",
        "max_bytes",
        "max_depth",
        "allowlist",
    }
)
# The ceilings an embedded-data entry may declare. A policy may bound the argument more tightly,
# never more loosely.
MAX_EMBEDDED_ARGUMENT_BYTES = 2 * 1024 * 1024
MAX_EMBEDDED_DEPTH = 64
MAX_EMBEDDED_PATHS = 64
EMBEDDED_ROOT = ("result", "data")
EMBEDDED_TYPES = frozenset({"string", "integer", "number", "boolean"})
_SEGMENT = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]{0,63}|\*)(?:\[\])?$")
_MARKER = re.compile(r"^[\x20-\x7e\n]{1,200}$")
# OC-05: no allowlisted segment may name a member's receive address, identity or account. Matched
# case-insensitively anywhere in a segment, so 1688's own misspelling (recieveAddress) is caught.
_FORBIDDEN_SEGMENT = re.compile(
    r"(?i)(receiv|reciev|address|member|nick|login|user|buyer|account|token|cookie|session|passw)"
)


class CapturePolicyRefused(PolicyBlockedError):
    """No usable capture policy: absent, malformed, or not the one a capture names. Nothing is
    captured or accepted under a policy that cannot be proven."""


def _policy_refused(code: str, message: str) -> CapturePolicyRefused:
    return CapturePolicyRefused(code, message)


@dataclass(frozen=True)
class Region:
    """One element the policy names, by the ``id`` or by a ``class`` token it carries."""

    by: str  # "id" or "class"
    token: str


@dataclass(frozen=True)
class HeadElement:
    """One ``<head>`` element the policy keeps: a tag carrying exactly this attribute value."""

    tag: str
    attribute: str
    value: str


@dataclass(frozen=True)
class CaptureBounds:
    max_html_bytes: int
    max_nodes: int
    max_image_refs: int


@dataclass(frozen=True)
class EmbeddedPath:
    """One allowlisted JSON path and the type of the leaf it names."""

    path: str
    segments: tuple[str, ...]
    type: str


@dataclass(frozen=True)
class EmbeddedDataRule:
    """ADR-0040 §3: the one inline script whose data argument a capture may carry, projected."""

    prefix: str
    argument_marker: str
    terminator: str
    bare_integer_keys: bool
    max_bytes: int
    max_depth: int
    allowlist: tuple[EmbeddedPath, ...]


@dataclass(frozen=True)
class BrowserCapturePolicy:
    supplier_key: str
    revision: str
    host: str
    product_root: Region
    allowed_regions: tuple[Region, ...]
    excluded_regions: tuple[Region, ...]
    excluded_tags: frozenset[str]
    # Attribute names kept per tag; ``*`` applies to every tag. Anything else is dropped.
    allowed_attributes: Mapping[str, frozenset[str]]
    head_allowance: tuple[HeadElement, ...]
    bounds: CaptureBounds
    # Exactly what is served to the extension and what the digest is computed over.
    canonical: bytes = field(repr=False)
    digest: str
    # ADR-0040 §3: the embedded-data member this supplier's captures carry, if any.
    embedded_data: EmbeddedDataRule | None = None

    def keeps_attribute(self, tag: str, name: str) -> bool:
        return name in self.allowed_attributes.get("*", frozenset()) or name in (
            self.allowed_attributes.get(tag, frozenset())
        )

    def is_excluded_region(self, attributes: Mapping[str, str]) -> bool:
        """Whether an element with these attributes is one of the policy's excluded regions."""
        identifier = attributes.get("id", "")
        classes = attributes.get("class", "").split()
        return any(
            region.token == identifier if region.by == "id" else region.token in classes
            for region in self.excluded_regions
        )

    def keeps_head(self, tag: str, attributes: Mapping[str, str]) -> bool:
        return any(
            allowed.tag == tag
            and allowed.value.lower() in attributes.get(allowed.attribute, "").lower().split()
            for allowed in self.head_allowance
        )


def canonical_bytes(document: Mapping[str, Any]) -> bytes:
    """The one canonical serialization: UTF-8, sorted keys, no insignificant whitespace."""
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def policy_digest(canonical: bytes) -> str:
    return hashlib.sha256(canonical).hexdigest()


def _text(value: Any, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _policy_refused("CAPTURE_POLICY_INVALID", f"the capture policy's {what} is not valid")
    return value


def _region(value: Any, what: str) -> Region:
    if (
        not isinstance(value, dict)
        or set(value) != {"by", "token"}
        or value["by"]
        not in (
            "id",
            "class",
        )
    ):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", f"the capture policy's {what} is not a region"
        )
    return Region(by=value["by"], token=_text(value["token"], _TOKEN, what))


def _regions(value: Any, what: str) -> tuple[Region, ...]:
    if not isinstance(value, list):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", f"the capture policy's {what} is not a list"
        )
    return tuple(_region(entry, what) for entry in value)


def _names(value: Any, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(set(value)) != len(value):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", f"the capture policy's {what} is not a name list"
        )
    return tuple(_text(entry, _NAME, what) for entry in value)


def _bound(value: Any, ceiling: int, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= ceiling:
        # A missing or unbounded cap refuses; nothing here falls back to a default (ADR-0010 §4).
        raise _policy_refused(
            "CAPTURE_POLICY_BOUND_INVALID", f"the capture policy's {what} is not within its ceiling"
        )
    return value


def _marker(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _MARKER.fullmatch(value):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", f"the embedded-data {what} is not printable text"
        )
    return value


def _embedded_path(entry: Any) -> EmbeddedPath:
    if not isinstance(entry, dict) or set(entry) != {"path", "type"}:
        raise _policy_refused("CAPTURE_POLICY_INVALID", "an embedded-data path is not exact")
    path, kind = entry["path"], entry["type"]
    if not isinstance(path, str) or kind not in EMBEDDED_TYPES:
        raise _policy_refused("CAPTURE_POLICY_INVALID", "an embedded-data path is not valid")
    segments = tuple(path.split("."))
    if (
        segments[: len(EMBEDDED_ROOT)] != EMBEDDED_ROOT
        or len(segments) <= len(EMBEDDED_ROOT)
        or not all(_SEGMENT.fullmatch(segment) for segment in segments)
    ):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "an embedded-data path is not under result.data"
        )
    if any(_FORBIDDEN_SEGMENT.search(segment) for segment in segments):
        # OC-05: a receive address, a member identity or an account is never allowlisted.
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "an embedded-data path names member or account data"
        )
    return EmbeddedPath(path=path, segments=segments, type=kind)


def _check_tree(paths: tuple[EmbeddedPath, ...]) -> None:
    """The allowlist is one tree: no path twice, no leaf that is also a container, no segment
    used both with and without ``[]``, and no map that has both named keys and ``*``."""
    leaves = {path.segments for path in paths}
    if len(leaves) != len(paths):
        raise _policy_refused("CAPTURE_POLICY_INVALID", "an embedded-data path is listed twice")
    shapes: dict[tuple[str, ...], str] = {}
    children: dict[tuple[str, ...], set[str]] = {}
    for path in paths:
        for depth in range(1, len(path.segments) + 1):
            prefix = tuple(segment.removesuffix("[]") for segment in path.segments[:depth])
            shape = path.segments[depth - 1]
            if shapes.setdefault(prefix, shape) != shape:
                raise _policy_refused(
                    "CAPTURE_POLICY_INVALID", "an embedded-data segment has two shapes"
                )
            children.setdefault(prefix[:-1], set()).add(prefix[-1])
    for path in paths:
        bare = tuple(segment.removesuffix("[]") for segment in path.segments)
        if bare in children:
            raise _policy_refused(
                "CAPTURE_POLICY_INVALID", "an embedded-data leaf is also a container"
            )
    if any("*" in names and len(names) > 1 for names in children.values()):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "an embedded-data map names keys beside its wildcard"
        )


def _embedded_data(value: Any) -> EmbeddedDataRule:
    if not isinstance(value, dict) or set(value) != _EMBEDDED_KEYS:
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "the capture policy's embedded data does not have its keys"
        )
    if not isinstance(value["bare_integer_keys"], bool):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "the embedded-data integer-key flag is not a boolean"
        )
    allowlist = value["allowlist"]
    if not isinstance(allowlist, list) or not 1 <= len(allowlist) <= MAX_EMBEDDED_PATHS:
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "the embedded-data allowlist is not a bounded list"
        )
    paths = tuple(_embedded_path(entry) for entry in allowlist)
    _check_tree(paths)
    return EmbeddedDataRule(
        prefix=_marker(value["prefix"], "prefix"),
        argument_marker=_marker(value["argument_marker"], "argument marker"),
        terminator=_marker(value["terminator"], "terminator"),
        bare_integer_keys=value["bare_integer_keys"],
        max_bytes=_bound(value["max_bytes"], MAX_EMBEDDED_ARGUMENT_BYTES, "embedded byte bound"),
        max_depth=_bound(value["max_depth"], MAX_EMBEDDED_DEPTH, "embedded depth bound"),
        allowlist=paths,
    )


def parse_policy(raw: bytes, supplier_key: str) -> BrowserCapturePolicy:
    """Validate one policy document strictly and bind it to its canonical bytes and digest."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "the capture policy is not UTF-8 JSON"
        ) from None
    if not isinstance(document, dict) or set(document) - {EMBEDDED_DATA_KEY} != _KEYS:
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "the capture policy does not have its exact keys"
        )
    if document["schema"] != POLICY_SCHEMA:
        raise _policy_refused("CAPTURE_POLICY_INVALID", "the capture policy names another schema")
    if document["supplier_key"] != supplier_key:
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "the capture policy belongs to another supplier"
        )
    attributes = document["allowed_attributes"]
    if not isinstance(attributes, dict) or not attributes:
        raise _policy_refused("CAPTURE_POLICY_INVALID", "the capture policy allows no attributes")
    head = document["head_allowance"]
    if not isinstance(head, list) or any(
        not isinstance(entry, dict) or set(entry) != {"tag", "attribute", "value"} for entry in head
    ):
        raise _policy_refused(
            "CAPTURE_POLICY_INVALID", "the capture policy's head allowance is invalid"
        )
    bounds = document["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != _BOUND_KEYS:
        raise _policy_refused(
            "CAPTURE_POLICY_BOUND_INVALID", "the capture policy states no exact bounds"
        )
    canonical = canonical_bytes(document)
    return BrowserCapturePolicy(
        supplier_key=supplier_key,
        revision=_text(document["revision"], _REVISION, "revision"),
        host=_text(document["host"], _HOST, "host"),
        product_root=_region(document["product_root"], "product root"),
        allowed_regions=_regions(document["allowed_regions"], "allowed regions"),
        excluded_regions=_regions(document["excluded_regions"], "excluded regions"),
        excluded_tags=frozenset(_names(document["excluded_tags"], "excluded tags")),
        allowed_attributes={
            (tag if tag == "*" else _text(tag, _NAME, "attribute tag")): frozenset(
                _names(names, "allowed attributes")
            )
            for tag, names in attributes.items()
        },
        head_allowance=tuple(
            HeadElement(
                tag=_text(entry["tag"], _NAME, "head tag"),
                attribute=_text(entry["attribute"], _NAME, "head attribute"),
                value=_text(entry["value"], _VALUE, "head value"),
            )
            for entry in head
        ),
        bounds=CaptureBounds(
            max_html_bytes=_bound(bounds["max_html_bytes"], MAX_HTML_BYTES, "byte bound"),
            max_nodes=_bound(bounds["max_nodes"], MAX_NODES, "node bound"),
            max_image_refs=_bound(bounds["max_image_refs"], MAX_IMAGE_REFS, "image bound"),
        ),
        canonical=canonical,
        digest=policy_digest(canonical),
        embedded_data=(
            _embedded_data(document[EMBEDDED_DATA_KEY]) if EMBEDDED_DATA_KEY in document else None
        ),
    )


class CapturePolicySource:
    """Reads a supplier's reviewed policy from the repository, every time it is asked.

    ``root`` is the directory that holds the supplier packages. A supplier key is checked against
    its own shape before it names a path, so it can never leave that directory.
    """

    def __init__(self, root: Path, sites: Mapping[str, bytes] | None = None) -> None:
        self._root = root
        # ADR-0030 §4: a configured site's policy is derived from its platform template's and
        # served from those bytes; it has no file of its own.
        # A live registry view (ADR-0036 §4) is kept as it is, so a rebound site's policy is
        # served the moment it is bound.
        self._sites: Mapping[str, bytes] = {} if sites is None else sites

    def load(self, supplier_key: str) -> BrowserCapturePolicy:
        if not _SUPPLIER_KEY.fullmatch(supplier_key):
            raise _policy_refused(
                "CAPTURE_POLICY_ABSENT", "no capture policy exists for that supplier"
            )
        if supplier_key in self._sites:
            return parse_policy(self._sites[supplier_key], supplier_key)
        path = self._root / supplier_key / POLICY_FILE
        try:
            raw = path.read_bytes()
        except OSError:
            # No reviewed policy means the extension transport is not reviewed for this supplier.
            raise _policy_refused(
                "CAPTURE_POLICY_ABSENT", "no capture policy exists for that supplier"
            ) from None
        return parse_policy(raw, supplier_key)
