"""1688's collection binding: where its product pages live, and what a captured page means.

This is site knowledge and nothing else (ADR-0010 §3, ADR-0040 §1). Every value is what the
reconnaissance of 2026-10-14 observed (ADR-0038 §9) — no host, path or query key was added because
it seemed likely, and none is a wildcard:

============================  =====================================================================
product path                  ``/offer/<number>.html``, the form of every product page
image host                    ``cbu01.alicdn.com``: the gallery's images
policy documents              none: no page of this site is ever read by the server
safe query keys               none: a product URL carries no query, so every query key fails closed
direct-URL read               none (OC-01): the server never reads a product page of this site;
                              only a page the user captured is recorded
list queue                    none (OC-01, ADR-0019 AC-28): a queue refuses before any read
============================  =====================================================================

Images are fetched by the server's policed gateway from the image hosts alone (ADR-0019 §7), under
the owner's caps by role (ADR-0037 §1): 5 MB for a representative or additional image, 30 MB for a
description image, 120 MB per product run. A captured page references at most a few dozen gallery
images (12 option images and 5 main images on the largest observed). The description is a separate
document the page names; it is not captured, so 1688 has no description images yet.

This module performs nothing. It holds no client, opens no connection, computes no checksum and
writes no row; the run, the fetch, the asset store, the revision and the job belong to generic
COLLECT core.
"""

from collections.abc import Mapping

from app.stages.collect.facts import FieldFact
from integrations.suppliers.ali1688 import PROFILE, STOREFRONT_HOST
from integrations.suppliers.ali1688.collect import (
    IMAGE_HOSTS,
    IMAGE_ROLES,
    parse_fields,
    path_number,
    resolve,
)
from integrations.suppliers.ali1688.collect.identity import SourceIdentity as ParsedIdentity
from integrations.suppliers.collection import (
    CollectionLimits,
    CollectionProfile,
    DocumentView,
    SourceIdentity,
    SourceIdentityResult,
    SupplierCollection,
    UnresolvedIdentity,
)

PRODUCT_PATH = r"/offer/\d{1,20}\.html"
# ADR-0037 §1: the owner's caps by role, in decimal MB, and the per-product run total.
MAX_IMAGE_BYTES = 5_000_000
MAX_DETAIL_IMAGE_BYTES = 30_000_000
MAX_IMAGE_REFS = 60
MAX_IMAGE_REQUESTS_PER_RUN = 60
MAX_NEW_IMAGE_BYTES_PER_RUN = 120_000_000
# ADR-0010 §4: at least 60 s between real reads of the same product.
SAME_PRODUCT_INTERVAL_S = 60.0

__all__ = [
    "COLLECTION",
    "IMAGE_HOSTS",
    "PRODUCT_PATH",
    "STOREFRONT_HOST",
    "build_profile",
]


def _identity(document: DocumentView, source_url: str) -> SourceIdentityResult:
    """The parser's own answer, said in the port's words."""
    outcome = resolve(document, source_url)
    if isinstance(outcome, ParsedIdentity):
        return SourceIdentity(outcome.source_product_id, outcome.agreed)
    return UnresolvedIdentity(outcome.reason, seen=outcome.seen, missing=outcome.missing)


def _fields(document: DocumentView) -> Mapping[str, FieldFact]:
    return parse_fields(document)


def build_profile(*, image_hosts: frozenset[str] = IMAGE_HOSTS) -> CollectionProfile:
    return CollectionProfile(
        supplier=PROFILE,
        product_path=PRODUCT_PATH,
        policy_paths=frozenset(),
        image_hosts=image_hosts,
        safe_query_keys={},
        limits=CollectionLimits(
            max_image_refs=MAX_IMAGE_REFS,
            max_image_bytes=MAX_IMAGE_BYTES,
            max_detail_image_bytes=MAX_DETAIL_IMAGE_BYTES,
            max_image_requests_per_run=MAX_IMAGE_REQUESTS_PER_RUN,
            max_new_image_bytes_per_run=MAX_NEW_IMAGE_BYTES_PER_RUN,
            same_product_interval_s=SAME_PRODUCT_INTERVAL_S,
        ),
        # OC-01: no page of this site is read by the server, and there is no list queue.
        direct_url_reads=False,
        queue_limits=None,
    )


COLLECTION = SupplierCollection(
    profile=build_profile(),
    roles=IMAGE_ROLES,
    identity=_identity,
    fields=_fields,
    url_product_hint=path_number,
)
