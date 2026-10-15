"""Supplier definitions known to this build.

KM통상, 슈퍼딜리버리 and 1688 are their own packages. Every other supplier is a site
configuration bound to its platform template (ADR-0030):
``integrations/suppliers/sites/<key>.json``. A site file that fails validation is reported
in ``SITE_PROBLEMS`` and its site is not offered; nothing else stops.

슈퍼딜리버리 and 1688 have a collection and no CONNECT definition (ADR-0040 §1, OC-02):
ICBM holds no credential for either and never signs in, so they are in ``COLLECTIONS``
and not in ``SUPPLIERS``.
"""

from collections.abc import Mapping
from types import MappingProxyType

from integrations.suppliers import ali1688, kmretail, superdelivery
from integrations.suppliers.ali1688.collection import COLLECTION as ALI1688_COLLECTION
from integrations.suppliers.base import SupplierDefinition
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.kmretail.collection import COLLECTION as KMRETAIL_COLLECTION
from integrations.suppliers.site_config import SiteStatus
from integrations.suppliers.sites import BoundSite, bind_sites
from integrations.suppliers.superdelivery.collection import COLLECTION as SUPERDELIVERY_COLLECTION

_SITES, SITE_PROBLEMS = bind_sites()
SITES: Mapping[str, BoundSite] = MappingProxyType({s.config.supplier_key: s for s in _SITES})

SUPPLIERS: tuple[SupplierDefinition, ...] = (
    kmretail.DEFINITION,
    *(site.definition for site in _SITES),
)
# The suppliers this build can collect from. A supplier appears here only once its profile,
# identity rule, field parser and image roles are all accepted evidence (ADR-0010 §3), or once its
# site configuration is bound to a reviewed template (ADR-0030 §1).
COLLECTIONS: tuple[SupplierCollection, ...] = (
    KMRETAIL_COLLECTION,
    SUPERDELIVERY_COLLECTION,
    ALI1688_COLLECTION,
    *(site.collection for site in _SITES),
)
# ADR-0030 §7: a packaged supplier that states its own status. KM통상 predates the status and
# states none.
_PACKAGED_ACTIVE: Mapping[str, bool] = MappingProxyType(
    {
        superdelivery.PROFILE.supplier_key: superdelivery.STATUS is SiteStatus.ACTIVE,
        ali1688.PROFILE.supplier_key: ali1688.STATUS is SiteStatus.ACTIVE,
    }
)


def repository_active(supplier_key: str) -> bool | None:
    """PT-06: True for an ACTIVE repository supplier, False for one in RECON, and None for a key
    that states no status (KM통상, or no repository supplier at all)."""
    if supplier_key in SITES:
        return SITES[supplier_key].config.active
    return _PACKAGED_ACTIVE.get(supplier_key)
