"""Read-back contract for one canonical Product (M4 PR-C, ADR-0013).

What the API returns is what the database holds: the group, its current membership revision, its
CONFIRMED members with each one's current source revision, and its Items with their structural
composition and current binding. Nothing is recomputed — no price, no readiness — and no source
fact value is copied: a member's facts are read through its current source revision.

Read-only: this contract creates nothing and changes nothing, and it is not a marketplace payload.
"""

from datetime import datetime

from pydantic import BaseModel

from app.collect.facts import FactsStatus
from app.products.model import BindingKind, GroupStatus
from app.products.store import ProductReadback


class ProductMemberView(BaseModel):
    member_id: str
    source_product_uid: str
    supplier_key: str
    source_product_id: str
    current_source_revision_id: str | None
    current_facts_status: FactsStatus | None


class CompositionView(BaseModel):
    composition_id: str
    composition_signature: str
    signature_version: str
    quantity: int
    unit_amount: str | None
    unit_code: str | None
    pack_count: int | None
    units_per_pack: int | None
    total_amount: str | None


class BindingView(BaseModel):
    binding_id: str
    binding_kind: BindingKind
    group_member_id: str
    provenance_revision_id: str
    valid_from: datetime


class ProductItemView(BaseModel):
    """``current_binding`` is ``None`` when no binding is valid now."""

    item_id: str
    composition: CompositionView
    current_binding: BindingView | None


class ProductView(BaseModel):
    product_group_id: str
    status: GroupStatus
    created_at: datetime
    retired_at: datetime | None
    membership_revision_id: str | None
    membership_revision_no: int | None
    members: tuple[ProductMemberView, ...]
    items: tuple[ProductItemView, ...]


def product_view(product: ProductReadback) -> ProductView:
    return ProductView(
        product_group_id=product.product_group_id,
        status=product.status,
        created_at=product.created_at,
        retired_at=product.retired_at,
        membership_revision_id=product.membership_revision_id,
        membership_revision_no=product.membership_revision_no,
        members=tuple(
            ProductMemberView(
                member_id=member.member_id,
                source_product_uid=member.source_product_uid,
                supplier_key=member.supplier_key,
                source_product_id=member.source_product_id,
                current_source_revision_id=member.current_source_revision_id,
                current_facts_status=member.current_facts_status,
            )
            for member in product.members
        ),
        items=tuple(
            ProductItemView(
                item_id=item.item_id,
                composition=CompositionView(
                    composition_id=item.composition.composition_id,
                    composition_signature=item.composition.composition_signature,
                    signature_version=item.composition.signature_version,
                    quantity=item.composition.quantity,
                    unit_amount=item.composition.unit_amount,
                    unit_code=item.composition.unit_code,
                    pack_count=item.composition.pack_count,
                    units_per_pack=item.composition.units_per_pack,
                    total_amount=item.composition.total_amount,
                ),
                current_binding=None
                if item.current_binding is None
                else BindingView(
                    binding_id=item.current_binding.binding_id,
                    binding_kind=item.current_binding.binding_kind,
                    group_member_id=item.current_binding.group_member_id,
                    provenance_revision_id=item.current_binding.provenance_revision_id,
                    valid_from=item.current_binding.valid_from,
                ),
            )
            for item in product.items
        ),
    )
