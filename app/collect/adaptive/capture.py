"""The independent `ValidationSample` capture owner (ADR-0017 §7.3).

It never receives a profile. The operator records one **product boundary** before any profile
runs; only that subtree is captured, and it must resolve to exactly one element. Inside it, the
capture's own reader and generic rules decide what is proof material:

* kept, sanitized — product controls and their state, and admissible embedded data as parsed
  literals (JSON-LD, JSON scripts, one pure-literal assignment);
* stripped by what a value *is*, never by element type — credentials, security fields, session,
  member, account and contact material, user-entered values, event handlers, URL queries,
  executable code;
* excluded and recorded by boundary and class only — non-authoritative (reviews, Q&A,
  recommendations, UGC, ads), private and navigation regions.

A final, independent scan of the sanitized structure refuses the sample on any residual secret or
private material, so nothing unsafe is ever returned as proof material. A block over the embedded
bounds is kept by digest only and marks the sample ``SAMPLE_TRUNCATED``.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.collect.adaptive.canonical import NonFiniteValue, canonical_json, digest, parse_json

CAPTURE_REVISION = "adaptive-capture-1"
SAMPLE_DIGEST_SCHEME = "icbm-validation-sample/v1"
BLOCK_MAX_BYTES = 64 * 1024
SAMPLE_EMBEDDED_MAX_BYTES = 256 * 1024

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_JSON_TYPES = frozenset({"application/ld+json", "application/json"})
_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:var|let|const)\s+)?([A-Za-z_$][\w$.]*)\s*=\s*(.+?)\s*;?\s*$", re.S
)
_URL_ATTRIBUTES = frozenset({"src", "href", "action", "data-src", "srcset", "poster"})
_SERVER_VALUE_INPUTS = frozenset({"hidden", "submit", "button"})
_SECRET_NAME = re.compile(r"(?i)(csrf|xsrf|token|session|secret|passw|auth|api[-_]?key|signature)")
_PRIVATE_NAME = re.compile(
    r"(?i)(member|customer|account|mypage|login|nickname|contact|birth|email|e-mail|phone|mobile"
    r"|address|point|grade|user(name|[-_]?id)?$)"
)
_SECRET_VALUE = re.compile(r"(?i)(eyJ[A-Za-z0-9_-]{10,}|\b[a-f0-9]{32,}\b|bearer\s+\S+)")
_URL_TEXT = re.compile(r"(?i)^(?:[a-z][a-z0-9+.-]*:)?//|^/")
_NAVIGATION_TAGS = frozenset({"header", "nav", "footer"})
_NON_AUTHORITATIVE_PREFIXES = (
    "review",
    "qna",
    "q-and-a",
    "recommend",
    "related",
    "recent",
    "ugc",
    "advert",
    "banner",
)
_NON_AUTHORITATIVE_EXACT = frozenset({"ad", "ads"})
_PRIVATE_PREFIXES = ("member", "account", "mypage", "login", "userinfo", "user-info")
# The final scan's own value patterns: deliberately wider than what the sanitizer strips.
_RESIDUAL_VALUE = re.compile(
    r"(?i)("
    r"eyJ[A-Za-z0-9_-]{10,}"
    r"|\b[a-f0-9]{32,}\b"
    r"|bearer\s+\S+"
    r"|\b(?:token|session|sid|sessid|jsessionid|phpsessid|auth|apikey|api_key|csrf|sig|signature)=\S+"
    r"|[\w.+-]+@[\w-]+\.[\w.-]+"
    r"|\b01[016789][-. ]?\d{3,4}[-. ]?\d{4}\b"
    r")"
)


class RegionClass(StrEnum):
    NON_AUTHORITATIVE = "NON_AUTHORITATIVE"
    PRIVATE = "PRIVATE"
    NAVIGATION = "NAVIGATION"


class OperatorReason(StrEnum):
    """Why an operator may exclude a further region. Closed: disagreeing with a profile is not a
    reason, and no profile has run when the scope is recorded."""

    PRIVACY = "PRIVACY"
    NON_AUTHORITATIVE = "NON_AUTHORITATIVE"


@dataclass(frozen=True)
class OperatorExclusion:
    token: str  # an element id or class token
    reason: OperatorReason


@dataclass(frozen=True)
class OperatorScope:
    decided_by: str
    decided_at: str
    product_boundary: str  # an element id or class token naming exactly one element
    confirmed_regions: tuple[str, ...]
    exclusions: tuple[OperatorExclusion, ...] = ()


class CaptureRefused(ValueError):
    """No sample is produced; the reason names kinds and boundaries, never a captured value."""


@dataclass(frozen=True)
class ValidationSample:
    """Immutable and content-addressed. Parsed views are rebuilt from the stored text each time."""

    structure_json: str
    expected_json: str
    provenance_json: str
    truncated: bool
    digest: str

    @property
    def structure(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.structure_json)
        return loaded

    @property
    def expected(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.expected_json)
        return loaded

    @property
    def provenance(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.provenance_json)
        return loaded


Node = dict[str, Any]


# ---------------------------------------------------------------- the capture's own reader


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: Node = {"tag": "#document", "attrs": {}, "children": []}
        self._open: list[Node] = [self.root]

    def _add(self, tag: str, attrs: list[tuple[str, str | None]]) -> Node:
        node: Node = {"tag": tag, "attrs": {k: v or "" for k, v in attrs}, "children": []}
        self._open[-1]["children"].append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._add(tag, attrs)
        if tag not in _VOID:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth]["tag"] == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1]["children"].append(data)


def _read(html: str) -> Node:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return reader.root


def _tokens(node: Mapping[str, Any]) -> set[str]:
    attrs = node.get("attrs", {})
    return {*attrs.get("class", "").lower().split(), attrs.get("id", "").lower()} - {""}


def boundary_of(node: Mapping[str, Any]) -> str:
    attrs = node.get("attrs", {})
    return f"{node['tag']}#{attrs.get('id', '')}.{'.'.join(sorted(attrs.get('class', '').split()))}"


def _region(node: Mapping[str, Any]) -> RegionClass | None:
    if node["tag"] in _NAVIGATION_TAGS:
        return RegionClass.NAVIGATION
    tokens = _tokens(node)
    if any(token.startswith(_PRIVATE_PREFIXES) for token in tokens):
        return RegionClass.PRIVATE
    if any(
        token.startswith(_NON_AUTHORITATIVE_PREFIXES) or token in _NON_AUTHORITATIVE_EXACT
        for token in tokens
    ):
        return RegionClass.NON_AUTHORITATIVE
    return None


def _elements(node: Node) -> list[Node]:
    return [child for child in node["children"] if isinstance(child, dict)]


def resolve_boundary(root: Node, token: str) -> Node:
    """The one element the operator's boundary names; none or several refuses the sample."""
    if not token.strip():
        raise CaptureRefused("the operator recorded no product boundary")
    wanted = token.strip().lower()
    found: list[Node] = []
    pending = _elements(root)
    while pending:
        node = pending.pop(0)
        if wanted in _tokens(node):
            found.append(node)
        pending[0:0] = _elements(node)
    if len(found) != 1:
        raise CaptureRefused(
            f"the product boundary resolves to {len(found)} elements, not exactly one"
        )
    if _region(found[0]) is not None:
        raise CaptureRefused("the product boundary is itself a non-product region")
    return found[0]


def detect_regions(html: str, product_boundary: str) -> list[tuple[str, RegionClass]]:
    """Regions inside the boundary the generic rules exclude, for the operator to confirm."""
    detected: list[tuple[str, RegionClass]] = []

    def visit(node: Node) -> None:
        for child in _elements(node):
            region = _region(child)
            if region is None:
                visit(child)
            else:
                detected.append((boundary_of(child), region))

    visit(resolve_boundary(_read(html), product_boundary))
    return detected


# ---------------------------------------------------------------- sanitizing


@dataclass
class _Sanitizer:
    scope: OperatorScope
    removals: list[list[str]] = field(default_factory=list)
    excluded: list[list[str]] = field(default_factory=list)
    embedded_bytes: int = 0
    truncated: bool = False

    def removed(self, what: str, node: Mapping[str, Any]) -> None:
        self.removals.append([what, boundary_of(node)])

    def _strip_query(self, value: str, what: str, node: Mapping[str, Any]) -> str:
        parts = urlsplit(value)
        bare = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if bare != value:
            self.removed(what, node)
        return bare

    def data(self, value: Any, node: Mapping[str, Any]) -> Any:
        if isinstance(value, dict):
            kept: dict[str, Any] = {}
            for key, item in value.items():
                if _SECRET_NAME.search(key) or _PRIVATE_NAME.search(key):
                    self.removed(f"EMBEDDED_KEY:{key}", node)
                    continue
                kept[key] = self.data(item, node)
            return kept
        if isinstance(value, list):
            return [self.data(item, node) for item in value]
        if isinstance(value, str):
            if _SECRET_VALUE.search(value):
                self.removed("EMBEDDED_SECRET_VALUE", node)
                return None
            if _URL_TEXT.match(value):
                return self._strip_query(value, "EMBEDDED_URL_QUERY", node)
        return value

    def script(self, node: Node) -> Node | None:
        body = "".join(c for c in node["children"] if isinstance(c, str))
        declared = node["attrs"].get("type", "").strip().lower()
        assignment: str | None = None
        try:
            if declared in _JSON_TYPES:
                data = parse_json(body)
            elif not declared and (match := _ASSIGNMENT.fullmatch(body)):
                # A pure literal only: the value parses as strict JSON, or the block is code.
                data, assignment = parse_json(match.group(2)), match.group(1)
            else:
                self.removed("SCRIPT_NOT_LITERAL", node)
                return None
        except (ValueError, NonFiniteValue):
            self.removed("SCRIPT_NOT_LITERAL", node)
            return None
        data = self.data(data, node)
        size = len(canonical_json(data).encode("utf-8"))
        attrs = {"type": node["attrs"]["type"]} if "type" in node["attrs"] else {}
        if size > BLOCK_MAX_BYTES or self.embedded_bytes + size > SAMPLE_EMBEDDED_MAX_BYTES:
            # Digest only, for diagnostics; the sample can never support a PASS (V8).
            self.truncated = True
            return {
                "tag": "script",
                "attrs": attrs,
                "children": [],
                "truncated": True,
                "digest": digest("icbm-truncated-block/v1", data),
            }
        self.embedded_bytes += size
        kept: Node = {"tag": "script", "attrs": attrs, "children": [], "data": data}
        if assignment is not None:
            kept["assignment"] = assignment
        return kept

    def _keeps_input_value(self, node: Mapping[str, Any]) -> bool:
        attrs = node["attrs"]
        name = attrs.get("name", "")
        return (
            attrs.get("type", "text").lower() in _SERVER_VALUE_INPUTS
            and not _SECRET_NAME.search(name)
            and not _PRIVATE_NAME.search(name)
            and not _SECRET_VALUE.search(attrs.get("value", ""))
        )

    def attributes(self, node: Node) -> dict[str, str]:
        kept: dict[str, str] = {}
        for name, value in node["attrs"].items():
            lowered = name.lower()
            if lowered.startswith("on"):
                self.removed(f"EVENT_HANDLER:{name}", node)
            elif (
                node["tag"] == "input" and lowered == "value" and not self._keeps_input_value(node)
            ):
                self.removed("INPUT_VALUE", node)
            elif _SECRET_NAME.search(name) or _SECRET_VALUE.search(value):
                self.removed(f"SECRET_ATTRIBUTE:{name}", node)
            elif _PRIVATE_NAME.search(name):
                self.removed(f"PRIVATE_ATTRIBUTE:{name}", node)
            elif lowered in _URL_ATTRIBUTES:
                kept[name] = self._strip_query(value, f"URL_QUERY:{name}", node)
            else:
                kept[name] = value
        return kept

    def element(self, node: Node) -> Node | None:
        tag = node["tag"]
        if tag in {"style", "template", "noscript", "iframe", "object"}:
            return None
        if tag == "script":
            return self.script(node)
        region = _region(node)
        if region is not None:
            self.excluded.append([boundary_of(node), region.value])
            return None
        tokens = _tokens(node)
        operator = next((e for e in self.scope.exclusions if e.token.lower() in tokens), None)
        if operator is not None:
            self.excluded.append([boundary_of(node), f"OPERATOR_{operator.reason.value}"])
            return None
        children: list[Any] = []
        for child in node["children"]:
            if isinstance(child, str):
                children.append(child)
            elif (kept := self.element(child)) is not None:
                children.append(kept)
        return {"tag": tag, "attrs": self.attributes(node), "children": children}


# ---------------------------------------------------------------- the final gate


def final_scan(structure: Mapping[str, Any]) -> list[str]:
    """An independent last pass over a sanitized structure: every attribute name and value, every
    text, every embedded key and value. Findings are ``kind@boundary`` only, never values."""
    findings: list[str] = []

    def embedded(value: Any, where: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if _SECRET_NAME.search(key) or _PRIVATE_NAME.search(key):
                    findings.append(f"EMBEDDED_KEY@{where}")
                embedded(item, where)
        elif isinstance(value, list):
            for item in value:
                embedded(item, where)
        elif isinstance(value, str):
            if _RESIDUAL_VALUE.search(value):
                findings.append(f"EMBEDDED_VALUE@{where}")
            if _URL_TEXT.match(value) and ("?" in value or "#" in value):
                findings.append(f"EMBEDDED_URL_QUERY@{where}")

    def visit(node: Mapping[str, Any]) -> None:
        where = boundary_of(node)
        for name, value in node.get("attrs", {}).items():
            lowered = name.lower()
            if lowered.startswith("on") or _SECRET_NAME.search(name) or _PRIVATE_NAME.search(name):
                findings.append(f"ATTRIBUTE_NAME:{name}@{where}")
            if _RESIDUAL_VALUE.search(value):
                findings.append(f"ATTRIBUTE_VALUE:{name}@{where}")
            if lowered in _URL_ATTRIBUTES and ("?" in value or "#" in value):
                findings.append(f"URL_QUERY:{name}@{where}")
        if "data" in node:
            embedded(node["data"], where)
        for child in node.get("children", ()):
            if isinstance(child, str):
                if _RESIDUAL_VALUE.search(child):
                    findings.append(f"TEXT@{where}")
            else:
                visit(child)

    visit(structure)
    return findings


def capture_sample(
    html: str, scope: OperatorScope, expected: Mapping[str, Any]
) -> ValidationSample:
    """Cut one sample from the operator's product boundary. It takes no profile."""
    unconfirmed = [
        found
        for found, region in detect_regions(html, scope.product_boundary)
        if region is not RegionClass.NAVIGATION and found not in scope.confirmed_regions
    ]
    if unconfirmed:
        raise CaptureRefused(f"the operator has not confirmed excluded regions: {unconfirmed}")
    boundary = resolve_boundary(_read(html), scope.product_boundary)
    sanitizer = _Sanitizer(scope)
    region = sanitizer.element(boundary)
    if region is None:
        raise CaptureRefused("the product boundary was excluded by the operator's own scope")
    structure: Node = {"tag": "#document", "attrs": {}, "children": [region]}
    if residual := final_scan(structure):
        raise CaptureRefused(f"residual secret or private material: {residual}")
    provenance = {
        "capture_revision": CAPTURE_REVISION,
        "scope": {
            "decided_by": scope.decided_by,
            "decided_at": scope.decided_at,
            "product_boundary": scope.product_boundary,
            "boundary_element": boundary_of(boundary),
            "confirmed_regions": list(scope.confirmed_regions),
            "exclusions": [[e.token, e.reason.value] for e in scope.exclusions],
        },
        "excluded": sanitizer.excluded,
        "removals": sanitizer.removals,
    }
    expected_payload = dict(expected)
    return ValidationSample(
        structure_json=canonical_json(structure),
        expected_json=canonical_json(expected_payload),
        provenance_json=canonical_json(provenance),
        truncated=sanitizer.truncated,
        digest=sample_digest(structure, expected_payload, provenance, sanitizer.truncated),
    )


def sample_digest(
    structure: Mapping[str, Any],
    expected: Mapping[str, Any],
    provenance: Mapping[str, Any],
    truncated: bool,
) -> str:
    """A sample's content digest, recomputable from its stored parts (read-back, tamper check)."""
    return digest(
        SAMPLE_DIGEST_SCHEME,
        {
            "structure": dict(structure),
            "expected": dict(expected),
            "provenance": dict(provenance),
            "truncated": truncated,
        },
    )


def residual_findings(text: str) -> list[str]:
    """The final scan's value patterns over one text, for V6 over engine output."""
    return [match.group(0)[:6] + "…" for match in _RESIDUAL_VALUE.finditer(text)]
