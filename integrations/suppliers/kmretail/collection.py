"""KM통상's collection binding: where its product pages live, and what its documents mean.

This is site knowledge and nothing else (ADR-0010 §3). Every value here is what the accepted
reconnaissance of `m3-recon-02` observed — no host, path or query key was added because it seemed
likely, and none is a wildcard:

============================  =====================================================================
product path                  ``/product/<name>/<number>/`` and the listing form the operator's own
                              URL used, ``/product/<name>/<number>/category/<n>/display/<n>/``
policy documents              ``/robots.txt`` (read, HTTP 200) and ``/member/agreement.html``
                              (the terms link the product page itself carries, HTTP 200)
image hosts                   ``kmretail.co.kr`` and ``onewbio.diskn.com`` — the two hosts phase B
                              was approved for. ``img.cafe24.com`` and ``img.echosting.cafe24.com``
                              were observed serving the platform's own layout assets and are **not**
                              approved: a UI asset is never product evidence
safe query keys               none. The product URL carried no query at all, so every query key
                              fails closed until evidence justifies one
============================  =====================================================================

The limits are bounded by what was actually observed, with headroom and nothing more: the page
references 32 images, of which the role rules recognise 13 as product evidence, and the largest
image phase B fetched was 1,437,349 bytes.

This module performs nothing. It holds no client, opens no connection, computes no checksum and
writes no row; the run, the fetch, the asset store, the revision and the job belong to generic
COLLECT core.
"""

from collections.abc import Mapping

from app.collect.facts import FieldFact
from integrations.suppliers.collection import (
    CollectionLimits,
    CollectionProfile,
    DocumentView,
    SourceIdentity,
    SourceIdentityResult,
    SupplierCollection,
    UnresolvedIdentity,
)
from integrations.suppliers.kmretail import PROFILE
from integrations.suppliers.kmretail.collect import IMAGE_ROLES, parse_fields, resolve
from integrations.suppliers.kmretail.collect.identity import SourceIdentity as ParsedIdentity
from integrations.suppliers.kmretail.collect.identity import _path_number

# The product path form the storefront writes, with the listing segments the operator's own URL
# may carry after it. Anything else is not a product page and is never read.
PRODUCT_PATH = r"/product/[^/]+/\d+(?:/category/\d+)?(?:/display/\d+)?/?"
ROBOTS_PATH = "/robots.txt"
TERMS_PATH = "/member/agreement.html"
STOREFRONT_HOST = "kmretail.co.kr"
# The image hosts phase B was approved for, and no others.
IMAGE_HOSTS = frozenset({STOREFRONT_HOST, "onewbio.diskn.com"})
# Observed: largest sampled image 1,437,349 bytes; 32 references, 13 of them product evidence.
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_REFS = 30
MAX_IMAGE_REQUESTS_PER_RUN = 30
MAX_NEW_IMAGE_BYTES_PER_RUN = 24 * 1024 * 1024
# ADR-0010 §4: at least 60 s between real reads of the same product.
SAME_PRODUCT_INTERVAL_S = 60.0


def _identity(document: DocumentView, source_url: str) -> SourceIdentityResult:
    """The parser's own answer, said in the port's words.

    The rule is the parser's and is not restated here: this only carries the answer across the
    boundary, so generic COLLECT core never imports a supplier's module to understand it.
    """
    outcome = resolve(document, source_url)
    if isinstance(outcome, ParsedIdentity):
        return SourceIdentity(outcome.source_product_id, outcome.agreed)
    return UnresolvedIdentity(outcome.reason, seen=outcome.seen, missing=outcome.missing)


def _fields(document: DocumentView) -> Mapping[str, FieldFact]:
    return parse_fields(document)


def _url_product_hint(url: str) -> str | None:
    """Which product this URL points at, read from the path alone.

    ``/product/<name>/355/`` and ``/product/<name>/355/category/23/display/1/`` point at one
    product, and so does the same URL after a rename. Pacing must treat them as one product
    before any of them has been read, so it asks the identity rule's own path reader rather than
    a second copy of it — one rule, in one place, even for a question this small.
    """
    return _path_number(url)


def build_profile(*, image_hosts: frozenset[str] = IMAGE_HOSTS) -> CollectionProfile:
    return CollectionProfile(
        supplier=PROFILE,
        product_path=PRODUCT_PATH,
        policy_paths=frozenset({ROBOTS_PATH, TERMS_PATH}),
        image_hosts=image_hosts,
        # No query key is safe until evidence justifies one, so a URL with a query is refused.
        safe_query_keys={},
        limits=CollectionLimits(
            max_image_refs=MAX_IMAGE_REFS,
            max_image_bytes=MAX_IMAGE_BYTES,
            max_image_requests_per_run=MAX_IMAGE_REQUESTS_PER_RUN,
            max_new_image_bytes_per_run=MAX_NEW_IMAGE_BYTES_PER_RUN,
            same_product_interval_s=SAME_PRODUCT_INTERVAL_S,
        ),
    )


COLLECTION = SupplierCollection(
    profile=build_profile(),
    roles=IMAGE_ROLES,
    identity=_identity,
    fields=_fields,
    url_product_hint=_url_product_hint,
)
