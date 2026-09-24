"""The independent capture owner of `ValidationSample` (ADR-0017 §7.3).

It has its own parser and its own generic rules, and it never receives a profile: the thing a
profile is validated against is never cut by that profile. It keeps sanitized product-scoped
evidence — controls and their state, admissible embedded data as parsed literals — and strips by
what a value *is*: credentials, security fields, session and member material, user-entered values,
non-literal scripts. Non-authoritative, private and navigation regions are excluded and recorded
by boundary and class only, never by content.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

CAPTURE_REVISION = "capture-proto-1"
BLOCK_MAX_BYTES = 64 * 1024
SAMPLE_EMBEDDED_MAX_BYTES = 256 * 1024
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_JSON_TYPES = frozenset({"application/ld+json", "application/json"})
_ASSIGNMENT = re.compile(r"^\s*(?:var|let|const)?\s*([A-Za-z_$][\w$.]*)\s*=\s*(.+?);?\s*$", re.S)
_SECRET_NAME = re.compile(r"(?i)(csrf|token|session|secret|passw|auth|sig(nature)?$|api[-_]?key)")
_SECRET_VALUE = re.compile(r"(eyJ[A-Za-z0-9_-]{10,}|[A-Fa-f0-9]{32,}|(?i:bearer\s+\S+))")
_MEMBER_KEY = re.compile(
    r"(?i)(member|customer|user(name|_?id)?$|email|phone|mobile|address|point|grade)"
)
_URL_ATTRS = frozenset({"src", "href", "action", "data-src"})
# An input keeps its structure and state; its ``value`` survives only for a server-authored control
# (hidden, submit, button) whose name and value are not secret or member material.
_INPUT_SERVER_VALUE_TYPES = frozenset({"hidden", "submit", "button"})
_EMBEDDED_URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//|^/", re.I)
_NON_AUTHORITATIVE = ("review", "qna", "recommend", "related", "recent", "ugc", "advert", "banner")
_NON_AUTHORITATIVE_EXACT = frozenset({"ad", "ads"})
_PRIVATE = ("member", "account", "mypage", "login", "userinfo", "user-info")
_NAVIGATION_TAGS = frozenset({"header", "nav", "footer"})


class RegionClass(StrEnum):
    NON_AUTHORITATIVE = "NON_AUTHORITATIVE"
    PRIVATE = "PRIVATE"
    NAVIGATION = "NAVIGATION"


class OperatorReason(StrEnum):
    """Why an operator may exclude a further region. Closed: disagreement with a profile is not a
    reason, and no profile has been evaluated when the scope is recorded."""

    PRIVACY = "PRIVACY"
    NON_AUTHORITATIVE = "NON_AUTHORITATIVE"


@dataclass(frozen=True)
class OperatorExclusion:
    token: str  # an element id or class token
    reason: OperatorReason


@dataclass(frozen=True)
class OperatorScope:
    """The operator's scope decision, recorded before any profile runs (ADR-0017 §7.3).

    ``product_boundary`` is the element id or class token of the one product region the operator
    approves. It must resolve to exactly one element; only that subtree is captured.
    """

    decided_by: str
    decided_at: str
    product_boundary: str
    confirmed_regions: tuple[str, ...]
    exclusions: tuple[OperatorExclusion, ...] = ()


class CaptureRefused(ValueError):
    pass


@dataclass(frozen=True)
class ValidationSample:
    """Immutable and content-addressed: the parsed views are rebuilt from stored text each time."""

    snapshot_json: str
    expected_json: str
    provenance_json: str
    truncated: bool
    digest: str

    @property
    def snapshot(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.snapshot_json)
        return loaded

    @property
    def expected(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.expected_json)
        return loaded

    @property
    def provenance(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.provenance_json)
        return loaded


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- the capture's own parser


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: dict[str, Any] = {"tag": "#document", "attrs": {}, "children": []}
        self._stack = [self.root]

    def _new(self, tag: str, attrs: list[tuple[str, str | None]]) -> dict[str, Any]:
        node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs}, "children": []}
        self._stack[-1]["children"].append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._new(tag, attrs)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._new(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth]["tag"] == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1]["children"].append(data)


def _parse(html: str) -> dict[str, Any]:
    parser = _Parser()
    parser.feed(html)
    parser.close()
    return parser.root


def _tokens(node: Mapping[str, Any]) -> list[str]:
    attrs = node.get("attrs", {})
    return [*attrs.get("class", "").lower().split(), attrs.get("id", "").lower()]


def boundary(node: Mapping[str, Any]) -> str:
    attrs = node.get("attrs", {})
    classes = ".".join(sorted(attrs.get("class", "").split()))
    return f"{node['tag']}#{attrs.get('id', '')}.{classes}"


def _region_class(node: Mapping[str, Any]) -> RegionClass | None:
    if node["tag"] in _NAVIGATION_TAGS:
        return RegionClass.NAVIGATION
    tokens = [t for t in _tokens(node) if t]
    if any(t.startswith(_PRIVATE) for t in tokens):
        return RegionClass.PRIVATE
    if any(t.startswith(_NON_AUTHORITATIVE) or t in _NON_AUTHORITATIVE_EXACT for t in tokens):
        return RegionClass.NON_AUTHORITATIVE
    return None


def _carries(node: Mapping[str, Any], token: str) -> bool:
    return token.lower() in set(_tokens(node))


def find_boundary(root: dict[str, Any], token: str) -> dict[str, Any]:
    """The one element the operator's boundary names; missing or ambiguous refuses the sample."""
    found: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        for child in node["children"]:
            if isinstance(child, dict):
                if _carries(child, token):
                    found.append(child)
                walk(child)

    if not token.strip():
        raise CaptureRefused("the operator has recorded no product boundary")
    walk(root)
    if len(found) != 1:
        raise CaptureRefused(
            f"the product boundary {token!r} resolves to {len(found)} elements, not exactly one"
        )
    if _region_class(found[0]) is not None:
        raise CaptureRefused("the product boundary is itself a non-product region")
    return found[0]


def detect_regions(html: str, product_boundary: str) -> list[tuple[str, RegionClass]]:
    """What the generic rules would exclude inside the product boundary, for the operator to
    confirm before capture."""
    found: list[tuple[str, RegionClass]] = []

    def walk(node: dict[str, Any]) -> None:
        for child in node["children"]:
            if isinstance(child, dict):
                region = _region_class(child)
                if region is not None:
                    found.append((boundary(child), region))
                else:
                    walk(child)

    walk(find_boundary(_parse(html), product_boundary))
    return found


# ---------------------------------------------------------------- sanitizing


@dataclass
class _Capture:
    scope: OperatorScope
    removals: list[list[str]] = field(default_factory=list)
    excluded: list[list[str]] = field(default_factory=list)
    embedded_bytes: int = 0
    truncated: bool = False

    def removed(self, what: str, where: Mapping[str, Any]) -> None:
        self.removals.append([what, boundary(where)])


def _strip_data(capture: _Capture, data: Any, where: Mapping[str, Any]) -> Any:
    if isinstance(data, dict):
        kept: dict[str, Any] = {}
        for key, value in data.items():
            if _SECRET_NAME.search(key) or _MEMBER_KEY.search(key):
                capture.removed(f"EMBEDDED_KEY:{key}", where)
                continue
            kept[key] = _strip_data(capture, value, where)
        return kept
    if isinstance(data, list):
        return [_strip_data(capture, item, where) for item in data]
    if isinstance(data, str) and _SECRET_VALUE.search(data):
        capture.removed("EMBEDDED_SECRET_VALUE", where)
        return None
    if isinstance(data, str) and _EMBEDDED_URL.match(data) and ("?" in data or "#" in data):
        # Every query key is secret-bearing by default (ADR-0010 §9), inside embedded data too.
        parts = urlsplit(data)
        capture.removed("EMBEDDED_URL_QUERY", where)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return data


def _script(capture: _Capture, node: dict[str, Any]) -> dict[str, Any] | None:
    body = "".join(c for c in node["children"] if isinstance(c, str))
    kind = node["attrs"].get("type", "").strip().lower()
    assignment: str | None = None
    try:
        if kind in _JSON_TYPES:
            data = json.loads(body)
        elif not kind and (match := _ASSIGNMENT.fullmatch(body)):
            # Pure literal only: the value must parse as JSON; any code makes it inadmissible.
            data = json.loads(match.group(2))
            assignment = match.group(1)
        else:
            capture.removed("SCRIPT_NOT_LITERAL", node)
            return None
    except ValueError:
        capture.removed("SCRIPT_NOT_LITERAL", node)
        return None
    data = _strip_data(capture, data, node)
    size = len(_canonical(data).encode("utf-8"))
    attrs = {"type": node["attrs"]["type"]} if "type" in node["attrs"] else {}
    if size > BLOCK_MAX_BYTES or capture.embedded_bytes + size > SAMPLE_EMBEDDED_MAX_BYTES:
        # Kept by digest only, for diagnostics; the sample can then never support a PASS (V8).
        capture.truncated = True
        return {
            "tag": "script",
            "attrs": attrs,
            "children": [],
            "truncated": True,
            "digest": hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest(),
        }
    capture.embedded_bytes += size
    kept: dict[str, Any] = {"tag": "script", "attrs": attrs, "children": [], "data": data}
    if assignment is not None:
        kept["assignment"] = assignment
    return kept


def _input_value_kept(node: Mapping[str, Any]) -> bool:
    attrs = node["attrs"]
    field_name = attrs.get("name", "")
    return (
        attrs.get("type", "text").lower() in _INPUT_SERVER_VALUE_TYPES
        and not _SECRET_NAME.search(field_name)
        and not _MEMBER_KEY.search(field_name)
        and not _SECRET_VALUE.search(attrs.get("value", ""))
    )


def _attrs(capture: _Capture, node: dict[str, Any]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for name, value in node["attrs"].items():
        if name.lower().startswith("on"):
            capture.removed(f"EVENT_HANDLER:{name}", node)
            continue
        if node["tag"] == "input" and name == "value" and not _input_value_kept(node):
            # A user-entered, credential, security or member value; the control itself stays.
            capture.removed("INPUT_VALUE", node)
            continue
        if _SECRET_NAME.search(name) or _SECRET_VALUE.search(value):
            capture.removed(f"SECRET_ATTR:{name}", node)
            continue
        if name in _URL_ATTRS:
            parts = urlsplit(value)
            clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            if clean != value:
                capture.removed(f"URL_QUERY:{name}", node)
            value = clean
        kept[name] = value
    return kept


def _operator_excluded(scope: OperatorScope, node: Mapping[str, Any]) -> OperatorExclusion | None:
    tokens = set(_tokens(node))
    return next((e for e in scope.exclusions if e.token.lower() in tokens), None)


def _sanitize(capture: _Capture, node: dict[str, Any]) -> dict[str, Any] | None:
    tag = node["tag"]
    if tag == "style":
        return None
    if tag == "script":
        return _script(capture, node)
    region = _region_class(node)
    if region is not None:
        capture.excluded.append([boundary(node), region.value])
        return None
    operator = _operator_excluded(capture.scope, node)
    if operator is not None:
        capture.excluded.append([boundary(node), f"OPERATOR_{operator.reason.value}"])
        return None
    children: list[Any] = []
    for child in node["children"]:
        if isinstance(child, str):
            children.append(child)
        elif (kept := _sanitize(capture, child)) is not None:
            children.append(kept)
    return {"tag": tag, "attrs": _attrs(capture, node), "children": children}


def capture_sample(
    html: str, scope: OperatorScope, expected: Mapping[str, Any]
) -> ValidationSample:
    """Cut one sample. It takes no profile: the scope is decided before any profile runs, and only
    the operator's one product boundary is captured."""
    detected = [
        b
        for b, region in detect_regions(html, scope.product_boundary)
        if region is not RegionClass.NAVIGATION
    ]
    if unconfirmed := [b for b in detected if b not in scope.confirmed_regions]:
        raise CaptureRefused(f"the operator has not confirmed excluded regions: {unconfirmed}")
    capture = _Capture(scope)
    region = find_boundary(_parse(html), scope.product_boundary)
    kept_region = _sanitize(capture, region)
    if kept_region is None:
        raise CaptureRefused("the product boundary was excluded by the operator's own scope")
    snapshot = {"tag": "#document", "attrs": {}, "children": [kept_region]}
    provenance = {
        "capture_revision": CAPTURE_REVISION,
        "scope": {
            "decided_by": scope.decided_by,
            "decided_at": scope.decided_at,
            "product_boundary": scope.product_boundary,
            "boundary_element": boundary(region),
            "confirmed_regions": list(scope.confirmed_regions),
            "exclusions": [[e.token, e.reason.value] for e in scope.exclusions],
        },
        "excluded": capture.excluded,
        "removals": capture.removals,
    }
    body = {
        "snapshot": snapshot,
        "expected": dict(expected),
        "provenance": provenance,
        "truncated": capture.truncated,
    }
    return ValidationSample(
        snapshot_json=_canonical(snapshot),
        expected_json=_canonical(dict(expected)),
        provenance_json=_canonical(provenance),
        truncated=capture.truncated,
        digest=hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest(),
    )


def secret_findings(text: str) -> list[str]:
    """The same secret scan the capture applies, for V6 over engine output."""
    return [match.group(0)[:8] + "…" for match in _SECRET_VALUE.finditer(text)]
