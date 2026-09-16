"""What identifies a KM통상 product at its source (Issue #52 ruling 5702780630, P0).

The source identity is the spine of every revision: it decides whether a collection appends to a
product's history or starts a new one. So it is frozen here against the evidence phase A retained,
and it is never guessed.

**The rule.** The identity is the product number the page declares in
``<meta property="product:productId">``. It is a digit string, and the page must agree with itself
*everywhere it states the number*: ``product:retailer_item_id``, the canonical link's path and the
product URL's own path must each be present, readable and equal to it. All four declarations state
``355`` in both retained captures.

**Where a path states the number.** This storefront writes a product path as
``/product/<name>/<number>/``, with anything after it — ``/category/23/display/1/`` — belonging to
the listing the reader came from. The number is read from that one position, in the canonical link
and in the product URL alike. A digit found anywhere else in a path is a category or a display
flag, and is never read as an identity.

**Why not the name.** The name sits in its own path segment and in ``og:title``; renaming a product
changes both and changes nothing about the product. A name, or a hash of one, is therefore never an
identity — a rename would silently start a second history for one product.

**When it cannot be read.** A missing or malformed number, a corroboration the page does not make
or that cannot be read in the expected shape, or two declarations that disagree, all leave the
identity *unresolved*. An absent corroboration is not agreement: for the field that decides whether
a history is appended to or split, the parser fails closed and the caller records no revision at
all rather than one under a half-checked identity.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from integrations.suppliers.collection import DocumentView
from integrations.suppliers.kmretail.collect.dom import Node, meta, read

# The page's own declaration of the product number, and the corroborations that must agree with it.
IDENTITY_META = "product:productId"
CORROBORATING_META = "product:retailer_item_id"
CANONICAL_REL = "canonical"
# The accepted shape of a product path: /product/<name>/<number>/...
PRODUCT_PATH_ROOT = "product"
PRODUCT_NUMBER_POSITION = 2


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
    """The differing values, when the page's declarations disagree."""

    missing: tuple[str, ...] = ()
    """The declarations the page does not make, or does not make in a shape that can be read."""


IdentityResult = SourceIdentity | UnresolvedIdentity

UNRESOLVED_ABSENT = "the page declares no product number"
UNRESOLVED_SHAPE = "the declared product number is not a digit string"
UNRESOLVED_INCOMPLETE = "the page does not corroborate the product number everywhere it must"
UNRESOLVED_DISAGREE = "the page's declarations of the product number disagree"


def _path_number(url: str) -> str | None:
    """The product number a KM product path states, or None when the path does not state one."""
    segments = [part for part in urlsplit(url).path.split("/") if part]
    if len(segments) <= PRODUCT_NUMBER_POSITION or segments[0].lower() != PRODUCT_PATH_ROOT:
        return None
    number = segments[PRODUCT_NUMBER_POSITION]
    return number if number.isdigit() else None


def _canonical_number(nodes: Sequence[Node]) -> str | None:
    for node in nodes:
        if node.tag != "link" or CANONICAL_REL not in node.attributes.get("rel", "").lower():
            continue
        return _path_number(node.attributes.get("href", ""))
    return None


def _declared_number(declared: dict[str, str], key: str) -> str | None:
    value = declared.get(key, "").strip()
    return value if value.isdigit() else None


def resolve(document: DocumentView, source_url: str) -> IdentityResult:
    """The product's source identity, or why it could not be read."""
    nodes = read(document.body)
    declared = meta(nodes)
    number = declared.get(IDENTITY_META, "").strip()
    if not number:
        return UnresolvedIdentity(UNRESOLVED_ABSENT, missing=(f"meta[{IDENTITY_META}]",))
    if not number.isdigit():
        return UnresolvedIdentity(UNRESOLVED_SHAPE)
    corroborations = {
        f"meta[{CORROBORATING_META}]": _declared_number(declared, CORROBORATING_META),
        "link[canonical]": _canonical_number(nodes),
        "url": _path_number(source_url),
    }
    missing = tuple(where for where, value in corroborations.items() if value is None)
    if missing:
        # A declaration the page does not make, or one whose path cannot be read in the accepted
        # shape, proves nothing. It is not agreement, and this field may not fail open.
        return UnresolvedIdentity(UNRESOLVED_INCOMPLETE, missing=missing)
    stated = {where: value for where, value in corroborations.items() if value is not None}
    disagreeing = {value for value in stated.values() if value != number}
    if disagreeing:
        return UnresolvedIdentity(UNRESOLVED_DISAGREE, seen=tuple(sorted(disagreeing | {number})))
    return SourceIdentity(source_product_id=number, agreed=(f"meta[{IDENTITY_META}]", *stated))
