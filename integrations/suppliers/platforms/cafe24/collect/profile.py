"""The Cafe24 template's profile values: its product-path forms, policy documents and default
limits (ADR-0030 §3, §4).

They are part of the template's extraction semantics (ADR-0010 §12: the profile's hosts, safe
query keys and limits), so they live in the hashed ``collect`` package and advance its revision
when they change.
"""

from collections.abc import Mapping
from types import MappingProxyType

from integrations.suppliers.platforms import PathForm

ROBOTS_PATH = "/robots.txt"
TERMS_PATH = "/member/agreement.html"

# The product path form Cafe24's search-friendly URLs use, with the listing segments an operator's
# URL may carry after it. A site names a form; it never writes a pattern (ADR-0030 §3).
PATH_FORMS: Mapping[str, PathForm] = MappingProxyType(
    {"seo": PathForm(r"/product/[^/]+/\d+(?:/category/\d+)?(?:/display/\d+)?/?")}
)
# KM통상's frozen limits are the template defaults, with the owner's per-image caps by role for
# every supplier (ADR-0037 §1, Issue #219 6103784916): 5 MB for the representative and additional
# images, 30 MB for a description image, and 120 MB per product run. A site may lower them; raising
# one needs the owner's recorded decision (ADR-0030 §3, PT-12).
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
