"""The supplier gate (ADR-0017 §3): a profile exists only for a registered supplier.

A supplier is registered when this build holds both its CONNECT ``SupplierDefinition`` (ADR-0007)
and its COLLECT access envelope, the ``SupplierCollection`` whose ``CollectionProfile`` fixes its
hosts, paths and limits (ADR-0010 §3). Both are reviewed repository declarations; a profile can
never introduce a supplier, a host or a limit of its own.
"""

from collections.abc import Callable, Iterable

from integrations.suppliers.base import SupplierDefinition
from integrations.suppliers.collection import SupplierCollection
from integrations.suppliers.registry import COLLECTIONS, SUPPLIERS

SupplierGate = Callable[[str], bool]


def registered_suppliers(
    definitions: Iterable[SupplierDefinition] = SUPPLIERS,
    collections: Iterable[SupplierCollection] = COLLECTIONS,
) -> frozenset[str]:
    connect = {definition.profile.supplier_key for definition in definitions}
    envelopes = {collection.supplier_key for collection in collections}
    return frozenset(connect & envelopes)


def build_supplier_gate(registered: Iterable[str] | None = None) -> SupplierGate:
    """The gate over this build's registry, or over an explicit set (tests, synthetic suppliers)."""
    known = frozenset(registered_suppliers() if registered is None else registered)

    def admits(supplier_key: str) -> bool:
        return supplier_key in known

    return admits
