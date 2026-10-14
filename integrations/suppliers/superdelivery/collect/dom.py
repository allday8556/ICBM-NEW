"""Reading a Super Delivery product capture as structure (ADR-0010 §3, ADR-0040 §2).

The same reader as the Godomall template's: a page is elements, attributes and visible text, and
nothing a script says is text. Each supplier package owns its own copy (a repository rule keeps a
package's imports to its own modules), so this package's extraction identity covers exactly what it
reads. Invisible characters are dropped and a no-break space is a space.

Two additions over the template's copy, both for words kept as the page wrote them:

- :meth:`Node.lines` keeps a line wherever the page breaks one (``<br>`` or a block element), so a
  statement written on its own line — ``販売元：…``, ``■生産地：…`` — is read on its own;
- :attr:`Node.own_text` is only the words written directly inside an element, not inside its
  children, so a set's 内訳 is read without the JAN and maker part number written beneath it.

Site knowledge is allowed to know where this storefront states a fact; it is not allowed to know
how a document was fetched. This turns immutable bytes into elements, attributes and text, and
nothing else: it opens nothing, stores nothing and decides nothing about a request.
"""

from collections.abc import Iterator, Mapping, Sequence
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
# Elements that stand on their own line or in their own cell: their words never run together with
# a neighbour's.
BLOCK_ELEMENTS = frozenset(
    {
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "li",
        "ol",
        "p",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
# A hidden element is still the page's own statement of a fact, but a hidden control is no offer
# to the reader. Hidden is read exactly: the ``hidden`` attribute, a hiding class token, or a style
# that hides (spaces ignored).
HIDDEN_CLASSES = frozenset({"displaynone", "hidden"})
HIDDEN_STYLES = ("display:none", "visibility:hidden")
# Characters that show nothing and join nothing. A no-break space is a space.
INVISIBLE = "﻿​⁠"
_UNSHOWN = str.maketrans({**dict.fromkeys(INVISIBLE, None), " ": " "})
_LINE = "\n"


def _hides(values: Mapping[str, str]) -> bool:
    if "hidden" in values:
        return True
    if any(name.lower() in HIDDEN_CLASSES for name in values.get("class", "").split()):
        return True
    style = "".join(values.get("style", "").lower().split())
    return any(marker in style for marker in HIDDEN_STYLES)


def clean(text: str) -> str:
    """Text as a reader sees it: invisible characters removed and whitespace collapsed."""
    return " ".join(text.translate(_UNSHOWN).split())


@dataclass
class Node:
    """One element of the document: what it holds, what it says and where it sits."""

    tag: str
    element_id: str
    classes: tuple[str, ...]
    attributes: Mapping[str, str]
    # The element's own contents in source order — its words and the elements written inside it.
    content: "list[str | Node]" = field(default_factory=list, repr=False)
    hidden: bool = False

    def _words(self, *, painted: bool, skip: tuple[str, ...], lines: bool) -> Iterator[str]:
        for item in self.content:
            if isinstance(item, str):
                yield item
                continue
            if item.tag in skip or any(item.marks(marker) for marker in skip):
                continue  # a subtree the rule means to leave out
            if painted and item.hidden:
                continue  # a hidden child's words belong to the child
            apart = item.tag in BLOCK_ELEMENTS
            if apart:
                yield _LINE if lines else " "
            yield from item._words(painted=painted, skip=skip, lines=lines)
            if apart:
                yield _LINE if lines else " "

    @property
    def text(self) -> str:
        """Everything written inside this element, painted or not, as one line."""
        return clean("".join(self._words(painted=False, skip=(), lines=False)))

    @property
    def visible_text(self) -> str:
        """Only what a reader is shown: a hidden child's words belong to the child."""
        if self.hidden:
            return ""
        return clean("".join(self._words(painted=True, skip=(), lines=False)))

    @property
    def own_text(self) -> str:
        """Only the words written directly inside this element, not inside its children."""
        return clean("".join(item for item in self.content if isinstance(item, str)))

    def text_outside(self, *markers: str) -> str:
        """This element's words with whole subtrees left out: a tag name, ``#id`` or ``.class``."""
        return clean("".join(self._words(painted=False, skip=markers, lines=False)))

    def lines(self, *markers: str) -> tuple[str, ...]:
        """This element's words, one entry per line the page wrote, empty lines dropped."""
        joined = "".join(self._words(painted=False, skip=markers, lines=True))
        return tuple(line for line in (clean(part) for part in joined.split(_LINE)) if line)

    @property
    def children(self) -> tuple["Node", ...]:
        """The elements written directly inside this one, in source order."""
        return tuple(item for item in self.content if isinstance(item, Node))

    def descendants(self) -> Iterator["Node"]:
        """Every element written inside this one, in source order."""
        for item in self.content:
            if isinstance(item, Node):
                yield item
                yield from item.descendants()

    def marks(self, token: str) -> bool:
        """Whether the element carries ``token`` exactly: ``#name`` its id, ``.name`` one of its
        classes. Nothing is matched by a prefix: this storefront's class names are reused widely."""
        if token.startswith("#"):
            return token[1:] == self.element_id
        if token.startswith("."):
            return token[1:] in self.classes
        return False

    def find(self, *tokens: str, tag: str | None = None) -> Iterator["Node"]:
        """The elements inside this one that carry every token (and are of ``tag``, if named)."""
        for node in self.descendants():
            if (tag is None or node.tag == tag) and all(node.marks(t) for t in tokens):
                yield node

    @property
    def selector(self) -> str:
        ident = f"#{self.element_id}" if self.element_id else ""
        classes = "".join(f".{name}" for name in self.classes[:2])
        return f"{self.tag}{ident}{classes}"


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
            hidden=_hides(values) or any(node.hidden for node in self._open),
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
            return  # what only a script says was never said to the reader
        if self._open:
            self._open[-1].content.append(data)


def read(body: str) -> tuple[Node, ...]:
    """Every element of the document in source order."""
    reader = _Reader()
    reader.feed(body)
    reader.close()
    return tuple(reader.nodes)


def find(nodes: Sequence[Node], *tokens: str, tag: str | None = None) -> Iterator[Node]:
    """The elements of the document that carry every token (and are of ``tag``, if named)."""
    for node in nodes:
        if (tag is None or node.tag == tag) and all(node.marks(t) for t in tokens):
            yield node


def first(nodes: Sequence[Node], *tokens: str, tag: str | None = None) -> Node | None:
    return next(find(nodes, *tokens, tag=tag), None)
