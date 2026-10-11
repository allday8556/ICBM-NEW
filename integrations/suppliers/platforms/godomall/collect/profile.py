"""The Godomall template's profile values: its product-path form, policy documents and default
limits (ADR-0030 §3, §4). They are extraction semantics (ADR-0010 §12), so they live in the hashed
``collect`` package.
"""

from collections.abc import Mapping
from types import MappingProxyType

from integrations.suppliers.platforms import PathForm

ROBOTS_PATH = "/robots.txt"
TERMS_PATH = "/service/agreement.php"

# Godomall addresses a product by one query key on one script path; every other query key (the
# listing's tracking ``mtn``, for one) is dropped by URL sanitization before any read.
PATH_FORMS: Mapping[str, PathForm] = MappingProxyType(
    {"goods_view": PathForm(r"/goods/goods_view\.php", frozenset({"goodsNo"}))}
)
# The same defaults as the Cafe24 template, with the owner's per-image caps by role for every
# supplier (ADR-0037 §1, Issue #219 6103784916): 5 MB for the representative and additional images,
# 30 MB for a description image, and 120 MB per product run. 건강산's robots.txt asks the bots it
# names for 10 s between requests, which the queue interval already keeps.
DEFAULT_LIMITS: Mapping[str, float] = MappingProxyType(
    {
        "max_image_refs": 30,
        "max_image_bytes": 5_000_000,
        "max_detail_image_bytes": 30_000_000,
        "max_image_requests_per_run": 30,
        "max_new_image_bytes_per_run": 120_000_000,
        "same_product_interval_s": 60.0,
        "max_discovered_links": 1000,
        "max_queue_products": 500,
        "min_queue_interval_s": 10.0,
        "issue_ttl_s": 120.0,
    }
)
