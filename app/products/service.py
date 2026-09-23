from collections.abc import Iterable

from app.core.errors import NotFoundError
from app.products.catalog import (
    DISPLAY_FIELDS,
    SELECTION_ITEM_NOT_SELECTABLE,
    SELECTION_ITEM_OUTSIDE_PRODUCT,
    SELECTION_MEMBERSHIP_STALE,
    SELECTION_NOT_SELECTABLE,
    SelectionConflictError,
    decode_cursor,
    encode_cursor,
    item_reasons,
    member_sources,
    needle,
    page_limit,
    product_unselectable_reason,
    search_text,
    selection_request,
)
from app.products.contracts import (
    HandoffState,
    ItemSelectionView,
    ProductDetailView,
    ProductPageView,
    ProductRowView,
    RegistrationTargetView,
    SourceHandoffView,
    TargetItemView,
    product_view,
)
from app.products.store import NAME_FIELD, ProductFoundationStore, ProductReadback


class ProductsService:
    """PRODUCT DB owner: canonical read-back (M4 PR-C, ADR-0013).

    ProductFactsRevision is COLLECT's source truth (ADR-0010). The canonical Product is the
    ProductGroup of ADR-0013 §1, materialized from durably RECORDED revisions by
    ``app.products.materialization``. Everything here reads persisted canonical state: no price,
    no readiness and no source fact value is computed or copied.

    Gate 1 G1-C adds the operator's product DB path over the same read-back: the list, search and
    pagination, the detail, and the registration-target selection (``app.products.catalog``). Each
    reads one consistent snapshot and writes nothing.
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
            raise _unknown()
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

    def source_handoff(
        self, supplier_key: str, source_product_id: str, revision_id: str
    ) -> SourceHandoffView:
        """Where a recorded source revision stands in the Product DB now (Gate 1 G1-E).

        Read-only follow-through after COLLECT: it materializes nothing, invents no Product
        identity and never asks for another collection. A source no Product holds yet is
        ``NOT_YET_VISIBLE``; one whose Product has moved to another revision says so.
        """
        group = self._store.group_of_source(supplier_key, source_product_id)
        product = None if group is None else self._store.readback(group)
        member = (
            None
            if product is None
            else next(
                (
                    m
                    for m in product.members
                    if (m.supplier_key, m.source_product_id) == (supplier_key, source_product_id)
                ),
                None,
            )
        )
        if product is None or member is None:
            state, product_id, current = HandoffState.NOT_YET_VISIBLE, None, None
        else:
            product_id, current = product.product_group_id, member.current_source_revision_id
            state = (
                HandoffState.MATERIALIZED
                if current == revision_id
                else HandoffState.CURRENT_REVISION_DIFFERS
            )
        return SourceHandoffView(
            supplier_key=supplier_key,
            source_product_id=source_product_id,
            revision_id=revision_id,
            state=state,
            product_group_id=product_id,
            current_source_revision_id=current,
        )

    # ------------------------------------------------------------------ product DB (G1-C)

    def page(
        self, *, query: str | None = None, cursor: str | None = None, limit: int | None = None
    ) -> ProductPageView:
        """One page of ACTIVE Products, newest first, optionally matching a search."""
        text = search_text(query)
        size = page_limit(limit)
        after = None if cursor is None else decode_cursor(cursor, text)
        with self._store.reading() as unit:
            page = unit.active_product_page(needle=needle(text), after=after, limit=size)
            rows = []
            for key in page.keys:
                product = unit.readback(key.product_group_id)
                if product is None:  # pragma: no cover - listed in this same snapshot
                    raise _unknown()
                rows.append(
                    ProductRowView(
                        product=product_view(product),
                        member_names=member_sources(unit, product, (NAME_FIELD,), images=False),
                    )
                )
        return ProductPageView(
            products=tuple(rows),
            query=text,
            limit=size,
            next_cursor=encode_cursor(page.keys[-1], text) if page.has_more else None,
            matching_total=page.matching_total,
        )

    def detail(self, product_group_id: str) -> ProductDetailView:
        """One Product, retired or not, with its members' current source facts and each Item's
        selectability."""
        with self._store.reading() as unit:
            product = unit.readback(product_group_id)
            if product is None:
                raise _unknown()
            current = unit.membership_is_current(product_group_id)
            sources = member_sources(unit, product, DISPLAY_FIELDS, images=True)
        reasons = item_reasons(product, current)
        return ProductDetailView(
            product=product_view(product),
            member_sources=sources,
            item_selection=tuple(
                ItemSelectionView(item_id=item_id, selectable=reason is None, reason=reason)
                for item_id, reason in reasons.items()
            ),
            selection_unavailable_reason=product_unselectable_reason(product, current),
        )

    def registration_target(
        self,
        product_group_id: str,
        membership_revision_id: str | None,
        item_ids: Iterable[str] | None,
    ) -> RegistrationTargetView:
        """Revalidate a registration-target selection against the Product as it is now.

        The selection must name this ACTIVE Product's current membership revision and only Items
        of it that are selectable now; anything else — a retired Product, a moved membership, an
        Item of another Product, an Item that lost its binding — is refused whole. Nothing is
        written: no Draft, no PricingSnapshot, no account target and no registration row.
        """
        membership, chosen = selection_request(membership_revision_id, item_ids)
        with self._store.reading() as unit:
            product = unit.readback(product_group_id)
            if product is None:
                raise _unknown()
            current = unit.membership_is_current(product_group_id)
        product_reason = product_unselectable_reason(product, current)
        if product_reason is not None:
            raise SelectionConflictError(
                SELECTION_NOT_SELECTABLE,
                "no Item of this product can be chosen now",
                details={"reason": product_reason},
            )
        if product.membership_revision_id != membership:
            raise SelectionConflictError(
                SELECTION_MEMBERSHIP_STALE,
                "the product's membership changed since the selection was made",
                details={"membership_revision_id": product.membership_revision_id},
            )
        items = {item.item_id: item for item in product.items}
        outside = sorted(item for item in chosen if item not in items)
        if outside:
            raise SelectionConflictError(
                SELECTION_ITEM_OUTSIDE_PRODUCT,
                "a selection names only Items of its own product",
                details={"item_ids": outside},
            )
        reasons = item_reasons(product, current)
        refused = {item: reasons[item] for item in sorted(chosen) if reasons[item] is not None}
        if refused:
            raise SelectionConflictError(
                SELECTION_ITEM_NOT_SELECTABLE,
                "a chosen Item cannot be a registration target now",
                details={"items": refused},
            )
        assert product.membership_revision_no is not None
        return RegistrationTargetView(
            product_group_id=product.product_group_id,
            membership_revision_id=membership,
            membership_revision_no=product.membership_revision_no,
            items=tuple(
                TargetItemView(
                    item_id=item.item_id,
                    composition_signature=item.composition.composition_signature,
                    quantity=item.composition.quantity,
                    binding_id=item.current_binding.binding_id,
                    binding_kind=item.current_binding.binding_kind,
                )
                for item in product.items
                if item.item_id in chosen and item.current_binding is not None
            ),
        )


def _unknown() -> NotFoundError:
    return NotFoundError("PRODUCTS_PRODUCT_UNKNOWN", "no product has that identifier")
