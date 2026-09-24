"""The engine's own element tree (ADR-0017 §1).

The engine reads either an HTML document (its own parser, below) or a ``ValidationSample``
snapshot (``from_snapshot``). The capture owner has a separate parser (``capture.py``): the thing
that cuts proof material and the thing that is validated against it never share a reader.
"""

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_NO_TEXT = frozenset({"script", "style"})
_HIDDEN_CLASSES = frozenset({"hidden", "displaynone", "sr-only", "invisible"})
_JSON_TYPES = frozenset({"application/ld+json", "application/json"})
# One top-level assignment of one value: ``var name = <json>;``. The value must parse as JSON.
_ASSIGNMENT = re.compile(r"^\s*(?:var|let|const)?\s*([A-Za-z_$][\w$.]*)\s*=\s*(.+?);?\s*$", re.S)


def _hides(style: str) -> bool:
    declarations: dict[str, str] = {}
    for part in style.split(";"):
        name, _, value = part.partition(":")
        declarations[name.strip().lower()] = value.strip().lower().replace("!important", "").strip()
    try:
        transparent = float(declarations.get("opacity", "1") or "1") == 0.0
    except ValueError:
        transparent = False
    return (
        declarations.get("display") == "none"
        or declarations.get("visibility") in {"hidden", "collapse"}
        or transparent
    )


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    index: int
    parent: "Node | None" = field(default=None, repr=False)
    children: "list[Node | str]" = field(default_factory=list, repr=False)
    # A parsed embedded data tree, for a JSON script or a pure-literal assignment.
    data: Any = None
    assignment: str | None = None

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attrs.get("class", "").split())

    @property
    def element_id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def hidden(self) -> bool:
        """Not presented to the reader: ``hidden``, ``aria-hidden``, ``display:none``,
        ``visibility:hidden``, ``opacity:0`` or a hiding class, on it or on any ancestor."""
        node: Node | None = self
        while node is not None:
            if (
                "hidden" in node.attrs
                or node.attrs.get("aria-hidden", "").strip().lower() == "true"
                or _hides(node.attrs.get("style", ""))
                or node.classes & _HIDDEN_CLASSES
            ):
                return True
            node = node.parent
        return False

    @property
    def disabled(self) -> bool:
        """Not operable: ``disabled`` or ``aria-disabled="true"``, on it or on a disabled
        ancestor fieldset."""
        node: Node | None = self
        while node is not None:
            if "disabled" in node.attrs and (node is self or node.tag == "fieldset"):
                return True
            if node.attrs.get("aria-disabled", "").strip().lower() == "true":
                return True
            node = node.parent
        return False

    def descendants(self) -> Iterator["Node"]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.descendants()

    def iter(self) -> Iterator["Node"]:
        yield self
        yield from self.descendants()

    def text(self) -> str:
        if self.tag in _NO_TEXT:
            return ""
        parts: list[str] = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return " ".join(" ".join(parts).split())

    def own_cells(self) -> list["Node"]:
        return [c for c in self.children if isinstance(c, Node) and c.tag in {"th", "td"}]


def read_embedded(node: Node) -> None:
    """Give a script node its data tree when it is JSON, or one pure-literal assignment."""
    if node.tag != "script":
        return
    body = "".join(c for c in node.children if isinstance(c, str))
    kind = node.attrs.get("type", "").strip().lower()
    try:
        if kind in _JSON_TYPES:
            node.data = json.loads(body)
        elif not kind:
            match = _ASSIGNMENT.fullmatch(body)
            if match:
                node.data = json.loads(match.group(2))
                node.assignment = match.group(1)
    except ValueError:
        node.data = None
        node.assignment = None


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, 0)
        self._stack = [self.root]
        self._count = 0

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> Node:
        self._count += 1
        parent = self._stack[-1]
        node = Node(tag, {k: v or "" for k, v in attrs}, self._count, parent)
        parent.children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._open(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                closed = self._stack[depth:]
                del self._stack[depth:]
                for node in closed:
                    read_embedded(node)
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def parse_html(text: str) -> Node:
    builder = _Builder()
    builder.feed(text)
    builder.close()
    for node in builder._stack[1:]:
        read_embedded(node)
    return builder.root


def from_snapshot(tree: Mapping[str, Any]) -> Node:
    """Rebuild an element tree from a ``ValidationSample`` snapshot, in document order."""
    counter = [0]

    def build(item: Mapping[str, Any], parent: Node | None) -> Node:
        node = Node(str(item["tag"]), dict(item.get("attrs", {})), counter[0], parent)
        counter[0] += 1
        node.data = item.get("data")
        node.assignment = item.get("assignment")
        for child in item.get("children", []):
            if isinstance(child, str):
                node.children.append(child)
            else:
                node.children.append(build(child, node))
        return node

    return build(tree, None)


def to_snapshot(node: Node) -> dict[str, Any]:
    """The inverse of ``from_snapshot``, for the deterministic V4 mutation suite."""
    item: dict[str, Any] = {"tag": node.tag, "attrs": dict(node.attrs), "children": []}
    if node.data is not None:
        item["data"] = node.data
    if node.assignment is not None:
        item["assignment"] = node.assignment
    for child in node.children:
        item["children"].append(child if isinstance(child, str) else to_snapshot(child))
    return item
