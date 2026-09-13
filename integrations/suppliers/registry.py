"""Supplier definitions known to this build. KM통상 is the first; each is site knowledge only."""

from integrations.suppliers import kmretail
from integrations.suppliers.base import SupplierDefinition

SUPPLIERS: tuple[SupplierDefinition, ...] = (kmretail.DEFINITION,)
