from app.core.errors import NotFoundError
from app.products.store import ProductFoundationStore, ProductReadback


class ProductsService:
    """PRODUCT DB owner: canonical read-back (M4 PR-C, ADR-0013).

    ProductFactsRevision is COLLECT's source truth (ADR-0010). The canonical Product is the
    ProductGroup of ADR-0013 §1, materialized from durably RECORDED revisions by
    ``app.products.materialization``. Everything here reads persisted canonical state: no price,
    no readiness and no source fact value is computed or copied.
    """

    def __init__(self, store: ProductFoundationStore) -> None:
        self._store = store

    def product_count(self) -> int:
        """ACTIVE canonical Products: not revisions, not source products, not retired groups."""
        return self._store.active_group_count()

    def product(self, product_group_id: str) -> ProductReadback:
        """One Product by its canonical identifier. A retired one stays addressable for history."""
        found = self._store.readback(product_group_id)
        if found is None:
            raise NotFoundError("PRODUCTS_PRODUCT_UNKNOWN", "no product has that identifier")
        return found

    def product_of_source(self, supplier_key: str, source_product_id: str) -> ProductReadback:
        """The Product in which this source identity is CONFIRMED now."""
        group = self._store.group_of_source(supplier_key, source_product_id)
        if group is None:
            raise NotFoundError(
                "PRODUCTS_SOURCE_NOT_MATERIALIZED",
                "this source identity is not a confirmed member of any product",
            )
        return self.product(group)
