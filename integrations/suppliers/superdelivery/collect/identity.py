"""What identifies a Super Delivery product at its source (ADR-0040 §2, ADR-0010 §3).

**The rule.** The identity is the product number of the page's own path, ``/p/r/pd_p/<number>/``:
the SD 商品番号. It is a digit string, and the capture must agree with itself everywhere it
states the number:

- the canonical link the page declares (``<link rel="canonical">``, the one head element the
  capture policy keeps) names the same path;
- the product block states it as ``SD品番：<number>``;
- every set the page lists names its SD item number as ``SD品番：<number>S<n>``, so a set number
  carries the product number it belongs to.

A missing or malformed number, a declaration the page does not make, or two that disagree leave
the identity *unresolved*, and the caller records no revision rather than one under a half-checked
identity. Nothing else — a name, a JAN, a maker part number — is ever an identity: a JAN names a
manufacturer's article, which several companies on this site may sell under their own numbers.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from integrations.suppliers.collection import DocumentView
from integrations.suppliers.superdelivery.collect.dom import Node, find, read

# /p/r/pd_p/<number>/ — the storefront's product path form (ADR-0040 §1).
PRODUCT_PATH_PARTS = ("p", "r", "pd_p")
CANONICAL_REL = "canonical"
PRODUCT_BLOCK = ".reduce-tax"
PRODUCT_NUMBER = re.compile(r"SD品番[：:]\s*(\d+)$")
SET_NUMBER = re.compile(r"SD品番[：:]\s*(\d+)S(\d+)")


@dataclass(frozen=True)
class SourceIdentity:
    """A resolved identity and the declarations that agreed on it."""

    source_product_id: str
    agreed: tuple[str, ...]


@dataclass(frozen=True)
class UnresolvedIdentity:
    """Why no identity could be read. No revision may be recorded under a guessed one."""

    reason: str
    seen: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


IdentityResult = SourceIdentity | UnresolvedIdentity

UNRESOLVED_PATH = "the URL is not a product path"
UNRESOLVED_INCOMPLETE = "the page does not corroborate the product number everywhere it must"
UNRESOLVED_DISAGREE = "the page's declarations of the product number disagree"


def path_number(url: str) -> str | None:
    """The product number a Super Delivery product path states, or None."""
    segments = [part for part in urlsplit(url).path.split("/") if part]
    if len(segments) != 4 or tuple(segments[:3]) != PRODUCT_PATH_PARTS:
        return None
    number = segments[3]
    return number if number.isascii() and number.isdigit() else None


def _canonical(nodes: Sequence[Node]) -> str | None:
    for node in nodes:
        if node.tag == "link" and CANONICAL_REL in node.attributes.get("rel", "").lower().split():
            return path_number(node.attributes.get("href", ""))
    return None


def _stated(nodes: Sequence[Node]) -> set[str]:
    """Every product number the product block states as ``SD品番：<number>``."""
    return {
        match.group(1)
        for block in find(nodes, PRODUCT_BLOCK)
        for span in block.find(tag="span")
        if (match := PRODUCT_NUMBER.search(span.text)) is not None
    }


def set_numbers(nodes: Sequence[Node]) -> tuple[tuple[str, str], ...]:
    """Every set's (product number, set number) the page states, in page order, once each."""
    found: list[tuple[str, str]] = []
    for node in nodes:
        if node.tag != "span":
            continue
        for match in SET_NUMBER.finditer(node.own_text):
            pair = (match.group(1), match.group(2))
            if pair not in found:
                found.append(pair)
    return tuple(found)


def resolve(document: DocumentView, source_url: str) -> IdentityResult:
    """The product's source identity, or why it could not be read."""
    number = path_number(source_url)
    if number is None:
        return UnresolvedIdentity(UNRESOLVED_PATH, missing=("url",))
    nodes = read(document.body)
    canonical = _canonical(nodes)
    stated = _stated(nodes)
    sets = set_numbers(nodes)
    missing = tuple(
        where
        for where, absent in (
            ("link[canonical]", canonical is None),
            ("SD品番", not stated),
            ("SD品番 of a set", not sets),
        )
        if absent
    )
    if missing or canonical is None:
        return UnresolvedIdentity(UNRESOLVED_INCOMPLETE, missing=missing)
    seen = {canonical, *stated, *(product for product, _ in sets)}
    if seen != {number}:
        return UnresolvedIdentity(UNRESOLVED_DISAGREE, seen=tuple(sorted({number, *seen})))
    return SourceIdentity(
        source_product_id=number, agreed=("url", "link[canonical]", "SD品番", "SD品番 of a set")
    )
