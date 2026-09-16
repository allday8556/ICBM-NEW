"""Reading a KM통상 product document as structure (ADR-0010 §3).

Site knowledge is allowed to know where this storefront states a fact; it is not allowed to know
how a document was fetched. This turns immutable bytes into elements, attributes and text, and
nothing else: it opens nothing, stores nothing and decides nothing about a request.

Text is kept per element so a rule can quote the exact words the page used as evidence, and script
and style contents are never text — a value that only a script mentions was never stated to the
reader (Issue #52 ruling 5702780630).
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
# A row or cell the storefront hides is still the page's own statement of a fact, but a hidden
# purchase or sold-out control is not an offer to the reader.
HIDDEN_MARKERS = ("displaynone", "display:none", "hidden")


def _collapse(words: Iterator[str]) -> str:
    return " ".join("".join(words).split())


@dataclass
class Node:
    """One element of the document: what it holds, what it says and where it sits."""

    tag: str
    element_id: str
    classes: tuple[str, ...]
    attributes: Mapping[str, str]
    ancestors: tuple[str, ...] = ()
    # The element's own contents in source order — its words and the elements written inside it.
    content: "list[str | Node]" = field(default_factory=list, repr=False)
    hidden: bool = False

    def _words(self, *, painted: bool, skip: tuple[str, ...]) -> Iterator[str]:
        for item in self.content:
            if isinstance(item, str):
                yield item
                continue
            if any(item.marks(marker) for marker in skip):
                continue  # a subtree the rule means to leave out
            if painted and item.hidden:
                continue  # a hidden child's words belong to the child
            yield from item._words(painted=painted, skip=skip)

    @property
    def text(self) -> str:
        """Everything written inside this element, painted or not.

        A hidden row is still the page's own statement of a fact, so a label and its value are
        read from here.
        """
        return _collapse(self._words(painted=False, skip=()))

    @property
    def visible_text(self) -> str:
        """Only what a reader is shown.

        An element's words are not made visible by a visible container around them: a hidden
        child's words belong to the child. A control's state is read from here, so a sold-out mark
        kept in a hidden template never reads as an offer withdrawn.
        """
        if self.hidden:
            return ""
        return _collapse(self._words(painted=True, skip=()))

    def text_outside(self, *markers: str) -> str:
        """This element's words with whole marked subtrees left out.

        A tab strip written inside a description block is navigation, not description. A rule says
        here what it means to leave out, rather than subtracting strings afterwards.
        """
        return _collapse(self._words(painted=False, skip=markers))

    def descendants(self) -> Iterator["Node"]:
        """Every element written inside this one, in source order."""
        for item in self.content:
            if isinstance(item, Node):
                yield item
                yield from item.descendants()

    def marks(self, token: str) -> bool:
        token = token.lower()
        return token == self.element_id.lower() or any(
            name.lower() == token or name.lower().startswith(token) for name in self.classes
        )

    def within(self, token: str) -> bool:
        token = token.lower()
        return any(token == name or name.startswith(token) for name in self.ancestors)

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

    def _ancestors(self) -> tuple[str, ...]:
        names: list[str] = []
        for node in self._open:
            names.append(node.tag)
            if node.element_id:
                names.append(node.element_id.lower())
            names.extend(name.lower() for name in node.classes)
        return tuple(names)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = tuple(name for name in values.get("class", "").split() if name)
        marked = f"{values.get('class', '')} {values.get('style', '')}".lower()
        node = Node(
            tag=tag,
            element_id=values.get("id", "").strip(),
            classes=classes,
            attributes=values,
            ancestors=self._ancestors(),
            hidden=any(marker in marked for marker in HIDDEN_MARKERS)
            or any(node.hidden for node in self._open),
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


def find(nodes: Sequence[Node], tag: str | None = None, **marks: str) -> Iterator[Node]:
    """Elements of a tag, optionally carrying a marker on themselves."""
    for node in nodes:
        if tag is not None and node.tag != tag:
            continue
        if all(node.marks(token) for token in marks.values()):
            yield node


def meta(nodes: Sequence[Node]) -> dict[str, str]:
    """``property``/``name`` to ``content``, for the document's declared metadata."""
    found: dict[str, str] = {}
    for node in nodes:
        if node.tag != "meta":
            continue
        key = node.attributes.get("property") or node.attributes.get("name") or ""
        content = node.attributes.get("content", "")
        if key and content and key not in found:
            found[key] = content
    return found
