class ProductsService:
    """PRODUCT DB owner.

    ProductFactsRevision is COLLECT's source truth, persisted from M3 (Issue #52 §0, ADR-0010 §1).
    The canonical Product is the ProductGroup of ADR-0013. M4 PR-B adds its tables
    (``app.products.models``), but no product is materialized from a revision until PR-C. The
    catalogue is therefore still empty by construction, not by a stubbed query.
    """

    def product_count(self) -> int:
        return 0
