"""Deterministic materialization of the canonical Product from durable source truth (M4 PR-C).

Issue #80 kickoff 5736827688, under ADR-0013 §3–§6 and ADR-0010::

    durably RECORDED ProductFactsRevision → current source revision
    → canonical ProductGroup and its CONFIRMED member
    → default product-side Item and BASE_PRODUCT binding, when the current revision proves them
    → canonical read-back

This module owns the automatic decisions; :class:`ProductFoundationStore` only persists them. It
makes no supplier or marketplace request, calls no AI and copies no source fact value into the
product database: it reads revision identities, statuses and fingerprints, and writes identifiers.

**Eligibility.** A revision may become current only when its own collection run is ``RECORDED``,
the run names exactly that revision, run and revision agree on the supplier, the source identity
and the ``facts_status``, and the revision's fingerprints recompute from its stored content. A
``REVIEW_REQUIRED`` revision is eligible: fact ambiguity is readiness's to carry (ADR-0013 §3, §8),
never the pointer's. A revision whose run is still ``PENDING``, or ended ``FAILED`` or
``NO_REVISION``, is not yet — or never — source truth, so it is passed over.

**The automatic rule.** For one source identity the target is the newest revision, by sequence,
whose run is ``RECORDED``. It must pass every check above or nothing moves: an older revision is
never chosen in its place. The pointer never moves backwards automatically; a replayed or late run
resolves to the same target, so it changes nothing.

**One decision, one unit of work.** The pointer move, its audit event, a new singleton group with
its CONFIRMED member and membership revision 1, the default composition and Item, and the binding
close/open all commit together or not at all. Anything that must write nothing — a newer current
revision, a retired group — abandons the unit, so even the source identity row is rolled back.
"""

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.collect.facts import FactsStatus
from app.collect.models import CollectionOutcome, CollectionRun, ProductFactsRevision
from app.collect.revisions import ProductFactsRevisionStore
from app.core.correlation import get_correlation_id, new_correlation_id
from app.core.errors import NotFoundError
from app.db.database import Database
from app.products.model import (
    DEFAULT_SINGLE_UNIT,
    DEFAULT_SINGLE_UNIT_SIGNATURE,
    GroupStatus,
    MoveReason,
)
from app.products.store import ProductFoundationStore, ProductFoundationUnit

logger = logging.getLogger("icbm.products")

# The automatic current-source rule this module implements, recorded on every move it makes.
RULE_VERSION = "current-source-rule/v1"
DECIDED_BY = "products.materializer"
# Why a singleton group's first membership revision exists.
MEMBERSHIP_REASON = "MATERIALIZED"


class MaterializationStatus(StrEnum):
    """What one materialization call did.

    ``MATERIALIZED``     at least one canonical row was written, with its audit event.
    ``UNCHANGED``        canonical state already follows the newest eligible revision.
    ``NOT_ELIGIBLE``     there is no durably RECORDED revision to follow yet.
    ``REFUSED``          the newest RECORDED revision failed an integrity check; nothing written.
    ``REVIEW_REQUIRED``  canonical state needs a decision no rule may take; nothing written.
    """

    MATERIALIZED = "MATERIALIZED"
    UNCHANGED = "UNCHANGED"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    REFUSED = "REFUSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class SourceDrift(StrEnum):
    """What the source fingerprints say about a move (ADR-0010 §6, ADR-0013 §3).

    Fingerprints are drift evidence only under the same ``extractor_revision``. Across an
    extractor change they prove neither drift nor its absence, so no classification is made.
    """

    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"
    NOT_COMPARABLE = "NOT_COMPARABLE"


class BaseProductEvidence(StrEnum):
    """Whether the current revision states exactly ``options = ABSENT`` and
    ``quantity_tiers = ABSENT`` (ruling B). Anything else — REVIEW_REQUIRED included — is not
    proof, and no default unit is inferred from it."""

    PROVEN = "PROVEN"
    NOT_PROVEN = "NOT_PROVEN"


# Reason codes: our own, never page content.
NO_RECORDED_REVISION = "PRODUCTS_NO_RECORDED_REVISION"
RUN_NOT_RECORDED = "PRODUCTS_RUN_NOT_RECORDED"
RUN_REVISION_MISMATCH = "PRODUCTS_RUN_REVISION_MISMATCH"
RUN_IDENTITY_MISMATCH = "PRODUCTS_RUN_IDENTITY_MISMATCH"
FACTS_STATUS_MISMATCH = "PRODUCTS_FACTS_STATUS_MISMATCH"
FINGERPRINTS_BROKEN = "PRODUCTS_FINGERPRINTS_BROKEN"
CURRENT_IS_NEWER = "PRODUCTS_CURRENT_IS_NEWER"
GROUP_RETIRED = "PRODUCTS_GROUP_RETIRED"


@dataclass(frozen=True)
class Materialization:
    """The outcome of one call, with the identifiers of everything it wrote."""

    status: MaterializationStatus
    supplier_key: str | None
    source_product_id: str | None
    reason: str | None = None
    source_product_uid: str | None = None
    current_source_revision_id: str | None = None
    move: MoveReason | None = None
    move_id: str | None = None
    previous_revision_id: str | None = None
    source_drift: SourceDrift | None = None
    product_group_id: str | None = None
    group_created: bool = False
    membership_revision_id: str | None = None
    base_product: BaseProductEvidence | None = None
    item_id: str | None = None
    item_created: bool = False
    bindings_closed: tuple[str, ...] = field(default_factory=tuple)
    binding_opened: str | None = None


@dataclass(frozen=True)
class _Target:
    """The newest RECORDED revision of one source identity, checked in full."""

    revision_id: str
    supplier_key: str
    source_product_id: str
    sequence: int
    extractor_revision: str
    source_fingerprint: str
    collection_run_id: str


class _Abandoned(Exception):
    """Leave the unit of work without writing anything: the result says why."""

    def __init__(self, result: Materialization) -> None:
        super().__init__(result.status)
        self.result = result


class ProductMaterializer:
    def __init__(
        self,
        *,
        db: Database,
        store: ProductFoundationStore,
        revisions: ProductFactsRevisionStore,
        audit: AuditLog,
    ) -> None:
        self._db = db
        self._store = store
        self._revisions = revisions
        self._audit = audit

    # ------------------------------------------------------------------ entry points

    def materialize_run(
        self, collection_run_id: str, *, correlation_id: str | None = None
    ) -> Materialization:
        """Materialize the source identity a durably RECORDED run appended a revision for.

        The run only names the identity: the decision is the identity's, so a late or replayed run
        resolves to that identity's newest eligible revision and never moves the pointer back.
        """
        with self._db.read() as session:
            run = session.get(CollectionRun, collection_run_id)
            if run is None:
                raise NotFoundError("COLLECT_RUN_UNKNOWN", "no collection run has that identifier")
            if run.outcome != CollectionOutcome.RECORDED or run.revision_id is None:
                return Materialization(
                    MaterializationStatus.NOT_ELIGIBLE,
                    run.supplier_key,
                    run.source_product_id,
                    reason=RUN_NOT_RECORDED,
                )
            revision = session.get(ProductFactsRevision, run.revision_id)
            if revision is None:
                return self._refusal(run.supplier_key, run.source_product_id, RUN_REVISION_MISMATCH)
            supplier_key, source_product_id = revision.supplier_key, revision.source_product_id
        return self.materialize_source(
            supplier_key, source_product_id, correlation_id=correlation_id
        )

    def materialize_source(
        self, supplier_key: str, source_product_id: str, *, correlation_id: str | None = None
    ) -> Materialization:
        """Make canonical state follow this source identity's newest eligible revision."""
        correlation = correlation_id or get_correlation_id() or new_correlation_id()
        found = self._newest_recorded(supplier_key, source_product_id)
        if found is None:
            return Materialization(
                MaterializationStatus.NOT_ELIGIBLE,
                supplier_key,
                source_product_id,
                reason=NO_RECORDED_REVISION,
            )
        target, problem = found
        if problem is not None:
            return self._refusal(supplier_key, source_product_id, problem, target.revision_id)
        try:
            with self._store.transaction() as unit:
                result = self._apply(unit, target, correlation)
        except _Abandoned as abandoned:
            result = abandoned.result
        logger.info(
            "products.materialized",
            extra={
                "status": result.status.value,
                "reason": result.reason,
                "supplier": supplier_key,
                "source_product_uid": result.source_product_uid,
                "revision_id": result.current_source_revision_id,
                "product_group_id": result.product_group_id,
                "move": None if result.move is None else result.move.value,
            },
        )
        return result

    # ------------------------------------------------------------------ eligibility

    def _newest_recorded(
        self, supplier_key: str, source_product_id: str
    ) -> tuple[_Target, str | None] | None:
        """The newest revision whose own run is RECORDED, and the first check it fails, if any.

        A revision whose run is still PENDING, or ended FAILED or NO_REVISION, is passed over: it
        is not durable source truth. The newest RECORDED one is checked in full, and a failure
        refuses the whole decision rather than falling back to an older revision.
        """
        with self._db.read() as session:
            rows = session.execute(
                select(ProductFactsRevision, CollectionRun)
                .outerjoin(
                    CollectionRun,
                    CollectionRun.collection_run_id == ProductFactsRevision.collection_run_id,
                )
                .where(
                    ProductFactsRevision.supplier_key == supplier_key,
                    ProductFactsRevision.source_product_id == source_product_id,
                )
                .order_by(ProductFactsRevision.sequence.desc())
            ).tuples()
            for revision, run in rows:
                if run is None or run.outcome != CollectionOutcome.RECORDED:
                    continue
                target = _Target(
                    revision_id=revision.revision_id,
                    supplier_key=revision.supplier_key,
                    source_product_id=revision.source_product_id,
                    sequence=revision.sequence,
                    extractor_revision=revision.extractor_revision,
                    source_fingerprint=revision.source_fingerprint,
                    collection_run_id=revision.collection_run_id,
                )
                return target, self._run_problem(revision, run) or self._integrity_problem(target)
        return None

    @staticmethod
    def _run_problem(revision: ProductFactsRevision, run: CollectionRun) -> str | None:
        if run.revision_id != revision.revision_id:
            return RUN_REVISION_MISMATCH
        if (run.supplier_key, run.source_product_id) != (
            revision.supplier_key,
            revision.source_product_id,
        ):
            # A run that never recorded which product the source identified cannot agree.
            return RUN_IDENTITY_MISMATCH
        if run.facts_status is None or FactsStatus(run.facts_status) != FactsStatus(
            revision.facts_status
        ):
            return FACTS_STATUS_MISMATCH
        return None

    def _integrity_problem(self, target: _Target) -> str | None:
        """Whether the revision's fingerprints recompute from its stored content alone. A
        structure that cannot even be read back is as broken as one that does not recompute."""
        try:
            stored = self._revisions.get(target.revision_id)
            intact = stored is not None and stored.fingerprints_intact()
        except (ValueError, KeyError, TypeError):
            intact = False
        return None if intact else FINGERPRINTS_BROKEN

    def _refusal(
        self,
        supplier_key: str | None,
        source_product_id: str | None,
        reason: str,
        revision_id: str | None = None,
    ) -> Materialization:
        logger.warning(
            "products.materialization_refused",
            extra={"supplier": supplier_key, "reason": reason, "revision_id": revision_id},
        )
        return Materialization(
            MaterializationStatus.REFUSED, supplier_key, source_product_id, reason=reason
        )

    # ------------------------------------------------------------------ the decision

    def _apply(
        self, unit: ProductFoundationUnit, target: _Target, correlation: str
    ) -> Materialization:
        source = unit.source_product(target.supplier_key, target.source_product_id)
        uid = source.source_product_uid
        current = unit.current_move(uid)
        membership = unit.confirmed_membership(uid)

        # Checks that must write nothing come first; abandoning also rolls back the identity row.
        if current is not None and current.revision_id != target.revision_id:
            current_row = unit.session.get(ProductFactsRevision, current.revision_id)
            assert current_row is not None  # a foreign key guarantees it
            if current_row.sequence > target.sequence:
                raise _Abandoned(
                    Materialization(
                        MaterializationStatus.UNCHANGED,
                        target.supplier_key,
                        target.source_product_id,
                        reason=CURRENT_IS_NEWER,
                        source_product_uid=uid,
                        current_source_revision_id=current.revision_id,
                    )
                )
        if membership is not None and membership.group_status is GroupStatus.RETIRED:
            # No successor is ever guessed (ADR-0013 §1, Canonical v3.1 §6.5).
            raise _Abandoned(
                Materialization(
                    MaterializationStatus.REVIEW_REQUIRED,
                    target.supplier_key,
                    target.source_product_id,
                    reason=GROUP_RETIRED,
                    source_product_uid=uid,
                    product_group_id=membership.product_group_id,
                )
            )

        # 1. The current source revision.
        move = None
        drift = None
        if current is None:
            move = unit.record_move(
                uid,
                target.revision_id,
                reason=MoveReason.INITIAL,
                decided_by=DECIDED_BY,
                correlation_id=correlation,
                rule_version=RULE_VERSION,
            )
        elif current.revision_id != target.revision_id:
            previous = unit.session.get(ProductFactsRevision, current.revision_id)
            assert previous is not None
            if previous.extractor_revision == target.extractor_revision:
                reason = MoveReason.NEWER_REVISION
                drift = (
                    SourceDrift.UNCHANGED
                    if previous.source_fingerprint == target.source_fingerprint
                    else SourceDrift.CHANGED
                )
            else:
                reason = MoveReason.EXTRACTOR_CHANGED
                drift = SourceDrift.NOT_COMPARABLE
            move = unit.record_move(
                uid,
                target.revision_id,
                reason=reason,
                decided_by=DECIDED_BY,
                correlation_id=correlation,
                rule_version=RULE_VERSION,
            )
        if move is not None:
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.PRODUCT_CURRENT_SOURCE_REVISION_MOVED,
                    action="current_source_revision.move",
                    actor=DECIDED_BY,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=uid,
                    reason_code=move.reason.value,
                    before=None
                    if move.previous_revision_id is None
                    else {"revision_id": move.previous_revision_id},
                    after={"revision_id": move.revision_id, "sequence": move.sequence},
                    details={
                        "move_id": move.move_id,
                        "rule_version": RULE_VERSION,
                        "supplier_key": target.supplier_key,
                        "collection_run_id": target.collection_run_id,
                        "source_drift": None if drift is None else drift.value,
                    },
                    correlation_id=correlation,
                ),
                session=unit.session,
            )

        # 2. The canonical group and its CONFIRMED member.
        group_created = False
        membership_revision_id = None
        if membership is None:
            group_id = unit.create_group(decided_by=DECIDED_BY)
            change = unit.confirm_new_member(
                group_id,
                uid,
                reason=MEMBERSHIP_REASON,
                decided_by=DECIDED_BY,
                correlation_id=correlation,
            )
            member_id = change.member_id
            assert change.revision is not None
            membership_revision_id = change.revision.membership_revision_id
            group_created = True
        else:
            group_id, member_id = membership.product_group_id, membership.member_id

        # 3. Bindings follow the current revision: none may claim an obsolete provenance.
        closed = []
        for binding in unit.open_bindings_of_member(member_id):
            if binding.provenance_revision_id != target.revision_id:
                unit.close_binding(binding.binding_id)
                closed.append(binding.binding_id)

        # 4. The default Item and its BASE_PRODUCT binding, only when proven (ruling B).
        proven = unit.states_no_options_and_no_tiers(target.revision_id)
        evidence = BaseProductEvidence.PROVEN if proven else BaseProductEvidence.NOT_PROVEN
        item = unit.find_item(group_id, DEFAULT_SINGLE_UNIT_SIGNATURE)
        item_created = False
        opened = None
        if proven:
            if item is None:
                composition = unit.composition(DEFAULT_SINGLE_UNIT)
                item = unit.item(group_id, composition.composition_id)
                item_created = True
            held = unit.open_binding_of_item(item.item_id)
            # Another member's binding is left alone: choosing between members is not this
            # rule's decision (ADR-0013 §6).
            if held is None:
                opened = unit.bind_base_product(
                    item.item_id,
                    member_id,
                    target.revision_id,
                    decided_by=DECIDED_BY,
                    correlation_id=correlation,
                ).binding_id

        changed = group_created or item_created or bool(closed) or opened is not None
        if changed:
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.PRODUCT_MATERIALIZED,
                    action="product.materialize",
                    actor=DECIDED_BY,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=group_id,
                    reason_code=evidence.value,
                    details={
                        "rule_version": RULE_VERSION,
                        "source_product_uid": uid,
                        "current_source_revision_id": target.revision_id,
                        "group_created": group_created,
                        "member_id": member_id,
                        "membership_revision_id": membership_revision_id,
                        "item_id": None if item is None else item.item_id,
                        "item_created": item_created,
                        "bindings_closed": closed,
                        "binding_opened": opened,
                    },
                    correlation_id=correlation,
                ),
                session=unit.session,
            )

        return Materialization(
            MaterializationStatus.MATERIALIZED
            if move is not None or changed
            else MaterializationStatus.UNCHANGED,
            target.supplier_key,
            target.source_product_id,
            source_product_uid=uid,
            current_source_revision_id=target.revision_id,
            move=None if move is None else move.reason,
            move_id=None if move is None else move.move_id,
            previous_revision_id=None if move is None else move.previous_revision_id,
            source_drift=drift,
            product_group_id=group_id,
            group_created=group_created,
            membership_revision_id=membership_revision_id,
            base_product=evidence,
            item_id=None if item is None else item.item_id,
            item_created=item_created,
            bindings_closed=tuple(closed),
            binding_opened=opened,
        )
