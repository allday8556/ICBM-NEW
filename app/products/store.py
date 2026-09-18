"""PRODUCT DB persistence for the M4 foundation (Issue #80 PR-B, ADR-0013).

This store writes and reads the foundation rows and nothing more. It never decides:
- which revision is current: the caller names the move and its reason (PR-C's materializer owns
  the automatic rule);
- which source products belong together: grouping, matching, merge and split belong to callers;
- a price or a readiness: PR-D;
- a derived image: PR-E.

Every cross-row invariant is enforced by the database (migration 0012), so this store is not the
only guard. It adds the domain side:
- the canonical composition signature;
- a clear refusal before a write the database would reject anyway;
- **one unit of work for every change to a group's CONFIRMED set together with its next
  membership revision**. A database trigger can check that a snapshot is complete, but it cannot
  make one appear; this module is the only production writer of membership, and a repository
  rule keeps it so.

A caller that must commit several foundation writes as one decision (PR-C, Issue #80 kickoff
5736827688 §8) opens :meth:`ProductFoundationStore.transaction` and makes them all on the one
:class:`ProductFoundationUnit` it yields. Every single-write method of the store is the same unit
of work over its own transaction.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.collect.facts import FactsStatus, FieldStatus
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
class ConfirmedMembership:
    """Where a source product is canonical now: its one CONFIRMED member and that member's group."""

    member_id: str
    product_group_id: str
    group_status: GroupStatus


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


# ---------------------------------------------------------------- canonical read-back


@dataclass(frozen=True)
class MemberReadback:
    member_id: str
    source_product_uid: str
    supplier_key: str
    source_product_id: str
    current_source_revision_id: str | None
    current_facts_status: FactsStatus | None


@dataclass(frozen=True)
class CompositionReadback:
    composition_id: str
    composition_signature: str
    signature_version: str
    quantity: int
    unit_amount: str | None
    unit_code: str | None
    pack_count: int | None
    units_per_pack: int | None
    total_amount: str | None


@dataclass(frozen=True)
class BindingReadback:
    binding_id: str
    binding_kind: BindingKind
    group_member_id: str
    provenance_revision_id: str
    valid_from: datetime


@dataclass(frozen=True)
class ItemReadback:
    item_id: str
    composition: CompositionReadback
    current_binding: BindingReadback | None


@dataclass(frozen=True)
class ProductReadback:
    """One canonical Product (ProductGroup) exactly as the database holds it. Nothing is
    recomputed: no price, no readiness and no source fact value."""

    product_group_id: str
    status: GroupStatus
    created_at: datetime
    retired_at: datetime | None
    membership_revision_id: str | None
    membership_revision_no: int | None
    members: tuple[MemberReadback, ...]
    items: tuple[ItemReadback, ...]


class ProductFoundationStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    @contextmanager
    def transaction(self) -> Iterator["ProductFoundationUnit"]:
        """One unit of work across several foundation writes: all commit together, or none do."""
        with self._db.write() as session:
            yield ProductFoundationUnit(session, self._clock)

    @contextmanager
    def _reading(self) -> Iterator["ProductFoundationUnit"]:
        with self._db.read() as session:
            yield ProductFoundationUnit(session, self._clock)

    # ------------------------------------------------------------------ single writes

    def source_product(self, supplier_key: str, source_product_id: str) -> SourceProductRecord:
        with self.transaction() as unit:
            return unit.source_product(supplier_key, source_product_id)

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
        with self.transaction() as unit:
            return unit.record_move(
                source_product_uid,
                revision_id,
                reason=reason,
                decided_by=decided_by,
                correlation_id=correlation_id,
                rule_version=rule_version,
            )

    def create_group(self, *, decided_by: str) -> str:
        with self.transaction() as unit:
            return unit.create_group(decided_by=decided_by)

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
        with self.transaction() as unit:
            return unit.add_candidate(
                product_group_id,
                source_product_uid,
                decided_by=decided_by,
                match_method=match_method,
                match_confidence=match_confidence,
                match_strategy_version=match_strategy_version,
            )

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
        with self.transaction() as unit:
            return unit.confirm_new_member(
                product_group_id,
                source_product_uid,
                reason=reason,
                decided_by=decided_by,
                correlation_id=correlation_id,
                match_method=match_method,
                match_confidence=match_confidence,
                match_strategy_version=match_strategy_version,
            )

    def change_member_status(
        self,
        member_id: str,
        status: MemberStatus,
        *,
        reason: str,
        decided_by: str,
        correlation_id: str,
    ) -> MembershipChange:
        with self.transaction() as unit:
            return unit.change_member_status(
                member_id,
                status,
                reason=reason,
                decided_by=decided_by,
                correlation_id=correlation_id,
            )

    def composition(self, spec: CompositionSpec) -> CompositionRecord:
        with self.transaction() as unit:
            return unit.composition(spec)

    def item(self, product_group_id: str, composition_id: str) -> ItemRecord:
        with self.transaction() as unit:
            return unit.item(product_group_id, composition_id)

    def bind_base_product(
        self,
        item_id: str,
        group_member_id: str,
        provenance_revision_id: str,
        *,
        decided_by: str,
        correlation_id: str,
    ) -> BindingRecord:
        with self.transaction() as unit:
            return unit.bind_base_product(
                item_id,
                group_member_id,
                provenance_revision_id,
                decided_by=decided_by,
                correlation_id=correlation_id,
            )

    # ------------------------------------------------------------------ reads

    def current_source_revision(self, source_product_uid: str) -> str | None:
        """The revision the newest move names, or ``None`` before any move."""
        with self._reading() as unit:
            move = unit.current_move(source_product_uid)
            return None if move is None else move.revision_id

    def current_membership_revision(self, product_group_id: str) -> MembershipRevision | None:
        """The group's newest membership revision, or ``None`` before its first CONFIRMED member."""
        with self._reading() as unit:
            return unit.current_membership_revision(product_group_id)

    def active_group_count(self) -> int:
        """How many canonical Products are ACTIVE. A retired group is history, not a product."""
        with self._db.read() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(ProductGroup)
                    .where(ProductGroup.status == GroupStatus.ACTIVE.value)
                )
                or 0
            )

    def readback(self, product_group_id: str) -> ProductReadback | None:
        """One group as persisted, retired or not: a retired group stays addressable for
        history."""
        with self._reading() as unit:
            return unit.readback(product_group_id)

    def group_of_source(self, supplier_key: str, source_product_id: str) -> str | None:
        """The group in which this source identity is CONFIRMED now, if any."""
        with self._reading() as unit:
            row = _source_product(unit.session, supplier_key, source_product_id)
            if row is None:
                return None
            membership = unit.confirmed_membership(row.source_product_uid)
            return None if membership is None else membership.product_group_id


class ProductFoundationUnit:
    """The foundation writes and reads over one caller-owned session.

    It never commits: the session's owner does, once, for everything done here. A failure at any
    step therefore leaves nothing behind.
    """

    def __init__(self, session: Session, clock: Clock) -> None:
        self.session = session
        self._clock = clock

    # ------------------------------------------------------------------ source identity

    def source_product(self, supplier_key: str, source_product_id: str) -> SourceProductRecord:
        """The one identity row of a source product that a revision already names.

        It is created on first use. An identity no revision states is refused: M4 never invents
        a source product.
        """
        session = self.session
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
            session.flush()
        return _source_record(row)

    # ------------------------------------------------------------------ current source revision

    def current_move(self, source_product_uid: str) -> RevisionMove | None:
        """The newest move of this source product's pointer: it names the current revision."""
        row = self.session.scalars(
            select(CurrentSourceRevisionMove)
            .where(CurrentSourceRevisionMove.source_product_uid == source_product_uid)
            .order_by(CurrentSourceRevisionMove.sequence.desc())
            .limit(1)
        ).first()
        return None if row is None else _move_record(row)

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
        last = self.current_move(source_product_uid)
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
        self.session.add(row)
        self.session.flush()
        return _move_record(row)

    # ------------------------------------------------------------------ canonical product

    def create_group(self, *, decided_by: str) -> str:
        group_id = str(uuid.uuid4())
        self.session.add(
            ProductGroup(
                product_group_id=group_id,
                status=GroupStatus.ACTIVE.value,
                decided_by=decided_by,
                created_at=self._clock.now(),
                retired_at=None,
            )
        )
        self.session.flush()
        return group_id

    def confirmed_membership(self, source_product_uid: str) -> ConfirmedMembership | None:
        """The source product's one CONFIRMED member, if it has one, and its group's status."""
        row = self.session.execute(
            select(GroupMember.member_id, GroupMember.product_group_id, ProductGroup.status)
            .join(ProductGroup, ProductGroup.product_group_id == GroupMember.product_group_id)
            .where(
                GroupMember.source_product_uid == source_product_uid,
                GroupMember.status == MemberStatus.CONFIRMED.value,
            )
        ).first()
        if row is None:
            return None
        return ConfirmedMembership(row[0], row[1], GroupStatus(row[2]))

    # A change to a group's CONFIRMED member set and the membership revision recording it are
    # one unit of work (ADR-0013 §4; PR #82 review 5247426764). No method here can make one
    # without the other. A repository rule keeps this module the only production writer of
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
        now = self._clock.now()
        member_id = str(uuid.uuid4())
        self.session.add(
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
        self.session.flush()
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
        now = self._clock.now()
        member_id = str(uuid.uuid4())
        self.session.add(
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
        self.session.flush()
        revision = self._append_membership_revision(
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
        row = self.session.get(GroupMember, member_id)
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
            self.session.flush()
            revision = self._append_membership_revision(
                row.product_group_id,
                reason=reason,
                decided_by=decided_by,
                correlation_id=correlation_id,
            )
        return MembershipChange(member_id, revision)

    def current_membership_revision(self, product_group_id: str) -> MembershipRevision | None:
        """The group's newest membership revision, or ``None`` before its first CONFIRMED member."""
        row = self.session.scalars(
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
        product_group_id: str,
        *,
        reason: str,
        decided_by: str,
        correlation_id: str,
    ) -> MembershipRevision:
        """Snapshot the group's complete CONFIRMED set, sorted, as its next revision, inside the
        caller's unit of work. It is never called on its own: only a set change calls it."""
        session = self.session
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
        row = self.session.scalars(
            select(ListingComposition).where(ListingComposition.composition_signature == signature)
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
            self.session.add(row)
            self.session.flush()
        return CompositionRecord(row.composition_id, row.composition_signature, row.quantity)

    def find_item(self, product_group_id: str, signature: str) -> ItemRecord | None:
        """The group's Item with this composition signature, if it exists. Creates nothing."""
        row = self.session.scalars(
            select(ProductItem).where(
                ProductItem.product_group_id == product_group_id,
                ProductItem.composition_signature == signature,
            )
        ).first()
        return None if row is None else ItemRecord(row.item_id, row.product_group_id, signature)

    def item(self, product_group_id: str, composition_id: str) -> ItemRecord:
        """The one Item of this group with this composition's signature, created on first use."""
        composition = self.session.get(ListingComposition, composition_id)
        if composition is None:
            raise NotFoundError("PRODUCTS_COMPOSITION_UNKNOWN", "no composition has that id")
        found = self.find_item(product_group_id, composition.composition_signature)
        if found is not None:
            return found
        row = ProductItem(
            item_id=str(uuid.uuid4()),
            product_group_id=product_group_id,
            composition_id=composition_id,
            composition_signature=composition.composition_signature,
            created_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        return ItemRecord(row.item_id, row.product_group_id, row.composition_signature)

    # ------------------------------------------------------------------ source binding

    def open_bindings_of_member(self, group_member_id: str) -> tuple[BindingRecord, ...]:
        """Every binding this member currently holds, on any Item."""
        return tuple(
            _binding_record(row)
            for row in self.session.scalars(
                select(SourceBinding)
                .where(
                    SourceBinding.group_member_id == group_member_id,
                    SourceBinding.valid_to.is_(None),
                )
                .order_by(SourceBinding.valid_from, SourceBinding.binding_id)
            )
        )

    def open_binding_of_item(self, item_id: str) -> BindingRecord | None:
        """The Item's one open binding, if any: the database allows at most one."""
        row = self.session.scalars(
            select(SourceBinding).where(
                SourceBinding.item_id == item_id, SourceBinding.valid_to.is_(None)
            )
        ).first()
        return None if row is None else _binding_record(row)

    def close_binding(self, binding_id: str) -> None:
        """Close one open binding's validity window. A binding is closed, never edited."""
        row = self.session.get(SourceBinding, binding_id)
        if row is None or row.valid_to is not None:
            raise InputValidationError(
                "PRODUCTS_BINDING_NOT_OPEN", "only an open binding can be closed"
            )
        row.valid_to = self._clock.now()
        self.session.flush()

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
        session = self.session
        signature = session.scalar(
            select(ProductItem.composition_signature).where(ProductItem.item_id == item_id)
        )
        if signature != DEFAULT_SINGLE_UNIT_SIGNATURE:
            raise InputValidationError(
                "PRODUCTS_BASE_PRODUCT_NOT_DEFAULT_UNIT",
                "a base-product binding fulfils only the default single-unit composition",
            )
        if not self.states_no_options_and_no_tiers(provenance_revision_id):
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
        return _binding_record(row)

    def states_no_options_and_no_tiers(self, revision_id: str) -> bool:
        """Whether this revision states exactly ``options = ABSENT`` and ``quantity_tiers =
        ABSENT``. Any other status, REVIEW_REQUIRED included, proves nothing (ruling B)."""
        absent = set(
            self.session.scalars(
                select(ProductFactsField.field_key).where(
                    ProductFactsField.revision_id == revision_id,
                    ProductFactsField.field_key.in_(BASE_PRODUCT_ABSENT_FIELDS),
                    ProductFactsField.status == FieldStatus.ABSENT.value,
                )
            )
        )
        return absent == set(BASE_PRODUCT_ABSENT_FIELDS)

    # ------------------------------------------------------------------ canonical read-back

    def readback(self, product_group_id: str) -> ProductReadback | None:
        session = self.session
        group = session.get(ProductGroup, product_group_id)
        if group is None:
            return None
        membership = self.current_membership_revision(product_group_id)
        members = []
        for member, source in session.execute(
            select(GroupMember, SourceProduct)
            .join(SourceProduct, SourceProduct.source_product_uid == GroupMember.source_product_uid)
            .where(
                GroupMember.product_group_id == product_group_id,
                GroupMember.status == MemberStatus.CONFIRMED.value,
            )
            .order_by(SourceProduct.supplier_key, SourceProduct.source_product_id)
        ).tuples():
            move = self.current_move(source.source_product_uid)
            facts_status = (
                None
                if move is None
                else session.scalar(
                    select(ProductFactsRevision.facts_status).where(
                        ProductFactsRevision.revision_id == move.revision_id
                    )
                )
            )
            members.append(
                MemberReadback(
                    member_id=member.member_id,
                    source_product_uid=source.source_product_uid,
                    supplier_key=source.supplier_key,
                    source_product_id=source.source_product_id,
                    current_source_revision_id=None if move is None else move.revision_id,
                    current_facts_status=None
                    if facts_status is None
                    else FactsStatus(facts_status),
                )
            )
        items = []
        for item, composition in session.execute(
            select(ProductItem, ListingComposition)
            .join(
                ListingComposition,
                ListingComposition.composition_id == ProductItem.composition_id,
            )
            .where(ProductItem.product_group_id == product_group_id)
            .order_by(ProductItem.composition_signature)
        ).tuples():
            binding = session.scalars(
                select(SourceBinding).where(
                    SourceBinding.item_id == item.item_id, SourceBinding.valid_to.is_(None)
                )
            ).first()
            items.append(
                ItemReadback(
                    item_id=item.item_id,
                    composition=CompositionReadback(
                        composition_id=composition.composition_id,
                        composition_signature=composition.composition_signature,
                        signature_version=composition.signature_version,
                        quantity=composition.quantity,
                        unit_amount=composition.unit_amount,
                        unit_code=composition.unit_code,
                        pack_count=composition.pack_count,
                        units_per_pack=composition.units_per_pack,
                        total_amount=composition.total_amount,
                    ),
                    current_binding=None
                    if binding is None
                    else BindingReadback(
                        binding_id=binding.binding_id,
                        binding_kind=BindingKind(binding.binding_kind),
                        group_member_id=binding.group_member_id,
                        provenance_revision_id=binding.provenance_revision_id,
                        valid_from=binding.valid_from,
                    ),
                )
            )
        return ProductReadback(
            product_group_id=group.product_group_id,
            status=GroupStatus(group.status),
            created_at=group.created_at,
            retired_at=group.retired_at,
            membership_revision_id=None
            if membership is None
            else membership.membership_revision_id,
            membership_revision_no=None if membership is None else membership.revision_no,
            members=tuple(members),
            items=tuple(items),
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


def _move_record(row: CurrentSourceRevisionMove) -> RevisionMove:
    return RevisionMove(
        row.move_id,
        row.source_product_uid,
        row.sequence,
        row.revision_id,
        row.previous_revision_id,
        MoveReason(row.reason),
    )


def _binding_record(row: SourceBinding) -> BindingRecord:
    return BindingRecord(
        row.binding_id,
        row.item_id,
        row.group_member_id,
        BindingKind(row.binding_kind),
        row.provenance_revision_id,
        row.valid_from,
    )
