"""1688 (https://detail.1688.com) — site knowledge only (ADR-0040).

China's wholesale marketplace. A product page states its prices in CNY (ADR-0038 §1), per SKU or
in quantity tiers, and carries every SKU's price, stock and freight in one inline script whose data
argument the capture extension projects onto a reviewed allowlist (ADR-0040 §3). The supplier key is
``ali1688``; its label is 1688.

**Captured pages only** (ADR-0040 §1, OC-01, OC-02). The user opens a product page in their own
signed-in browser and presses 수집 in the capture extension; exactly that page is read. ICBM holds
no credential for this site, never signs in and never reads its pages itself: the package declares
a collection and a capture policy, and no CONNECT definition. Its collection profile declares no
direct-URL read and no list queue. The server fetches only images: the gallery's, and the
description's image references the extension read within the capture (ADR-0040 §3.2, OC-09).
It never reads a page or the description document of this site.

**RECON** (ADR-0030 §7, ADR-0040 §6). The supplier collects, but its products cannot be prepared
for a marketplace until a reviewed PR cites its acceptance record and makes it ``ACTIVE``.

This package describes the supplier; it cannot make a request. A repository rule keeps network,
browser and logging imports out of it.
"""

from integrations.suppliers.ali1688.collect import IMAGE_ROLES
from integrations.suppliers.base import RequestPolicy, SupplierProfile
from integrations.suppliers.site_config import SiteStatus

__all__ = ["IMAGE_ROLES", "PROFILE", "STATUS", "STOREFRONT_HOST"]

STOREFRONT_HOST = "detail.1688.com"
# ADR-0030 §7: collected, not yet preparable for a marketplace.
STATUS = SiteStatus.RECON

PROFILE = SupplierProfile(
    supplier_key="ali1688",
    display_name="1688",
    base_url=f"https://{STOREFRONT_HOST}",
    # OC-02: ICBM never signs in. The member's session is the user's own browser's, and a page it
    # captures is whatever that session showed.
    auth_required=False,
    egress_hosts=frozenset({STOREFRONT_HOST}),
    request_policy=RequestPolicy(
        max_concurrency=1,
        minimum_request_interval_s=2.0,
        auth_retry_limit=1,
        request_timeout_s=20.0,
    ),
    # ADR-0038 §6 (O3): a Chinese wholesale marketplace — the origin country of its 구매대행
    # products.
    country="CN",
)
