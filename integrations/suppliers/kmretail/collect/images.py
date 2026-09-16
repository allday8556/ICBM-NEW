"""Which image references of a KM통상 product page are product evidence, and which are furniture.

The storefront is Cafe24, so the page marks each image's purpose in its own DOM: the representative
image sits in the product image module's ``keyImg`` container, the additional images in that
module's ``addimage`` list, the description sequence inside ``#prdDetail``, and the layout's own
assets in the header, the promotion banner, the category navigation, the community boards and the
hosting popup. Reconnaissance on 2026-09-15 (Issue #52 comments 5696110694, 5696172833) confirmed
every rule below against the two retained product captures.

Two properties matter more than coverage:

* **Positive selection.** A reference earns a product role only when the DOM says so. There is no
  rule over host names or path words — no ``skin``/``icon``/``btn`` folklore — because that is
  fail-open for naming nobody has seen yet, and because a host is never evidence of a role: the
  same third-party host serves this storefront's logo and its description images alike.
* **Fail closed.** Anything no rule recognises is :data:`ImageRole.UNKNOWN`, and the sampler never
  spends a request on ``UNKNOWN`` or ``UI_COMMON``. The ``UI_COMMON`` rules therefore never widen
  what is sampled; they only let the findings say *why* a reference was left out instead of
  lumping it in with the unrecognised ones.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from integrations.suppliers.collection import (
    SAMPLED_ROLES,
    ImageCandidate,
    ImageRole,
    ImageRoleRules,
)
from integrations.suppliers.kmretail.collect.revision import EXTRACTION_REVISION

# These rules are part of the package's one collection identity (ruling 5702780630).
ROLE_RULES_REVISION = EXTRACTION_REVISION

# The attributes a Cafe24 page uses to point at an image. ``ec-data-src`` is the platform's own
# lazy-load attribute and carries the whole description sequence, so it is read like ``src``.
IMAGE_ATTRIBUTES = ("src", "data-src", "ec-data-src", "data-original")
_SRCSET = "srcset"
_OG_IMAGE = "meta[og:image]"
# HTML's void elements: they have no end tag, so they never open a scope. Keeping one on the
# ancestry stack would make the next close tag remove the wrong element and leave a container such
# as the description block standing over everything that follows it (review 5222192371 §1).
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


@dataclass(frozen=True)
class Element:
    """One element of the document, reduced to what a role rule may look at.

    ``frame`` identifies this occurrence for the whole parse, so a role can name the scope that
    proved it and that scope can be checked again once the document ends. It is positional only:
    nothing is ever inferred from a host, a path or a document order.
    """

    frame: int
    tag: str
    element_id: str
    classes: frozenset[str]

    def marks(self, token: str) -> bool:
        token = token.lower()
        return token == self.element_id.lower() or any(
            token == name.lower() or name.lower().startswith(token) for name in self.classes
        )


def _within(chain: Sequence[Element], *tokens: str) -> bool:
    """Every token names an ancestor, in order, however deeply nested between them."""
    remaining = list(tokens)
    for element in chain:
        if remaining and element.marks(remaining[0]):
            remaining.pop(0)
    return not remaining


@dataclass(frozen=True)
class RoleRule:
    """One piece of site knowledge: what a container means for the images inside it.

    ``marker`` names the container itself. ``within`` names outer containers the marker must sit
    inside, for a marker that is only meaningful in one place.
    """

    rule_id: str
    role: ImageRole
    marker: str
    within: tuple[str, ...] = ()

    def matches(self, chain: Sequence[Element], element: Element) -> bool:
        return element.marks(self.marker) and (not self.within or _within(chain, *self.within))


# The innermost recognised container decides, so a rule is never fooled by an outer container that
# a malformed page left open: this storefront writes its description block with unclosed tags, and
# its community boards and the hosting popup sit inside it as a parser sees the document.
# Within one container, the first rule listed decides.
ROLE_RULES: tuple[RoleRule, ...] = (
    # The representative image of the product module.
    RoleRule("km.primary.key_image", ImageRole.PRIMARY, "keyImg", ("xans-product-image",)),
    # The additional-image list of the same module.
    RoleRule("km.thumbnail.additional", ImageRole.THUMBNAIL, "xans-product-addimage"),
    # The ordered description sequence, lazy-loaded or not.
    RoleRule("km.detail.prd_detail", ImageRole.DETAIL, "prdDetail"),
    # The product action area's photo slide: product imagery, but not the representative one.
    RoleRule(
        "km.aux.photoslide", ImageRole.PRODUCT_AUX, "xans-photoslide", ("xans-product-action",)
    ),
    # Everything the layout owns. These never widen the sample; they only let the findings name
    # the exclusion instead of lumping it in with the references no rule recognised.
    RoleRule("km.ui.header_logo", ImageRole.UI_COMMON, "xans-layout-logotop"),
    RoleRule("km.ui.promotion_banner", ImageRole.UI_COMMON, "promotionBanner"),
    RoleRule("km.ui.category_navigation", ImageRole.UI_COMMON, "xans-layout-category"),
    RoleRule("km.ui.community_board", ImageRole.UI_COMMON, "board"),
    RoleRule("km.ui.footer", ImageRole.UI_COMMON, "addr"),
    RoleRule("km.ui.hosting_popup", ImageRole.UI_COMMON, "progressPaybar"),
    RoleRule("km.ui.option_control", ImageRole.UI_COMMON, "xans-product-option"),
    RoleRule("km.ui.quantity_control", ImageRole.UI_COMMON, "quantity"),
    RoleRule("km.ui.info_area", ImageRole.UI_COMMON, "infoArea"),
)
# The page's own declaration of its representative image. It has no container to speak of, so it
# is read from the attribute; when it names the same asset as the key image, the sampler's own
# de-duplication keeps the two to one slot.
OG_IMAGE_RULE = RoleRule("km.primary.og_image", ImageRole.PRIMARY, "")
_UNRECOGNISED = "km.unknown"
# A product role whose proving scope the page never closed is withdrawn at EOF: the scope reached
# the end of the document still open, so it stands over everything after it and nothing it appears
# to contain is proven (review 5222613374 §2). The provisional rule is kept for the audit.
_UNCLOSED_SCOPE = "km.unclosed_scope"


class _References(HTMLParser):
    def __init__(self, product_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = product_url
        self._chain: list[Element] = []
        self._frames = 0
        self._closed: set[int] = set()
        self.found: list[ImageCandidate] = []
        # Per reference, the frame of the scope that proved its role, or None when the referencing
        # element proved it by itself and there is no scope that could be left open.
        self.proofs: list[int | None] = []

    def closed_frames(self) -> frozenset[int]:
        """The scopes the page closed with their own end tag.

        A scope removed while unwinding an ancestor's close is not among them: the page never said
        where that scope ended, so it never proved what it contains.
        """
        return frozenset(self._closed)

    def _element(self, tag: str, values: dict[str, str]) -> Element:
        self._frames += 1
        return Element(
            frame=self._frames,
            tag=tag,
            element_id=values.get("id", "").strip(),
            classes=frozenset(name for name in values.get("class", "").split() if name),
        )

    def _role(self, element: Element) -> tuple[ImageRole, str, int | None]:
        """The innermost recognised container's role, and the frame of that container.

        The referencing element is judged with its own ancestry, so an element that is itself a
        recognised container is judged by it. Ancestry is only ever what the document proved open:
        recovery from malformed markup drops elements and never invents one, so a broken page can
        lose a product role but can never gain one (review 5222192371 §3). The answer is
        provisional until the document ends: see :func:`classify_images`.
        """
        ancestry = (*self._chain, element)
        for depth in range(len(ancestry) - 1, -1, -1):
            container = ancestry[depth]
            for rule in ROLE_RULES:
                if rule.matches(ancestry[: depth + 1], container):
                    # The last link is the referencing element itself: it opens no scope, so its
                    # proof cannot be left hanging by a missing close.
                    proof = None if depth == len(ancestry) - 1 else container.frame
                    return rule.role, rule.rule_id, proof
        return ImageRole.UNKNOWN, _UNRECOGNISED, None

    def _reference(self, raw: str, element: Element, attribute: str) -> None:
        raw = raw.strip()
        if not raw or raw.lower().startswith("data:"):
            return
        url = urljoin(self._base, raw)
        if urlsplit(url).scheme not in ("http", "https"):
            return
        if attribute == _OG_IMAGE:
            # The page's own declaration of its representative image. It is proved by the element
            # that carries it, not by a container, so no unclosed scope can withdraw it.
            role, rule_id, frame = OG_IMAGE_RULE.role, OG_IMAGE_RULE.rule_id, None
        else:
            role, rule_id, frame = self._role(element)
        self.found.append(ImageCandidate(url=url, role=role, order=len(self.found), rule=rule_id))
        self.proofs.append(frame)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        element = self._element(tag, values)
        if tag not in VOID_ELEMENTS:
            self._chain.append(element)
        if tag == "img":
            for attribute in IMAGE_ATTRIBUTES:
                if values.get(attribute):
                    self._reference(values[attribute], element, attribute)
            for entry in values.get(_SRCSET, "").split(","):
                if entry.strip():
                    self._reference(entry.split()[0], element, _SRCSET)
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key == "og:image" and values.get("content"):
                self._reference(values["content"], element, _OG_IMAGE)

    def handle_endtag(self, tag: str) -> None:
        """Close the nearest element of that name, and with it anything the page left unclosed.

        A close never removes an element of another name: one missing or stray end tag would
        otherwise shift the whole stack for the rest of the document. A close with nothing open to
        match is ignored rather than allowed to disturb ancestry (review 5222192371 §2).
        """
        if tag in VOID_ELEMENTS:
            return
        for depth in range(len(self._chain) - 1, -1, -1):
            if self._chain[depth].tag == tag:
                # Only this element was closed by the page. Whatever sat above it is unwound with
                # it but was never closed, so it stays unproven.
                self._closed.add(self._chain[depth].frame)
                del self._chain[depth:]
                return


def classify_images(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
    """Every image reference of a KM통상 product document, in the document's own order, with the
    role its DOM supports. Nothing is fetched and nothing but structure is kept.

    Roles are provisional while the document is read, because a scope only proves what it contains
    once the page closes it with its own end tag. A scope the page never closed proves nothing: it
    stands over everything written after it, so the page never said where it stopped. Being unwound
    by an ancestor's close is not the page saying it — that is the same missing close seen from
    outside. Every sample-eligible role proved by such a scope is withdrawn to ``UNKNOWN`` before
    anything is sampled: the genuine references inside it as well as any later one that inherited
    it (review 5222613374 §2).

    A role the referencing element proved by itself, such as the page's own ``og:image``
    declaration, opens no scope and is never withdrawn this way (§4).

    This is deliberately conservative. A malformed page can yield fewer product images, or none; it
    can never yield more, and it can never widen what a phase-B run is allowed to request.
    """
    parser = _References(product_url)
    parser.feed(body)
    parser.close()
    closed = parser.closed_frames()
    return tuple(
        candidate
        if not (candidate.role in SAMPLED_ROLES and frame is not None and frame not in closed)
        else replace(candidate, role=ImageRole.UNKNOWN, rule=f"{_UNCLOSED_SCOPE}:{candidate.rule}")
        for candidate, frame in zip(parser.found, parser.proofs, strict=True)
    )


IMAGE_ROLES = ImageRoleRules(identity=ROLE_RULES_REVISION, classify=classify_images)
