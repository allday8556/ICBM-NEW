class ProductsService:
    """PRODUCT DB owner.

    The canonical Product / ProductFactsRevision schema is introduced in M4 after architect
    schema review (ARCHITECT_REVIEW §8). Until then no table can hold a product, so the
    catalogue is empty by construction rather than by a stubbed query.
    """

    def product_count(self) -> int:
        return 0
