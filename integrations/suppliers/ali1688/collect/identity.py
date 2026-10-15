"""What identifies a 1688 product at its source (ADR-0040 §1, ADR-0010 §3).

**The rule.** The identity is the offer number of the page's own path, ``/offer/<number>.html``.
It is a digit string, and the capture must agree with itself everywhere it states the number:

- the canonical link the page declares (``<link rel="canonical">``, the one head element the capture
  policy keeps) names the same path;
- the embedded-data member's ``gallery.fields.offerId``, when the member was accepted, is the same
  number.

A missing or malformed number, a canonical link the page does not declare, or two declarations that
disagree leave the identity *unresolved*, and the caller records no revision rather than one under a
half-checked identity. A title, an item number (货号) or a SKU id is never an identity.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from integrations.suppliers.ali1688.collect.dom import read
from integrations.suppliers.ali1688.collect.embedded import OFFER_ID, at, member
from integrations.suppliers.collection import DocumentView

STOREFRONT_HOST = "detail.1688.com"
_PATH = re.compile(r"^/offer/([0-9]{1,20})\.html$")


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
UNRESOLVED_INCOMPLETE = "the page does not declare its canonical product link"
UNRESOLVED_DISAGREE = "the page's declarations of the offer number disagree"


def path_number(url: str) -> str | None:
    """The offer number a 1688 product URL states, or None."""
    parts = urlsplit(url)
    if parts.hostname != STOREFRONT_HOST:
        return None
    match = _PATH.fullmatch(parts.path)
    return match.group(1) if match else None


def resolve(document: DocumentView, source_url: str) -> IdentityResult:
    """The product's source identity, or why it could not be read."""
    number = path_number(source_url)
    if number is None:
        return UnresolvedIdentity(UNRESOLVED_PATH, missing=("url",))
    canonical = [
        path_number(node.attributes.get("href", ""))
        for node in read(document.body)
        if node.tag == "link" and "canonical" in node.attributes.get("rel", "").lower().split()
    ]
    if len(canonical) != 1 or canonical[0] is None:
        return UnresolvedIdentity(UNRESOLVED_INCOMPLETE, missing=("link[canonical]",))
    seen = {canonical[0]}
    agreed = ["url", "link[canonical]"]
    data, _ = member(document)
    offer = at(data, *OFFER_ID) if data is not None else None
    if isinstance(offer, int) and not isinstance(offer, bool):
        seen.add(str(offer))
        agreed.append("embedded:offerId")
    if seen != {number}:
        return UnresolvedIdentity(UNRESOLVED_DISAGREE, seen=tuple(sorted({number, *seen})))
    return SourceIdentity(source_product_id=number, agreed=tuple(agreed))
