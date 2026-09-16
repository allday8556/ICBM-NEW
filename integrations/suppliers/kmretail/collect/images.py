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
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from integrations.suppliers.collection import ImageCandidate, ImageRole, ImageRoleRules

# The identity of these rules. A semantic change to what a rule means advances it, and a
# repository rule pins it to the package's EXTRACTOR_REVISION (comment 5696242775 §4).
ROLE_RULES_REVISION = "kmretail-images-1"

# The attributes a Cafe24 page uses to point at an image. ``ec-data-src`` is the platform's own
# lazy-load attribute and carries the whole description sequence, so it is read like ``src``.
IMAGE_ATTRIBUTES = ("src", "data-src", "ec-data-src", "data-original")
_SRCSET = "srcset"
_OG_IMAGE = "meta[og:image]"


@dataclass(frozen=True)
class Element:
    """One open element of the document, reduced to what a role rule may look at."""

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


class _References(HTMLParser):
    def __init__(self, product_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = product_url
        self._chain: list[Element] = []
        self.found: list[ImageCandidate] = []

    def _element(self, tag: str, values: dict[str, str]) -> Element:
        return Element(
            tag=tag,
            element_id=values.get("id", "").strip(),
            classes=frozenset(name for name in values.get("class", "").split() if name),
        )

    def _role(self) -> tuple[ImageRole, str]:
        """The innermost recognised container's role; UNKNOWN when no rule recognises any of them.

        The referencing element is the last link of the chain, so an element that is itself a
        recognised container is judged by it.
        """
        for depth in range(len(self._chain) - 1, -1, -1):
            container = self._chain[depth]
            for rule in ROLE_RULES:
                if rule.matches(self._chain[: depth + 1], container):
                    return rule.role, rule.rule_id
        return ImageRole.UNKNOWN, _UNRECOGNISED

    def _reference(self, raw: str, element: Element, attribute: str) -> None:
        raw = raw.strip()
        if not raw or raw.lower().startswith("data:"):
            return
        url = urljoin(self._base, raw)
        if urlsplit(url).scheme not in ("http", "https"):
            return
        if attribute == _OG_IMAGE:
            role, rule_id = OG_IMAGE_RULE.role, OG_IMAGE_RULE.rule_id
        else:
            role, rule_id = self._role()
        self.found.append(ImageCandidate(url=url, role=role, order=len(self.found), rule=rule_id))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        element = self._element(tag, values)
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
        if self._chain:
            self._chain.pop()


def classify_images(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
    """Every image reference of a KM통상 product document, in the document's own order, with the
    role its DOM supports. Nothing is fetched and nothing but structure is kept."""
    parser = _References(product_url)
    parser.feed(body)
    parser.close()
    return tuple(parser.found)


IMAGE_ROLES = ImageRoleRules(identity=ROLE_RULES_REVISION, classify=classify_images)
