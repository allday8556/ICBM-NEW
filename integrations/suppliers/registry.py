"""Supplier definitions known to this build.

KM통상 is its own package. Every other supplier is a site configuration bound to its platform
template (ADR-0030): ``integrations/suppliers/sites/<key>.json``. A site file that fails validation
is reported in ``SITE_PROBLEMS`` and its site is not offered; nothing else stops.
"""

from collections.abc import Mapping
from types import MappingProxyType

from integrations.suppliers import kmretail
from integrations.suppliers.base import SupplierDefinition
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.kmretail.collection import COLLECTION as KMRETAIL_COLLECTION
from integrations.suppliers.sites import BoundSite, bind_sites

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
    *(site.collection for site in _SITES),
)
