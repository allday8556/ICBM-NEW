"""Which image references of a captured 1688 page are product evidence (ADR-0040 §1, §4).

The capture keeps the product gallery, the title block and the attribute table (the reviewed
capture policy). The gallery is one list, ``ul.od-gallery-list``, whose product images are its
``img.preview-img`` elements in the page's own order: the first is the representative image, the
rest are additional images, the option (SKU) images among them. Everything else the capture holds
is no product image.

Positive selection only: a reference earns a role because the page's own structure says so, never
because of its host or its path. Anything no rule recognises is ``UNKNOWN`` and never fetched.

The description sequence is not on this page: it is a separate document the page names. Within
the user's capture the extension reads it and sends only its image references on the declared
image hosts (ADR-0040 §3.2, OC-09); the server re-checks them and hands them here.
:func:`description_candidates` makes each one a ``DETAIL`` candidate, in the document's own order.
The server never reads the document.
"""

from urllib.parse import urljoin, urlsplit

from integrations.suppliers.ali1688.collect.dom import read
from integrations.suppliers.ali1688.collect.revision import EXTRACTION_REVISION
from integrations.suppliers.collection import (
    DocumentView,
    ImageCandidate,
    ImageRole,
    ImageRoleRules,
)

# These rules are part of the package's one collection identity.
ROLE_RULES_REVISION = EXTRACTION_REVISION
# The image host reconnaissance named for 1688's gallery (ADR-0040 §1). Nothing else is fetched.
IMAGE_HOSTS = frozenset({"cbu01.alicdn.com"})

GALLERY_LIST = "od-gallery-list"
GALLERY_IMAGE = "preview-img"
PRIMARY_RULE = "ali1688.primary.gallery"
THUMBNAIL_RULE = "ali1688.thumbnail.gallery"
DETAIL_RULE = "ali1688.detail.description"
UNKNOWN_RULE = "ali1688.unknown"
# The rules a supplier publishes for the image auto-selection (rule name -> role).
ROLE_TABLE: tuple[tuple[str, ImageRole], ...] = (
    (PRIMARY_RULE, ImageRole.PRIMARY),
    (THUMBNAIL_RULE, ImageRole.THUMBNAIL),
    (DETAIL_RULE, ImageRole.DETAIL),
)


def classify_images(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
    """Every image reference of a captured 1688 page, in the page's own order, with its role."""
    found: list[ImageCandidate] = []
    primary_taken = False
    for node in read(body):
        if node.tag != "img":
            continue
        written = node.attributes.get("src", "")
        raw = written.strip()
        if not raw or raw.lower().startswith("data:"):
            continue
        url = urljoin(product_url, raw)
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        in_gallery = node.has_class(GALLERY_IMAGE) and node.inside(class_token=GALLERY_LIST)
        if in_gallery and not primary_taken:
            role, rule = ImageRole.PRIMARY, PRIMARY_RULE
            primary_taken = True
        elif in_gallery:
            role, rule = ImageRole.THUMBNAIL, THUMBNAIL_RULE
        else:
            role, rule = ImageRole.UNKNOWN, UNKNOWN_RULE
        found.append(
            ImageCandidate(url=url, role=role, order=len(found), rule=rule, source=written)
        )
    return tuple(found)


def description_candidates(document: DocumentView, product_url: str) -> tuple[ImageCandidate, ...]:
    """The description's image references the capture carries, as ``DETAIL`` candidates.

    Only a reference on 1688's own image host is product evidence; any other is ``UNKNOWN`` and
    never fetched (the server already refused a member holding one). Nothing here reads anything.
    """
    captured = document.captured_images
    if captured is None or captured.urls is None:
        return ()
    found: list[ImageCandidate] = []
    for url in captured.urls:
        on_host = urlsplit(url).hostname in IMAGE_HOSTS
        found.append(
            ImageCandidate(
                url=url,
                role=ImageRole.DETAIL if on_host else ImageRole.UNKNOWN,
                order=len(found),
                rule=DETAIL_RULE if on_host else UNKNOWN_RULE,
                source=url,
            )
        )
    return tuple(found)


IMAGE_ROLES = ImageRoleRules(identity=ROLE_RULES_REVISION, classify=classify_images)
