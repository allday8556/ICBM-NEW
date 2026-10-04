"""Persistence owner for AtomicSKU-qualified Product Item identities."""

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.platform.core.clock import Clock
from app.platform.core.errors import InputValidationError, NotFoundError
from app.platform.db.database import Database
from app.stages.products.atomic_sku_item_models import AtomicSKUProductItem
from app.stages.products.atomic_sku_models import AtomicSKU
from app.stages.products.atomic_sku_store import AtomicSKUStore
from app.stages.products.models import ListingComposition


@dataclass(frozen=True)
class AtomicSKUItemRecord:
    atomic_sku_item_id: str
    product_group_id: str
    composition_id: str
    composition_signature: str
    atomic_sku_id: str
    atomic_sku_selection_signature: str


class AtomicSKUItemStore:
    """Creates stable Items only for AtomicSKUs in the product's current reviewed set."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    def item(
        self, product_group_id: str, composition_id: str, atomic_sku_id: str
    ) -> AtomicSKUItemRecord:
        with self._db.write() as session:
            composition = session.get(ListingComposition, composition_id)
            if composition is None:
                raise NotFoundError("PRODUCTS_COMPOSITION_UNKNOWN", "no composition has that id")
            atomic_sku = session.get(AtomicSKU, atomic_sku_id)
            if atomic_sku is None:
                raise NotFoundError("PRODUCTS_ATOMIC_SKU_UNKNOWN", "no AtomicSKU has that id")
            if atomic_sku.product_group_id != product_group_id:
                raise InputValidationError(
                    "PRODUCTS_ATOMIC_SKU_GROUP_MISMATCH",
                    "the AtomicSKU does not belong to this product group",
                )
            if not AtomicSKUStore.is_current_member_for_use(
                session, product_group_id, atomic_sku_id
            ):
                raise InputValidationError(
                    "PRODUCTS_ATOMIC_SKU_NOT_CURRENT",
                    "the AtomicSKU or its reviewed proof is not current for this product group",
                )
            found = session.scalars(
                select(AtomicSKUProductItem).where(
                    AtomicSKUProductItem.product_group_id == product_group_id,
                    AtomicSKUProductItem.composition_signature == composition.composition_signature,
                    AtomicSKUProductItem.atomic_sku_id == atomic_sku_id,
                )
            ).first()
            if found is not None:
                return self._record(found)
            row = AtomicSKUProductItem(
                atomic_sku_item_id=str(uuid.uuid4()),
                product_group_id=product_group_id,
                composition_id=composition_id,
                composition_signature=composition.composition_signature,
                atomic_sku_id=atomic_sku_id,
                atomic_sku_selection_signature=atomic_sku.selection_signature,
                created_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            return self._record(row)

    def find(
        self, product_group_id: str, composition_signature: str, atomic_sku_id: str
    ) -> AtomicSKUItemRecord | None:
        """Read one exact v2 Item identity without creating it."""
        with self._db.read() as session:
            row = session.scalars(
                select(AtomicSKUProductItem).where(
                    AtomicSKUProductItem.product_group_id == product_group_id,
                    AtomicSKUProductItem.composition_signature == composition_signature,
                    AtomicSKUProductItem.atomic_sku_id == atomic_sku_id,
                )
            ).first()
            return None if row is None else self._record(row)

    @staticmethod
    def _record(row: AtomicSKUProductItem) -> AtomicSKUItemRecord:
        return AtomicSKUItemRecord(
            row.atomic_sku_item_id,
            row.product_group_id,
            row.composition_id,
            row.composition_signature,
            row.atomic_sku_id,
            row.atomic_sku_selection_signature,
        )


__all__ = ["AtomicSKUItemRecord", "AtomicSKUItemStore"]
