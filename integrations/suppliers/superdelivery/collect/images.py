"""Which image references of a Super Delivery capture are product evidence (ADR-0040 §1).

The product block marks each image's purpose in its own DOM, and the capture policy has already
left out the smartphone layout's duplicated slides and thumbnail strip:

==========================  ======================================================================
``#largeImageParentBox``    the main image the product block shows, its ``product_image_preview``
``#thumbImageBox``          the product's image list, in the page's order (the first is the main
                            image again; the sampler's de-duplication keeps the two to one slot)
``.product-comment``        the description (商品説明), when the company writes images into it
``#promark_movie``          the site's own "動画あり" mark, layout furniture
``.loadingImage``           the cart button's loading spinner, layout furniture
==========================  ======================================================================

**Positive selection.** A reference earns a product role only when the DOM says so: nothing is
read from a host name, a path word or document order. Anything no rule recognises is
:data:`ImageRole.UNKNOWN`, and the sampler never spends a request on it.

**Fail closed.** A product role whose proving container the capture never closed is withdrawn to
``UNKNOWN`` before anything is sampled (the KM통상 rule, review 5222613374 §2): a container that
reaches the end of the document still open never proved what it contains.
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
from integrations.suppliers.superdelivery.collect.revision import EXTRACTION_REVISION

ROLE_RULES_REVISION = EXTRACTION_REVISION
# The capture policy keeps ``src`` alone on an image: the page's lazy-load and srcset attributes are
# dropped in the browser, so ``src`` is the only reference a capture can carry.
IMAGE_ATTRIBUTES = ("src",)
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
    """One element of the capture, reduced to what a role rule may look at."""

    frame: int
    tag: str
    element_id: str
    classes: frozenset[str]

    def marks(self, token: str) -> bool:
        """``#name`` is exactly the id, ``.name`` exactly one class; nothing matches a prefix."""
        if token.startswith("#"):
            return token[1:] == self.element_id
        return token.startswith(".") and token[1:] in self.classes


@dataclass(frozen=True)
class RoleRule:
    """What a container means for the images inside it. ``reference`` names a mark the
    referencing element itself must carry, for a container that holds more than its own image."""

    rule_id: str
    role: ImageRole
    marker: str
    reference: str = ""

    def matches(self, container: Element, reference: Element) -> bool:
        return container.marks(self.marker) and (
            not self.reference or reference.marks(self.reference)
        )


# The innermost recognised container decides; within one container, the first rule listed decides.
ROLE_RULES: tuple[RoleRule, ...] = (
    RoleRule(
        "sd.primary.large_image",
        ImageRole.PRIMARY,
        "#largeImageParentBox",
        reference=".product_image_preview",
    ),
    RoleRule("sd.ui.large_image_furniture", ImageRole.UI_COMMON, "#largeImageParentBox"),
    RoleRule("sd.thumbnail.image_list", ImageRole.THUMBNAIL, "#thumbImageBox"),
    RoleRule("sd.detail.product_comment", ImageRole.DETAIL, ".product-comment"),
    RoleRule("sd.ui.movie_mark", ImageRole.UI_COMMON, "#promark_movie"),
    RoleRule("sd.ui.cart_loading", ImageRole.UI_COMMON, ".loadingImage"),
)
_UNRECOGNISED = "sd.unknown"
_UNCLOSED_SCOPE = "sd.unclosed_scope"


class _References(HTMLParser):
    def __init__(self, product_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = product_url
        self._chain: list[Element] = []
        self._frames = 0
        self._closed: set[int] = set()
        self.found: list[ImageCandidate] = []
        self.proofs: list[int | None] = []

    def closed_frames(self) -> frozenset[int]:
        """The containers the capture closed with their own end tag."""
        return frozenset(self._closed)

    def _role(self, element: Element) -> tuple[ImageRole, str, int | None]:
        ancestry: Sequence[Element] = (*self._chain, element)
        for depth in range(len(ancestry) - 1, -1, -1):
            container = ancestry[depth]
            for rule in ROLE_RULES:
                if rule.matches(container, element):
                    proof = None if depth == len(ancestry) - 1 else container.frame
                    return rule.role, rule.rule_id, proof
        return ImageRole.UNKNOWN, _UNRECOGNISED, None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        self._frames += 1
        element = Element(
            frame=self._frames,
            tag=tag,
            element_id=values.get("id", "").strip(),
            classes=frozenset(name for name in values.get("class", "").split() if name),
        )
        if tag not in VOID_ELEMENTS:
            self._chain.append(element)
        if tag != "img":
            return
        for attribute in IMAGE_ATTRIBUTES:
            written = values.get(attribute, "")
            raw = written.strip()
            if not raw or raw.lower().startswith("data:"):
                continue
            url = urljoin(self._base, raw)
            if urlsplit(url).scheme not in ("http", "https"):
                continue
            role, rule_id, frame = self._role(element)
            self.found.append(
                ImageCandidate(
                    url=url, role=role, order=len(self.found), rule=rule_id, source=written
                )
            )
            self.proofs.append(frame)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        for depth in range(len(self._chain) - 1, -1, -1):
            if self._chain[depth].tag == tag:
                self._closed.add(self._chain[depth].frame)
                del self._chain[depth:]
                return


def classify_images(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
    """Every image reference of a Super Delivery capture, in the document's own order, with the
    role its DOM supports. Nothing is fetched and nothing but structure is kept."""
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
