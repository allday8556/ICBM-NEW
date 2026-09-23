"""Read-back contract for one canonical Product (M4 PR-C, ADR-0013).

What the API returns is what the database holds: the group, its current membership revision, its
CONFIRMED members with each one's current source revision, and its Items with their structural
composition and current binding. Nothing is recomputed — no price, no readiness — and no source
fact value is copied: a member's facts are read through its current source revision.

Read-only: this contract creates nothing and changes nothing, and it is not a marketplace payload.

**Product DB read models** (Gate 1 G1-C, ADR-0015 §5). The operator screen's list, detail and
registration-target selection wrap :class:`ProductView` unchanged and add only what the screen
needs: each member's source facts read through its exact current source revision and named by
it, and each Item's server-owned selectability. There is no canonical product name: members that
state different names each keep their own. No price, readiness, compliance or marketplace verdict
appears here.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app.collect.facts import FactsStatus, FieldStatus
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
    """``quantity_offer_id`` names the exact offer of a SOURCE_OFFER and is ``None`` for
    BASE_PRODUCT; no source price is recomputed here."""

    binding_id: str
    binding_kind: BindingKind
    group_member_id: str
    provenance_revision_id: str
    valid_from: datetime
    fulfillment_quantity: int
    quantity_offer_id: str | None


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


# ---------------------------------------------------------------- product DB read models (G1-C)


class SourceFactView(BaseModel):
    """One source fact of one member, as its current source revision states it.

    ``status`` is ``None`` when the revision holds no such field. ``value`` is set only for a
    CONFIRMED fact; a fact under review or absent shows no value, and nothing fills it from another
    member or Product."""

    key: str
    status: FieldStatus | None
    value: str | None


class SourceImagesView(BaseModel):
    """The images field of the same revision: how many references it names, how many were
    included, and the stored-bytes digest of the first included representative, if any. No
    locator or URL is exposed."""

    status: FieldStatus | None
    references: int
    included: int
    representative_sha256: str | None


class MemberSourceView(BaseModel):
    """One CONFIRMED member and the facts of **its own** current source revision, named by
    ``source_revision_id`` (``None`` before any revision is current)."""

    member_id: str
    supplier_key: str
    source_product_id: str
    source_revision_id: str | None
    facts_status: FactsStatus | None
    facts: tuple[SourceFactView, ...]
    images: SourceImagesView | None


class ItemSelectionView(BaseModel):
    """Whether this Item may be chosen as a registration target now, and if not, the server's
    reason code."""

    item_id: str
    selectable: bool
    reason: str | None


class ProductRowView(BaseModel):
    """One list row: the canonical Product and each member's current name, member-scoped."""

    product: ProductView
    member_names: tuple[MemberSourceView, ...]


class ProductPageView(BaseModel):
    """One page of ACTIVE Products. ``next_cursor`` continues this same search and is ``None`` on
    the last page."""

    products: tuple[ProductRowView, ...]
    query: str | None
    limit: int
    next_cursor: str | None
    matching_total: int


class ProductDetailView(BaseModel):
    """One Product, retired or not, with every member's source facts and every Item's
    selectability. ``selection_unavailable_reason`` is the Product-wide reason no Item of it is
    selectable, if there is one."""

    product: ProductView
    member_sources: tuple[MemberSourceView, ...]
    item_selection: tuple[ItemSelectionView, ...]
    selection_unavailable_reason: str | None


class HandoffState(StrEnum):
    """Where one recorded source revision stands in the Product DB now (Gate 1 G1-E).

    ``MATERIALIZED``: a Product holds the source as a CONFIRMED member whose current revision is
    exactly this one. ``CURRENT_REVISION_DIFFERS``: a Product holds the source, but its current
    revision is another one. ``NOT_YET_VISIBLE``: no Product holds the source as a CONFIRMED member
    yet. None of them is a quality verdict, and none asks for another collection.
    """

    MATERIALIZED = "MATERIALIZED"
    CURRENT_REVISION_DIFFERS = "CURRENT_REVISION_DIFFERS"
    NOT_YET_VISIBLE = "NOT_YET_VISIBLE"


class SourceHandoffView(BaseModel):
    supplier_key: str
    source_product_id: str
    revision_id: str
    state: HandoffState
    product_group_id: str | None
    current_source_revision_id: str | None


class TargetItemView(BaseModel):
    item_id: str
    composition_signature: str
    quantity: int
    binding_id: str
    binding_kind: BindingKind


class RegistrationTargetView(BaseModel):
    """A selection the server revalidated against the current Product: one ACTIVE Product, its
    current membership revision and the chosen Items with the binding each holds now.

    It is a read-only handoff, never durable: nothing is created — no Draft, no PricingSnapshot,
    no account target and no registration row."""

    product_group_id: str
    membership_revision_id: str
    membership_revision_no: int
    items: tuple[TargetItemView, ...]


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
                    fulfillment_quantity=item.current_binding.fulfillment_quantity,
                    quantity_offer_id=item.current_binding.quantity_offer_id,
                ),
            )
            for item in product.items
        ),
    )
