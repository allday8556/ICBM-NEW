"""What identifies a KM통상 product at its source (Issue #52 ruling 5702780630, P0).

The source identity is the spine of every revision: it decides whether a collection appends to a
product's history or starts a new one. So it is frozen here against the evidence phase A retained,
and it is never guessed.

**The rule.** The identity is the product number the page declares in
``<meta property="product:productId">``. It is a digit string, and the page must agree with itself:
``product:retailer_item_id``, the canonical link's last path segment and the product URL's own
number all state the same value in the retained captures.

**Why not the name.** The name sits in its own path segment and in ``og:title``; renaming a product
changes both and changes nothing about the product. A name, or a hash of one, is therefore never an
identity — a rename would silently start a second history for one product.

**When it cannot be read.** A missing number, a number that is not digits, or two declarations that
disagree leave the identity *unresolved*. The caller records no revision at all rather than one
under an invented identity.
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


IdentityResult = SourceIdentity | UnresolvedIdentity

UNRESOLVED_ABSENT = "the page declares no product number"
UNRESOLVED_SHAPE = "the declared product number is not a digit string"
UNRESOLVED_DISAGREE = "the page's declarations of the product number disagree"


def _canonical_number(nodes: Sequence[Node]) -> str | None:
    for node in nodes:
        if node.tag != "link" or CANONICAL_REL not in node.attributes.get("rel", "").lower():
            continue
        path = urlsplit(node.attributes.get("href", "")).path
        digits = [part for part in path.split("/") if part.isdigit()]
        return digits[-1] if digits else None
    return None


def _url_number(source_url: str) -> str | None:
    digits = [part for part in urlsplit(source_url).path.split("/") if part.isdigit()]
    # The product number is the segment that follows the product name, not the category or the
    # display flag that follow it.
    return digits[0] if digits else None


def resolve(document: DocumentView, source_url: str) -> IdentityResult:
    """The product's source identity, or why it could not be read."""
    nodes = read(document.body)
    declared = meta(nodes)
    number = declared.get(IDENTITY_META, "").strip()
    if not number:
        return UnresolvedIdentity(UNRESOLVED_ABSENT)
    if not number.isdigit():
        return UnresolvedIdentity(UNRESOLVED_SHAPE)
    agreed = [f"meta[{IDENTITY_META}]"]
    corroborations = {
        f"meta[{CORROBORATING_META}]": declared.get(CORROBORATING_META, "").strip() or None,
        "link[canonical]": _canonical_number(nodes),
        "url": _url_number(source_url),
    }
    for where, value in corroborations.items():
        if value is None:
            continue  # a declaration the page did not make cannot disagree with one it did
        if value != number:
            return UnresolvedIdentity(UNRESOLVED_DISAGREE, tuple(sorted({number, value})))
        agreed.append(where)
    return SourceIdentity(source_product_id=number, agreed=tuple(agreed))
