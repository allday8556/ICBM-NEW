"""Reading a captured 1688 page as structure (ADR-0010 §3).

This turns immutable text into elements, attributes and words, and nothing else: it opens nothing,
stores nothing and decides nothing about a request. Script and style contents are never words — the
capture carries none (ADR-0019 §6), and a value only a script mentions was never stated to the
reader.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser

# HTML's void elements never have an end tag, so they never open a scope.
VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_UNTEXT = frozenset({"script", "style"})


@dataclass
class Node:
    """One element of the document: what it holds, what it says and where it sits."""

    tag: str
    element_id: str
    classes: tuple[str, ...]
    attributes: Mapping[str, str]
    parent: "Node | None" = field(default=None, repr=False)
    content: "list[str | Node]" = field(default_factory=list, repr=False)

    @property
    def text(self) -> str:
        """Everything written inside this element, with whitespace collapsed."""
        return " ".join("".join(self._words()).split())

    def _words(self) -> Iterator[str]:
        for item in self.content:
            if isinstance(item, str):
                yield item
            else:
                yield from item._words()

    def descendants(self) -> Iterator["Node"]:
        """Every element written inside this one, in source order."""
        for item in self.content:
            if isinstance(item, Node):
                yield item
                yield from item.descendants()

    def has_class(self, token: str) -> bool:
        return token in self.classes

    def inside(self, *, element_id: str = "", class_token: str = "") -> bool:
        """Whether an ancestor carries this id or this class token."""
        at = self.parent
        while at is not None:
            if (element_id and at.element_id == element_id) or (
                class_token and at.has_class(class_token)
            ):
                return True
            at = at.parent
        return False


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[Node] = []
        self._open: list[Node] = []
        self._untext = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        node = Node(
            tag=tag,
            element_id=values.get("id", "").strip(),
            classes=tuple(name for name in values.get("class", "").split() if name),
            attributes=values,
            parent=self._open[-1] if self._open else None,
        )
        self.nodes.append(node)
        if self._open:
            self._open[-1].content.append(node)
        if tag in _UNTEXT:
            self._untext += 1
        if tag not in VOID_ELEMENTS:
            self._open.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in _UNTEXT and self._untext:
            self._untext -= 1
        if tag in VOID_ELEMENTS:
            return
        for depth in range(len(self._open) - 1, -1, -1):
            if self._open[depth].tag == tag:
                del self._open[depth:]
                return

    def handle_data(self, data: str) -> None:
        if self._untext:
            return
        if self._open:
            self._open[-1].content.append(data)


def read(body: str) -> tuple[Node, ...]:
    """Every element of the document in source order."""
    reader = _Reader()
    reader.feed(body)
    reader.close()
    return tuple(reader.nodes)
