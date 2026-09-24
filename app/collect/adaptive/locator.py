"""A bounded, declarative locator language (ADR-0017 §3: no expression language).

Grammar::

    locator   := step (combinator step)*          at most 8 steps, at most 200 characters
    combinator:= " " (descendant) | ">" (child)
    step      := [tag | "*"] ["#" ident] ("." ident)* ("[" ident ["=" value] "]")*

A step that matches nothing of this grammar — a pseudo-class, a function, a quote, a sibling
combinator — is refused, so a profile can never smuggle an expression into a locator.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

from app.collect.adaptive.document import Element

MAX_LENGTH = 200
MAX_STEPS = 8
_NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
_STEP = re.compile(
    rf"(?P<tag>{_NAME}|\*)?(?P<id>#{_NAME})?(?P<classes>(?:\.{_NAME})*)"
    rf"(?P<attrs>(?:\[{_NAME}(?:=[^\]\s\"'<>]+)?\])*)"
)
_ATTRIBUTE = re.compile(rf"\[({_NAME})(?:=([^\]\s\"'<>]+))?\]")


class LocatorError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    child_of_previous: bool
    tag: str | None
    element_id: str | None
    classes: frozenset[str]
    attributes: tuple[tuple[str, str | None], ...]

    def admits(self, element: Element) -> bool:
        if self.tag is not None and element.tag != self.tag:
            return False
        if self.element_id is not None and element.element_id != self.element_id:
            return False
        if not self.classes <= element.classes:
            return False
        return all(
            name in element.attributes and (value is None or element.attributes[name] == value)
            for name, value in self.attributes
        )


@dataclass(frozen=True)
class Locator:
    text: str
    steps: tuple[Step, ...]


@lru_cache(maxsize=1024)
def compile_locator(text: str) -> Locator:
    if not text.strip() or len(text) > MAX_LENGTH:
        raise LocatorError("a locator has 1 to 200 characters")
    tokens = text.replace(">", " > ").split()
    steps: list[Step] = []
    child = False
    for token in tokens:
        if token == ">":
            if not steps or child:
                raise LocatorError(f"a child combinator needs a step on both sides: {text!r}")
            child = True
            continue
        match = _STEP.fullmatch(token)
        if match is None or not any(match.groupdict().values()):
            raise LocatorError(f"not a bounded locator step: {token!r}")
        tag = match.group("tag")
        steps.append(
            Step(
                child_of_previous=child,
                tag=None if tag in (None, "*") else tag,
                element_id=match.group("id")[1:] if match.group("id") else None,
                classes=frozenset(filter(None, (match.group("classes") or "").split("."))),
                attributes=tuple(
                    (name, value or None)
                    for name, value in _ATTRIBUTE.findall(match.group("attrs"))
                ),
            )
        )
        child = False
    if not steps or child:
        raise LocatorError(f"a locator ends with a step: {text!r}")
    if len(steps) > MAX_STEPS:
        raise LocatorError("a locator has at most eight steps")
    return Locator(text, tuple(steps))


def _satisfies(element: Element, steps: tuple[Step, ...]) -> bool:
    last = steps[-1]
    if not last.admits(element):
        return False
    if len(steps) == 1:
        return True
    ancestor = element.parent
    if last.child_of_previous:
        return ancestor is not None and _satisfies(ancestor, steps[:-1])
    while ancestor is not None:
        if _satisfies(ancestor, steps[:-1]):
            return True
        ancestor = ancestor.parent
    return False


def find(root: Element, locator: str | Locator) -> list[Element]:
    """Every descendant of ``root`` the locator names, in document order."""
    compiled = compile_locator(locator) if isinstance(locator, str) else locator
    return [element for element in root.descendants() if _satisfies(element, compiled.steps)]
