"""A bounded selector language (ADR-0017 §3: no expression language, no unbounded pattern).

``tag#id.class[attr]`` or ``[attr=value]`` compounds, joined by a descendant space or a ``>``
child combinator. At most eight compounds and 200 characters. Nothing else is accepted.
"""

import re
from dataclasses import dataclass

from prototypes.adaptive_collector.dom import Node

MAX_SELECTOR_LENGTH = 200
MAX_COMPOUNDS = 8
_IDENT = r"[A-Za-z_][\w-]*"
_COMPOUND = re.compile(
    rf"^(?P<tag>{_IDENT}|\*)?(?P<id>#{_IDENT})?(?P<classes>(?:\.{_IDENT})*)"
    rf"(?P<attrs>(?:\[{_IDENT}(?:=[^\]\s]+)?\])*)$"
)
_ATTR = re.compile(rf"\[({_IDENT})(?:=([^\]\s]+))?\]")


class SelectorError(ValueError):
    pass


@dataclass(frozen=True)
class Compound:
    tag: str | None
    element_id: str | None
    classes: frozenset[str]
    attrs: tuple[tuple[str, str | None], ...]

    def matches(self, node: Node) -> bool:
        if self.tag is not None and node.tag != self.tag:
            return False
        if self.element_id is not None and node.element_id != self.element_id:
            return False
        if not self.classes <= node.classes:
            return False
        for name, value in self.attrs:
            if name not in node.attrs or (value is not None and node.attrs[name] != value):
                return False
        return True


@dataclass(frozen=True)
class Selector:
    source: str
    steps: tuple[tuple[str, Compound], ...]  # (combinator, compound); the first combinator is " "


def parse_selector(text: str) -> Selector:
    if not text or len(text) > MAX_SELECTOR_LENGTH:
        raise SelectorError("a selector is 1 to 200 characters")
    tokens = text.replace(">", " > ").split()
    steps: list[tuple[str, Compound]] = []
    combinator = " "
    for token in tokens:
        if token == ">":
            if not steps or combinator == ">":
                raise SelectorError(f"misplaced child combinator in {text!r}")
            combinator = ">"
            continue
        match = _COMPOUND.fullmatch(token)
        if match is None or not token:
            raise SelectorError(f"not a bounded selector compound: {token!r}")
        tag = match.group("tag")
        steps.append(
            (
                combinator,
                Compound(
                    tag=None if tag in (None, "*") else tag,
                    element_id=(match.group("id") or "")[1:] or None,
                    classes=frozenset(c for c in (match.group("classes") or "").split(".") if c),
                    attrs=tuple(_ATTR.findall(match.group("attrs") or "")),
                ),
            )
        )
        combinator = " "
    if not steps or combinator == ">":
        raise SelectorError(f"incomplete selector {text!r}")
    if len(steps) > MAX_COMPOUNDS:
        raise SelectorError("a selector has at most eight compounds")
    return Selector(text, tuple((c, s) for c, s in steps))


def _matches_at(node: Node, steps: tuple[tuple[str, Compound], ...]) -> bool:
    """Does ``node`` satisfy the last step, with its ancestors satisfying the rest?"""
    combinator, compound = steps[-1]
    if not compound.matches(node):
        return False
    rest = steps[:-1]
    if not rest:
        return True
    ancestor = node.parent
    if combinator == ">":
        return ancestor is not None and _matches_at(ancestor, rest)
    while ancestor is not None:
        if _matches_at(ancestor, rest):
            return True
        ancestor = ancestor.parent
    return False


def select(root: Node, selector: Selector | str) -> list[Node]:
    parsed = parse_selector(selector) if isinstance(selector, str) else selector
    return [node for node in root.descendants() if _matches_at(node, parsed.steps)]
