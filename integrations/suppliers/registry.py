"""Supplier definitions known to this build. KM통상 is the first; each is site knowledge only."""

from integrations.suppliers import kmretail
from integrations.suppliers.base import SupplierDefinition
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.kmretail.collection import COLLECTION as KMRETAIL_COLLECTION

SUPPLIERS: tuple[SupplierDefinition, ...] = (kmretail.DEFINITION,)
# The suppliers this build can collect from. A supplier appears here only once its profile,
# identity rule, field parser and image roles are all accepted evidence (ADR-0010 §3).
COLLECTIONS: tuple[SupplierCollection, ...] = (KMRETAIL_COLLECTION,)
