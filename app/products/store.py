"""PRODUCT DB persistence for the M4 foundation (Issue #80 PR-B, ADR-0013).

This store writes and reads the foundation rows and nothing more. It never decides:
- which revision is current: the caller names the move and its reason; PR-C owns the rule;
- which source products belong together: grouping, matching, merge and split are PR-C and later;
- a price or a readiness: PR-D;
- a derived image: PR-E.

Every cross-row invariant is enforced by the database (migration 0012), so this store is not the
only guard. It adds the domain side:
- the canonical composition signature;
- a clear refusal before a write the database would reject anyway;
- **one unit of work for every change to a group's CONFIRMED set together with its next
  membership revision**. A database trigger can check that a snapshot is complete, but it cannot
  make one appear; this store is the only production writer of membership, and a repository rule
  keeps it so.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collect.facts import FieldStatus
from app.collect.models import ProductFactsField, ProductFactsRevision
from app.core.clock import Clock
from app.core.errors import InputValidationError, NotFoundError
from app.db.database import Database
from app.products.model import (
    BASE_PRODUCT_ABSENT_FIELDS,
    BASE_PRODUCT_PROVENANCE_FIELDS,
    DEFAULT_SINGLE_UNIT_SIGNATURE,
    MEMBER_TRANSITIONS,
    SIGNATURE_VERSION,
    BindingKind,
    CompositionSpec,
    GroupStatus,
    MemberStatus,
    MoveReason,
    canonical_structure,
    composition_signature,
)
from app.products.models import (
    CurrentSourceRevisionMove,
    GroupMember,
    GroupMembershipRevision,
    ListingComposition,
    ProductGroup,
    ProductItem,
    SourceBinding,
    SourceProduct,
)


@dataclass(frozen=True)
class SourceProductRecord:
    source_product_uid: str
    supplier_key: str
    source_product_id: str


@dataclass(frozen=True)
class RevisionMove:
    move_id: str
    source_product_uid: str
    sequence: int
    revision_id: str
    previous_revision_id: str | None
    reason: MoveReason


@dataclass(frozen=True)
class MembershipRevision:
    membership_revision_id: str
    product_group_id: str
    revision_no: int
    source_product_uids: tuple[str, ...]


@dataclass(frozen=True)
class MembershipChange:
    """A persisted member change, with the membership revision it appended (``None`` when the
    CONFIRMED set did not change)."""

    member_id: str
    revision: MembershipRevision | None


@dataclass(frozen=True)
class CompositionRecord:
    composition_id: str
    composition_signature: str
    quantity: int


@dataclass(frozen=True)
class ItemRecord:
    item_id: str
    product_group_id: str
    composition_signature: str


@dataclass(frozen=True)
class BindingRecord:
    binding_id: str
    item_id: str
    group_member_id: str
    binding_kind: BindingKind
    provenance_revision_id: str
    valid_from: datetime


class ProductFoundationStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # ------------------------------------------------------------------ source identity

    def source_product(self, supplier_key: str, source_product_id: str) -> SourceProductRecord:
        """The one identity row of a source product that a revision already names.

        It is created on first use. An identity no revision states is refused: M4 never invents
        a source product.
        """
        with self._db.write() as session:
            row = _source_product(session, supplier_key, source_product_id)
            if row is None:
                named = session.scalar(
                    select(func.count())
                    .select_from(ProductFactsRevision)
                    .where(
                        ProductFactsRevision.supplier_key == supplier_key,
                        ProductFactsRevision.source_product_id == source_product_id,
                    )
                )
                if not named:
                    raise NotFoundError(
                        "PRODUCTS_SOURCE_IDENTITY_UNKNOWN",
                        "no revision names this source identity",
                    )
                row = SourceProduct(
                    source_product_uid=str(uuid.uuid4()),
                    supplier_key=supplier_key,
                    source_product_id=source_product_id,
                    created_at=self._clock.now(),
                )
                session.add(row)
            return _source_record(row)

    # ------------------------------------------------------------------ current source revision

    def current_source_revision(self, source_product_uid: str) -> str | None:
        """The revision the newest move names, or ``None`` before any move."""
        with self._db.read() as session:
            return session.scalar(
                select(CurrentSourceRevisionMove.revision_id)
                .where(CurrentSourceRevisionMove.source_product_uid == source_product_uid)
                .order_by(CurrentSourceRevisionMove.sequence.desc())
                .limit(1)
            )

    def record_move(
        self,
        source_product_uid: str,
        revision_id: str,
        *,
        reason: MoveReason,
        decided_by: str,
        correlation_id: str,
        rule_version: str | None = None,
    ) -> RevisionMove:
        """Append one move that the caller decided. The store chooses nothing: it only extends
        the history from its current end, and the database refuses a revision of another source
        identity or a move out of order."""
        with self._db.write() as session:
            last = session.scalars(
                select(CurrentSourceRevisionMove)
                .where(CurrentSourceRevisionMove.source_product_uid == source_product_uid)
                .order_by(CurrentSourceRevisionMove.sequence.desc())
                .limit(1)
            ).first()
            sequence = 1 if last is None else last.sequence + 1
            if (reason is MoveReason.INITIAL) != (sequence == 1):
                raise InputValidationError(
                    "PRODUCTS_MOVE_REASON_INVALID",
                    "INITIAL opens a history and nothing else does",
                )
            row = CurrentSourceRevisionMove(
                move_id=str(uuid.uuid4()),
                source_product_uid=source_product_uid,
                sequence=sequence,
                revision_id=revision_id,
                previous_revision_id=None if last is None else last.revision_id,
                reason=reason.value,
                rule_version=rule_version,
                decided_by=decided_by,
                correlation_id=correlation_id,
                moved_at=self._clock.now(),
            )
            session.add(row)
            session.flush()
            return RevisionMove(
                row.move_id,
                row.source_product_uid,
                row.sequence,
                row.revision_id,
                row.previous_revision_id,
                reason,
            )

    # ------------------------------------------------------------------ canonical product

    def create_group(self, *, decided_by: str) -> str:
        with self._db.write() as session:
            group_id = str(uuid.uuid4())
            session.add(
                ProductGroup(
                    product_group_id=group_id,
                    status=GroupStatus.ACTIVE.value,
                    decided_by=decided_by,
                    created_at=self._clock.now(),
                    retired_at=None,
                )
            )
            return group_id

    # A change to a group's CONFIRMED member set and the membership revision recording it are
    # one unit of work (ADR-0013 §4; PR #82 review 5247426764). No method here can commit one
    # without the other. A repository rule keeps this store the only production writer of
    # membership, and requires every method here that could change the CONFIRMED set to append
    # the revision.

    def add_candidate(
        self,
        product_group_id: str,
        source_product_uid: str,
        *,
        decided_by: str,
        match_method: str | None = None,
        match_confidence: float | None = None,
        match_strategy_version: str | None = None,
    ) -> str:
        """Record a CANDIDATE member. A candidate is never canonical: the CONFIRMED set does not
        change, so no membership revision is appended."""
        with self._db.write() as session:
            now = self._clock.now()
            member_id = str(uuid.uuid4())
            session.add(
                GroupMember(
                    member_id=member_id,
                    product_group_id=product_group_id,
                    source_product_uid=source_product_uid,
                    status=MemberStatus.CANDIDATE.value,
                    match_method=match_method,
                    match_confidence=match_confidence,
                    match_strategy_version=match_strategy_version,
                    decided_by=decided_by,
                    created_at=now,
                    decided_at=now,
                )
            )
            return member_id

    def confirm_new_member(
        self,
        product_group_id: str,
        source_product_uid: str,
        *,
        reason: str,
        decided_by: str,
        correlation_id: str,
        match_method: str | None = None,
        match_confidence: float | None = None,
        match_strategy_version: str | None = None,
    ) -> MembershipChange:
        """Add a CONFIRMED member and append the group's next membership revision, atomically.

        The caller has decided the membership; this only persists that decision and its snapshot
        together. If either write fails, neither is committed.
        """
        with self._db.write() as session:
            now = self._clock.now()
            member_id = str(uuid.uuid4())
            session.add(
                GroupMember(
                    member_id=member_id,
                    product_group_id=product_group_id,
                    source_product_uid=source_product_uid,
                    status=MemberStatus.CONFIRMED.value,
                    match_method=match_method,
                    match_confidence=match_confidence,
                    match_strategy_version=match_strategy_version,
                    decided_by=decided_by,
                    created_at=now,
                    decided_at=now,
                )
            )
            session.flush()
            revision = self._append_membership_revision(
                session,
                product_group_id,
                reason=reason,
                decided_by=decided_by,
                correlation_id=correlation_id,
            )
            return MembershipChange(member_id, revision)

    def change_member_status(
        self,
        member_id: str,
        status: MemberStatus,
        *,
        reason: str,
        decided_by: str,
        correlation_id: str,
    ) -> MembershipChange:
        """Move a member one way, and record the canonical set if it changed.

        A move into or out of CONFIRMED changes the canonical set, so the next membership
        revision is appended in the same unit of work. ``CANDIDATE -> REJECTED`` changes nothing
        canonical and appends none.
        """
        with self._db.write() as session:
            row = session.get(GroupMember, member_id)
            if row is None:
                raise NotFoundError("PRODUCTS_MEMBER_UNKNOWN", "no group member has that id")
            before = MemberStatus(row.status)
            if (before, status) not in MEMBER_TRANSITIONS:
                raise InputValidationError(
                    "PRODUCTS_MEMBER_TRANSITION_INVALID",
                    f"a member does not move from {before.value} to {status.value}",
                )
            row.status = status.value
            row.decided_by = decided_by
            row.decided_at = self._clock.now()
            revision = None
            if MemberStatus.CONFIRMED in (before, status):
                session.flush()
                revision = self._append_membership_revision(
                    session,
                    row.product_group_id,
                    reason=reason,
                    decided_by=decided_by,
                    correlation_id=correlation_id,
                )
            return MembershipChange(member_id, revision)

    def current_membership_revision(self, product_group_id: str) -> MembershipRevision | None:
        """The group's newest membership revision, or ``None`` before its first CONFIRMED member."""
        with self._db.read() as session:
            row = session.scalars(
                select(GroupMembershipRevision)
                .where(GroupMembershipRevision.product_group_id == product_group_id)
                .order_by(GroupMembershipRevision.revision_no.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            return MembershipRevision(
                row.membership_revision_id,
                product_group_id,
                row.revision_no,
                tuple(json.loads(row.members_json)),
            )

    def _append_membership_revision(
        self,
        session: Session,
        product_group_id: str,
        *,
        reason: str,
        decided_by: str,
        correlation_id: str,
    ) -> MembershipRevision:
        """Snapshot the group's complete CONFIRMED set, sorted, as its next revision, inside the
        caller's unit of work. It is never called on its own: only a set change calls it."""
        members = tuple(
            sorted(
                session.scalars(
                    select(GroupMember.source_product_uid).where(
                        GroupMember.product_group_id == product_group_id,
                        GroupMember.status == MemberStatus.CONFIRMED.value,
                    )
                )
            )
        )
        last = session.scalar(
            select(func.max(GroupMembershipRevision.revision_no)).where(
                GroupMembershipRevision.product_group_id == product_group_id
            )
        )
        row = GroupMembershipRevision(
            membership_revision_id=str(uuid.uuid4()),
            product_group_id=product_group_id,
            revision_no=(last or 0) + 1,
            members_json=json.dumps(list(members)),
            member_count=len(members),
            reason=reason,
            decided_by=decided_by,
            correlation_id=correlation_id,
            created_at=self._clock.now(),
        )
        session.add(row)
        session.flush()
        return MembershipRevision(
            row.membership_revision_id, product_group_id, row.revision_no, members
        )

    # ------------------------------------------------------------------ composition and Item

    def composition(self, spec: CompositionSpec) -> CompositionRecord:
        """The one immutable composition with this canonical structure, created on first use."""
        structure = canonical_structure(spec)
        signature = composition_signature(spec)
        with self._db.write() as session:
            row = session.scalars(
                select(ListingComposition).where(
                    ListingComposition.composition_signature == signature
                )
            ).first()
            if row is None:
                row = ListingComposition(
                    composition_id=str(uuid.uuid4()),
                    quantity=structure["quantity"],
                    unit_amount=structure["unit_amount"],
                    unit_code=structure["unit_code"],
                    pack_count=structure["pack_count"],
                    units_per_pack=structure["units_per_pack"],
                    total_amount=structure["total_amount"],
                    composition_signature=signature,
                    signature_version=SIGNATURE_VERSION,
                    created_at=self._clock.now(),
                )
                session.add(row)
            return CompositionRecord(row.composition_id, row.composition_signature, row.quantity)

    def item(self, product_group_id: str, composition_id: str) -> ItemRecord:
        """The one Item of this group with this composition's signature, created on first use."""
        with self._db.write() as session:
            composition = session.get(ListingComposition, composition_id)
            if composition is None:
                raise NotFoundError("PRODUCTS_COMPOSITION_UNKNOWN", "no composition has that id")
            row = session.scalars(
                select(ProductItem).where(
                    ProductItem.product_group_id == product_group_id,
                    ProductItem.composition_signature == composition.composition_signature,
                )
            ).first()
            if row is None:
                row = ProductItem(
                    item_id=str(uuid.uuid4()),
                    product_group_id=product_group_id,
                    composition_id=composition_id,
                    composition_signature=composition.composition_signature,
                    created_at=self._clock.now(),
                )
                session.add(row)
            return ItemRecord(row.item_id, row.product_group_id, row.composition_signature)

    # ------------------------------------------------------------------ source binding

    def bind_base_product(
        self,
        item_id: str,
        group_member_id: str,
        provenance_revision_id: str,
        *,
        decided_by: str,
        correlation_id: str,
    ) -> BindingRecord:
        """Bind an Item to its member's base product: the current binding is closed and this one
        opens, in one unit of work.

        The revision must state ``options`` and ``quantity_tiers`` as ABSENT. Nothing is inferred
        from them: no source SKU and no quantity offer is created, and none is referenced.

        The Item must be the default single unit: quantity 1, every unit and pack field unknown
        (PR #82 review 5247426764). A quantity-1 structure that states a pack, a unit or a total
        is a seller configuration the source never proved, so a base product cannot fulfil it.
        """
        with self._db.write() as session:
            signature = session.scalar(
                select(ProductItem.composition_signature).where(ProductItem.item_id == item_id)
            )
            if signature != DEFAULT_SINGLE_UNIT_SIGNATURE:
                raise InputValidationError(
                    "PRODUCTS_BASE_PRODUCT_NOT_DEFAULT_UNIT",
                    "a base-product binding fulfils only the default single-unit composition",
                )
            absent = set(
                session.scalars(
                    select(ProductFactsField.field_key).where(
                        ProductFactsField.revision_id == provenance_revision_id,
                        ProductFactsField.field_key.in_(BASE_PRODUCT_ABSENT_FIELDS),
                        ProductFactsField.status == FieldStatus.ABSENT.value,
                    )
                )
            )
            if absent != set(BASE_PRODUCT_ABSENT_FIELDS):
                raise InputValidationError(
                    "PRODUCTS_BASE_PRODUCT_NOT_PROVEN",
                    "a base-product binding needs a revision stating no options and no tiers",
                )
            now = self._clock.now()
            for open_binding in session.scalars(
                select(SourceBinding).where(
                    SourceBinding.item_id == item_id, SourceBinding.valid_to.is_(None)
                )
            ):
                open_binding.valid_to = now
            session.flush()
            row = SourceBinding(
                binding_id=str(uuid.uuid4()),
                item_id=item_id,
                group_member_id=group_member_id,
                binding_kind=BindingKind.BASE_PRODUCT.value,
                fulfillment_quantity=1,
                provenance_revision_id=provenance_revision_id,
                provenance_fields=json.dumps(list(BASE_PRODUCT_PROVENANCE_FIELDS)),
                decided_by=decided_by,
                correlation_id=correlation_id,
                valid_from=now,
                valid_to=None,
            )
            session.add(row)
            session.flush()
            return BindingRecord(
                row.binding_id,
                item_id,
                group_member_id,
                BindingKind.BASE_PRODUCT,
                provenance_revision_id,
                now,
            )


def _source_product(
    session: Session, supplier_key: str, source_product_id: str
) -> SourceProduct | None:
    return session.scalars(
        select(SourceProduct).where(
            SourceProduct.supplier_key == supplier_key,
            SourceProduct.source_product_id == source_product_id,
        )
    ).first()


def _source_record(row: SourceProduct) -> SourceProductRecord:
    return SourceProductRecord(row.source_product_uid, row.supplier_key, row.source_product_id)
