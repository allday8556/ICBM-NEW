class ProductsService:
    """PRODUCT DB owner.

    The canonical Product identity and its link to the current accepted ProductFactsRevision
    arrive in M4 after architect schema review (ARCHITECT_REVIEW §8). ProductFactsRevision itself
    is COLLECT's source truth, persisted from M3 (Issue #52 §0, ADR-0010 §1). Until M4 no table
    can hold a product, so the catalogue is empty by construction rather than by a stubbed query.
    """

    def product_count(self) -> int:
        return 0
