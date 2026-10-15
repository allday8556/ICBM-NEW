"""슈퍼딜리버리 (Super Delivery, https://www.superdelivery.com) — site knowledge only (ADR-0040).

A Japanese wholesale marketplace: many supplier companies (出展企業) sell on one storefront, and a
company shows its wholesale price only to a member it has approved (ADR-0038 §9). The supplier key
is ``superdelivery``; its currency is JPY (ADR-0038 §1).

**Captured pages only** (ADR-0040 §1, OC-01, OC-02). The user opens a product page in their own
signed-in browser and presses 수집 in the capture extension; exactly that page is read. ICBM holds
no credential for this site, never signs in and never reads its pages itself: the package declares
a collection and a capture policy, and no CONNECT definition — there is no login form to describe
and nothing to prove. Its collection profile declares no direct-URL read and no list queue.

**RECON** (ADR-0030 §7, ADR-0040 §6). The supplier collects, but its products cannot be prepared
for a marketplace until a reviewed PR cites its acceptance record and makes it ``ACTIVE``.

This package describes the supplier; it cannot make a request. A repository rule keeps network,
browser and logging imports out of it.
"""

from integrations.suppliers.base import RequestPolicy, SupplierProfile
from integrations.suppliers.site_config import SiteStatus
from integrations.suppliers.superdelivery.collect import IMAGE_ROLES

__all__ = ["IMAGE_ROLES", "PROFILE", "STATUS", "STOREFRONT_HOST"]

STOREFRONT_HOST = "www.superdelivery.com"
# ADR-0030 §7: collected, not yet preparable for a marketplace.
STATUS = SiteStatus.RECON

PROFILE = SupplierProfile(
    supplier_key="superdelivery",
    display_name="슈퍼딜리버리",
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
    # ADR-0038 §6 (O3): a Japanese wholesale marketplace — the origin country of its 구매대행
    # products.
    country="JP",
)
