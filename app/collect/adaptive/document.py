"""The engine's element tree and its HTML reader (ADR-0017 §1, §7.3).

The engine reads a document handed to it (a ``DocumentView`` body in a later slice) or a
``ValidationSample`` structure. The capture owner has a reader of its own (``capture.py``): what
cuts proof material and what is validated against it never share a parser.

Presentation and operability are read conservatively. An element is *presented* only when neither
it nor an ancestor hides it (``hidden``, ``aria-hidden``, ``display:none``, ``visibility:hidden``
or ``collapse``, ``opacity:0``, a hiding class); it is *operable* only when neither it nor an
enclosing fieldset is disabled (``disabled``, ``aria-disabled="true"``).
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from app.collect.adaptive.canonical import NonFiniteValue, parse_json

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_SCRIPT_TEXT = frozenset({"script", "style", "template"})
_HIDING_CLASSES = frozenset({"hidden", "displaynone", "sr-only", "invisible"})
_JSON_SCRIPT_TYPES = frozenset({"application/ld+json", "application/json"})
_LITERAL_ASSIGNMENT = re.compile(
    r"^\s*(?:(?:var|let|const)\s+)?([A-Za-z_$][\w$.]*)\s*=\s*(.+?)\s*;?\s*$", re.S
)


def _style_hides(style: str) -> bool:
    declared: dict[str, str] = {}
    for declaration in style.split(";"):
        name, _, value = declaration.partition(":")
        declared[name.strip().lower()] = value.replace("!important", "").strip().lower()
    opacity = declared.get("opacity", "")
    try:
        transparent = bool(opacity) and float(opacity) == 0.0
    except ValueError:
        transparent = False
    return (
        declared.get("display") == "none"
        or declared.get("visibility") in {"hidden", "collapse"}
        or transparent
    )


@dataclass(eq=False)
class Element:
    tag: str
    attributes: dict[str, str]
    position: int  # document order, unique within one tree
    parent: "Element | None" = field(default=None, repr=False)
    children: "list[Element | str]" = field(default_factory=list, repr=False)
    # Admissible embedded data: a JSON script, or one pure-literal assignment and its target.
    data: Any = None
    assignment: str | None = None

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attributes.get("class", "").split())

    @property
    def element_id(self) -> str:
        return self.attributes.get("id", "")

    def _lineage(self) -> Iterator["Element"]:
        node: Element | None = self
        while node is not None:
            yield node
            node = node.parent

    @property
    def presented(self) -> bool:
        for node in self._lineage():
            attributes = node.attributes
            if (
                "hidden" in attributes
                or attributes.get("aria-hidden", "").strip().lower() == "true"
                or _style_hides(attributes.get("style", ""))
                or node.classes & _HIDING_CLASSES
            ):
                return False
        return True

    @property
    def operable(self) -> bool:
        for node in self._lineage():
            if node.attributes.get("aria-disabled", "").strip().lower() == "true":
                return False
            if "disabled" in node.attributes and (node is self or node.tag == "fieldset"):
                return False
        return True

    def walk(self) -> Iterator["Element"]:
        """This element and every descendant, in document order."""
        yield self
        for child in self.children:
            if isinstance(child, Element):
                yield from child.walk()

    def descendants(self) -> Iterator["Element"]:
        walker = self.walk()
        next(walker)
        yield from walker

    def own_text(self) -> str:
        return " ".join(" ".join(c for c in self.children if isinstance(c, str)).split())

    def text(self) -> str:
        if self.tag in _SCRIPT_TEXT:
            return ""
        pieces = [c if isinstance(c, str) else c.text() for c in self.children]
        return " ".join(" ".join(pieces).split())

    def cells(self) -> list["Element"]:
        return [c for c in self.children if isinstance(c, Element) and c.tag in {"th", "td"}]


def read_embedded(element: Element) -> None:
    """Attach a script's data tree when it is JSON or one pure-literal assignment, else nothing."""
    if element.tag != "script":
        return
    body = "".join(c for c in element.children if isinstance(c, str))
    declared = element.attributes.get("type", "").strip().lower()
    try:
        if declared in _JSON_SCRIPT_TYPES:
            element.data = parse_json(body)
        elif not declared and (match := _LITERAL_ASSIGNMENT.fullmatch(body)):
            element.data = parse_json(match.group(2))
            element.assignment = match.group(1)
    except (ValueError, NonFiniteValue):
        element.data, element.assignment = None, None


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, 0)
        self._open = [self.root]
        self._next = 1

    def _add(self, tag: str, attrs: list[tuple[str, str | None]]) -> Element:
        element = Element(tag, {k: v or "" for k, v in attrs}, self._next, parent=self._open[-1])
        self._next += 1
        self._open[-1].children.append(element)
        return element

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = self._add(tag, attrs)
        if tag not in _VOID:
            self._open.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self._open) - 1, 0, -1):
            if self._open[depth].tag == tag:
                for closed in self._open[depth:]:
                    read_embedded(closed)
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)


def read_html(text: str) -> Element:
    reader = _Reader()
    reader.feed(text)
    reader.close()
    for unclosed in reader._open[1:]:
        read_embedded(unclosed)
    return reader.root


def from_structure(tree: Mapping[str, Any]) -> Element:
    """Rebuild a tree from a ``ValidationSample`` structure, numbering it in document order."""
    counter = 0

    def build(item: Mapping[str, Any], parent: Element | None) -> Element:
        nonlocal counter
        element = Element(str(item["tag"]), dict(item.get("attrs", {})), counter, parent=parent)
        counter += 1
        element.data = item.get("data")
        element.assignment = item.get("assignment")
        for child in item.get("children", ()):
            element.children.append(child if isinstance(child, str) else build(child, element))
        return element

    return build(tree, None)


def to_structure(element: Element) -> dict[str, Any]:
    item: dict[str, Any] = {"tag": element.tag, "attrs": dict(element.attributes), "children": []}
    if element.data is not None:
        item["data"] = element.data
    if element.assignment is not None:
        item["assignment"] = element.assignment
    for child in element.children:
        item["children"].append(child if isinstance(child, str) else to_structure(child))
    return item
