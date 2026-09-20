"""REGISTER persistence for the M5 foundation (Issue #89 PR-B, ADR-0014).

This store writes and reads the registration rows and nothing more. It never decides:
- a preflight or its fingerprint (PR-C): the caller names them when it freezes a Snapshot;
- a payload, a category or an outbound value (PR-C): the caller gives the sanitized values;
- a marketplace request, a read-back or a comparison (PR-D, PR-E): the caller reports their
  sanitized results and the outcome it proved.

Every cross-row invariant is enforced by the database (migration 0016), so this store is not the
only guard. It adds the domain side:
- **the canonical account scope**: a Draft, Snapshot, Batch, Intent or override opens only for a
  ``marketplace_account_id`` bound to its committed provider identity (``ACCOUNT_IDENTITY.md``;
  review 5255746944, blocker 2), never for a free account string;
- **the Draft price pin**: each Draft Item names the exact M4 PricingSnapshot of that Item in the
  Draft's target context, a new selection is a new Draft revision, and a Snapshot freezes that
  pinned price with the membership and facts revisions it was computed from, never a price the
  caller hands in (§2, blocker 1);
- **the provider-listing unit**: a ``SINGLE_LISTING_WITH_OPTIONS`` Snapshot sends exactly every
  open Item of its Draft revision, ``SEPARATE_LISTINGS`` one Item, ``SELECTED_OFFERS`` a chosen
  subset; an Intent opens only while its Snapshot is still its Draft's current revision (R3,
  blocker 3);
- the deterministic identities: each ``registration_item_key`` from the listing identity and the
  Item key, and each Intent's idempotency key (§7, §8);
- **every durable digest from a sanitized canonical representation** it is handed, never from wire
  bytes (§15, B4): it has no way to receive wire bytes at all;
- **the conflict scope of an unresolved CREATE, widened by the merge and split lineage of its
  groups** (§10, R2): the database compares the groups themselves, and this store also refuses a
  group that a MERGE or SPLIT connects to one of them, fail-closed;
- one audit event per change, in the same unit of work;
- a clear refusal before a write the database would reject anyway.

It is the only production writer of the registration tables (a repository rule keeps it so).
"""

import json
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.accounts import require_bound
from app.connect.marketplace.capability import RemoteOutcome
from app.core.clock import Clock
from app.core.errors import ErrorClass, InputValidationError, NotFoundError
from app.db.database import Database
from app.jobs.models import Job
from app.products.models import GroupChangeEvent, PricingSnapshot, ProductItem
from app.products.pricing_store import PricingSnapshotRecord, PricingUnit
from app.register.model import (
    BLOCKING_STATES,
    AbsenceEvidence,
    BatchSummary,
    ExecutionScopeState,
    IntentState,
    ListingShape,
    Operation,
    RegistrationConflictError,
    RegistrationLifecycle,
    ResolutionEvidence,
    ResolvedBy,
    ScopePauseReason,
    VerificationState,
    effective_outcome,
    idempotency_key,
    pause_class_allowed,
    registration_item_key,
    sanitized_digest,
    summarize,
    uncovered_single_listing,
    valid_listing_identity,
)
from app.register.models import (
    DuplicateOverride,
    MarketplaceRegistration,
    MarketplaceRegistrationItem,
    RegistrationAttempt,
    RegistrationBatch,
    RegistrationDraft,
    RegistrationDraftItem,
    RegistrationExecutionScope,
    RegistrationIntent,
    RegistrationItemSnapshot,
    RegistrationSnapshot,
)
from app.register.sanitize import problems

# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class DraftItemRecord:
    draft_item_id: str
    item_id: str
    product_group_id: str
    composition_signature: str
    pricing_snapshot_id: str
    ordinal: int


@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    marketplace_key: str
    marketplace_account_id: str
    listing_shape: ListingShape
    draft_revision: int
    items: tuple[DraftItemRecord, ...]


@dataclass(frozen=True)
class ItemSnapshotSpec:
    """What the caller (PR-C) built for one Item of the Draft. Every mapping is already sanitized.

    The price, and the membership and facts revisions it was computed from, are not the caller's
    to give: the Snapshot freezes the Draft Item's pinned PricingSnapshot (§2, blocker 1).
    """

    item_id: str
    source_snapshot: Mapping[str, Any]
    publication_assets: Sequence[Mapping[str, Any]]
    outbound_values: Mapping[str, Any]


@dataclass(frozen=True)
class SnapshotSpec:
    """One provider-listing unit as the caller (PR-C) froze it (§6)."""

    draft_id: str
    draft_revision: int
    listing_identity: str
    preflight_rule_version: str
    preflight_fingerprint: str
    category_mapping_revision: str
    taxonomy_revision: str
    policy_revisions: Mapping[str, str]
    detail_composition_revision: str
    sanitizer_profile_version: str
    payload: Mapping[str, Any]
    items: Sequence[ItemSnapshotSpec]


@dataclass(frozen=True)
class ItemSnapshotRecord:
    item_snapshot_id: str
    registration_item_key: str
    ordinal: int
    item_id: str
    group_id_at_registration: str
    group_membership_revision_id: str
    listing_composition_id: str
    composition_signature: str
    source_product_facts_revision_id: str
    pricing_snapshot_id: str


@dataclass(frozen=True)
class SnapshotRecord:
    registration_snapshot_id: str
    draft_id: str
    draft_revision: int
    marketplace_key: str
    marketplace_account_id: str
    listing_shape: ListingShape
    listing_identity: str
    preflight_fingerprint: str
    payload_hash: str
    items: tuple[ItemSnapshotRecord, ...]


@dataclass(frozen=True)
class IntentRecord:
    intent_id: str
    registration_batch_id: str
    registration_snapshot_id: str
    marketplace_key: str
    marketplace_account_id: str
    operation: Operation
    idempotency_key: str
    state: IntentState
    remote_outcome: RemoteOutcome | None
    marketplace_product_id: str | None
    verification_state: VerificationState


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    intent_id: str
    attempt_no: int
    request_payload_hash: str
    finished: bool
    remote_outcome: RemoteOutcome | None
    resolved_outcome: RemoteOutcome | None
    resolved_by: ResolvedBy | None
    resolution_evidence_kind: ResolutionEvidence | None
    # The cause the attempt finished with, independent of whether the mutation happened (§9).
    error_class: ErrorClass | None = None
    error_code: str | None = None
    # When the attempt opened: what windows an execution policy against a scope reset (PR-E).
    started_at: datetime | None = None

    @property
    def outcome(self) -> RemoteOutcome | None:
        """The outcome now: its evidence-backed resolution, else what the attempt finished with."""
        return effective_outcome(self.remote_outcome, self.resolved_outcome)

    @property
    def ambiguous_result(self) -> bool:
        """``RegistrationAttempt.ambiguous_result`` (Canonical v3.1 §10.3): a projection of the
        outcome, never written independently (`ERRORS.md` §3)."""
        return self.outcome is RemoteOutcome.UNKNOWN


@dataclass(frozen=True)
class RegistrationItemRecord:
    registration_item_id: str
    registration_item_key: str
    item_snapshot_id: str
    current_group_id: str
    listing_composition_id: str
    current_source_binding_id: str | None
    marketplace_option_id: str | None


@dataclass(frozen=True)
class RegistrationRecord:
    registration_id: str
    intent_id: str
    registration_snapshot_id: str
    marketplace_key: str
    marketplace_account_id: str
    marketplace_product_id: str
    seller_product_code: str
    published_state: str
    lifecycle_state: RegistrationLifecycle
    items: tuple[RegistrationItemRecord, ...]


@dataclass(frozen=True)
class ConflictRecord:
    """A CREATE Intent in a unit's R2 conflict scope, and its state (``SENT`` or ``UNKNOWN``)."""

    intent_id: str
    state: IntentState


@dataclass(frozen=True)
class OverrideRecord:
    override_id: str
    marketplace_key: str
    marketplace_account_id: str
    product_group_id: str
    listing_composition_id: str | None
    active: bool


@dataclass(frozen=True)
class ScopeRecord:
    """The REGISTER send brake of one execution scope (§26), as the caller reads it.

    An absent row is an ACTIVE scope that has never been paused, so the reader never has to know
    whether a row exists: :meth:`active` is that scope.
    """

    marketplace_key: str
    marketplace_account_id: str
    endpoint_group: str
    state: ExecutionScopeState
    pause_reason: ScopePauseReason | None = None
    pause_error_class: ErrorClass | None = None
    paused_at: datetime | None = None
    pause_policy_version: str | None = None
    resume_generation: int = 0
    resumed_at: datetime | None = None
    resumed_by: str | None = None
    resume_reason: str | None = None

    @classmethod
    def active(
        cls, marketplace_key: str, marketplace_account_id: str, endpoint_group: str
    ) -> "ScopeRecord":
        return cls(
            marketplace_key=marketplace_key,
            marketplace_account_id=marketplace_account_id,
            endpoint_group=endpoint_group,
            state=ExecutionScopeState.ACTIVE,
        )

    @property
    def paused(self) -> bool:
        return self.state is ExecutionScopeState.PAUSED

    def canonical(self) -> dict[str, Any]:
        """The scope's safe evidence shape: identities, enums, versions and its boundary."""
        return {
            "marketplace_key": self.marketplace_key,
            "marketplace_account_id": self.marketplace_account_id,
            "endpoint_group": self.endpoint_group,
            "state": self.state.value,
            "pause_reason": None if self.pause_reason is None else self.pause_reason.value,
            "pause_error_class": (
                None if self.pause_error_class is None else self.pause_error_class.value
            ),
            "paused_at": None if self.paused_at is None else self.paused_at.isoformat(),
            "pause_policy_version": self.pause_policy_version,
            "resume_generation": self.resume_generation,
            "resumed_at": None if self.resumed_at is None else self.resumed_at.isoformat(),
            "resumed_by": self.resumed_by,
            "resume_reason": self.resume_reason,
        }


# ---------------------------------------------------------------- the store


class RegistrationStore:
    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    @contextmanager
    def transaction(self) -> Iterator["RegistrationUnit"]:
        """One unit of work across several registration writes: all commit together, or none."""
        with self._db.write() as session:
            yield RegistrationUnit(session, self._clock, self._audit)

    @contextmanager
    def reading(self) -> Iterator["RegistrationUnit"]:
        with self._db.read() as session:
            yield RegistrationUnit(session, self._clock, self._audit)

    def draft(self, draft_id: str) -> DraftRecord | None:
        with self.reading() as unit:
            return unit.draft(draft_id)

    def snapshot(self, registration_snapshot_id: str) -> SnapshotRecord | None:
        with self.reading() as unit:
            return unit.snapshot(registration_snapshot_id)

    def intent(self, intent_id: str) -> IntentRecord | None:
        with self.reading() as unit:
            return unit.intent(intent_id)

    def attempts(self, intent_id: str) -> tuple[AttemptRecord, ...]:
        with self.reading() as unit:
            return unit.attempts(intent_id)

    def registration(self, registration_id: str) -> RegistrationRecord | None:
        with self.reading() as unit:
            return unit.registration(registration_id)

    def batch_summary(self, registration_batch_id: str) -> BatchSummary:
        with self.reading() as unit:
            return unit.batch_summary(registration_batch_id)

    def conflicting_intents(self, registration_snapshot_id: str) -> tuple[str, ...]:
        with self.reading() as unit:
            return unit.conflicting_intents(registration_snapshot_id)

    def snapshot_payload(self, registration_snapshot_id: str) -> Mapping[str, Any] | None:
        with self.reading() as unit:
            return unit.snapshot_payload(registration_snapshot_id)

    def scope_attempts(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        *,
        operation: Operation = Operation.CREATE,
        limit: int = 50,
    ) -> tuple[AttemptRecord, ...]:
        with self.reading() as unit:
            return unit.scope_attempts(
                marketplace_key, marketplace_account_id, operation=operation, limit=limit
            )

    def jobs_with_open_attempts(
        self, job_type: str, terminal_states: Sequence[str], *, limit: int = 500
    ) -> tuple[str, ...]:
        with self.reading() as unit:
            return unit.jobs_with_open_attempts(job_type, terminal_states, limit=limit)

    def active_job(self, job_type: str, intent_id: str, states: Sequence[str]) -> str | None:
        with self.reading() as unit:
            return unit.active_job(job_type, intent_id, states)

    def execution_scope(
        self, marketplace_key: str, marketplace_account_id: str, endpoint_group: str
    ) -> ScopeRecord:
        with self.reading() as unit:
            return unit.execution_scope(marketplace_key, marketplace_account_id, endpoint_group)


class RegistrationUnit:
    """The registration writes and reads over one caller-owned session. It never commits: the
    session's owner does, once, for everything done here."""

    def __init__(self, session: Session, clock: Clock, audit: AuditLog) -> None:
        self.session = session
        self._clock = clock
        self._audit = audit

    # ------------------------------------------------------------------ drafts (§2)

    def create_draft(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        listing_shape: ListingShape,
        *,
        created_by: str,
        correlation_id: str,
    ) -> DraftRecord:
        _require_text(created_by=created_by)
        require_bound(self.session, marketplace_key, marketplace_account_id)
        now = self._clock.now()
        row = RegistrationDraft(
            draft_id=str(uuid.uuid4()),
            marketplace_key=marketplace_key,
            marketplace_account_id=marketplace_account_id,
            listing_shape=ListingShape(listing_shape).value,
            draft_revision=1,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self.session.flush()
        self._event(
            AuditEventType.REGISTRATION_DRAFT_RECORDED,
            "create_draft",
            created_by,
            correlation_id,
            row.draft_id,
            {
                "marketplace_account_id": row.marketplace_account_id,
                "listing_shape": row.listing_shape,
                "draft_revision": 1,
            },
        )
        return self._draft_record(row)

    def add_draft_item(
        self,
        draft_id: str,
        item_id: str,
        pricing_snapshot_id: str,
        *,
        added_by: str,
        correlation_id: str,
    ) -> DraftRecord:
        """Add an existing M4 Item, pinned to its exact PricingSnapshot for this Draft's
        marketplace and canonical account (§2). Choosing that price is the caller's (PR-C); this
        store only refuses a price of another Item or another target context."""
        draft = self._draft_row(draft_id)
        item = self.session.get(ProductItem, item_id)
        if item is None:
            raise NotFoundError("REGISTER_ITEM_NOT_FOUND", "the draft names no existing M4 Item")
        self._require_price(draft, item_id, pricing_snapshot_id)
        open_items = self._open_items(draft_id)
        if any(
            row.item_id == item_id
            or (
                row.product_group_id == item.product_group_id
                and row.composition_signature == item.composition_signature
            )
            for row in open_items
        ):
            raise InputValidationError(
                "REGISTER_DRAFT_ITEM_DUPLICATE",
                "a draft holds each group + composition signature once",
            )
        now = self._clock.now()
        row = RegistrationDraftItem(
            draft_item_id=str(uuid.uuid4()),
            draft_id=draft_id,
            item_id=item_id,
            product_group_id=item.product_group_id,
            composition_signature=item.composition_signature,
            pricing_snapshot_id=pricing_snapshot_id,
            ordinal=max((r.ordinal for r in open_items), default=-1) + 1,
            added_by=added_by,
            added_at=now,
        )
        self.session.add(row)
        self.session.flush()
        return self._advance(draft, added_by, correlation_id, "add_draft_item", item_id)

    def change_draft_item_price(
        self,
        draft_id: str,
        item_id: str,
        pricing_snapshot_id: str,
        *,
        changed_by: str,
        correlation_id: str,
    ) -> DraftRecord:
        """Pin an open Item to another exact PricingSnapshot of the same Item and target (§2).

        The open row is closed and a new one opens in the same position with the new price, and
        the Draft advances one revision: a Snapshot built on the previous revision is stale, and
        the Draft history keeps every price it held. Selecting the pinned price again changes
        nothing.
        """
        draft = self._draft_row(draft_id)
        current = next((r for r in self._open_items(draft_id) if r.item_id == item_id), None)
        if current is None:
            raise NotFoundError(
                "REGISTER_DRAFT_ITEM_NOT_FOUND", "the item is not open in the draft"
            )
        if current.pricing_snapshot_id == pricing_snapshot_id:
            return self._draft_record(draft)
        self._require_price(draft, item_id, pricing_snapshot_id)
        now = self._clock.now()
        current.removed_by = changed_by
        current.removed_at = now
        self.session.flush()
        self.session.add(
            RegistrationDraftItem(
                draft_item_id=str(uuid.uuid4()),
                draft_id=draft_id,
                item_id=item_id,
                product_group_id=current.product_group_id,
                composition_signature=current.composition_signature,
                pricing_snapshot_id=pricing_snapshot_id,
                ordinal=current.ordinal,
                added_by=changed_by,
                added_at=now,
            )
        )
        self.session.flush()
        return self._advance(draft, changed_by, correlation_id, "change_draft_item_price", item_id)

    def remove_draft_item(
        self, draft_id: str, item_id: str, *, removed_by: str, correlation_id: str
    ) -> DraftRecord:
        draft = self._draft_row(draft_id)
        row = next((r for r in self._open_items(draft_id) if r.item_id == item_id), None)
        if row is None:
            raise NotFoundError(
                "REGISTER_DRAFT_ITEM_NOT_FOUND", "the item is not open in the draft"
            )
        row.removed_by = removed_by
        row.removed_at = self._clock.now()
        self.session.flush()
        return self._advance(draft, removed_by, correlation_id, "remove_draft_item", item_id)

    def change_listing_shape(
        self, draft_id: str, listing_shape: ListingShape, *, changed_by: str, correlation_id: str
    ) -> DraftRecord:
        draft = self._draft_row(draft_id)
        draft.listing_shape = ListingShape(listing_shape).value
        return self._advance(draft, changed_by, correlation_id, "change_listing_shape", None)

    def draft(self, draft_id: str) -> DraftRecord | None:
        row = self.session.get(RegistrationDraft, draft_id)
        return None if row is None else self._draft_record(row)

    # ------------------------------------------------------------------ snapshots (§6, §7)

    def freeze_snapshot(
        self, spec: SnapshotSpec, *, created_by: str, correlation_id: str
    ) -> SnapshotRecord:
        """Freeze one provider-listing unit exactly as the caller built it. The caller has already
        evaluated a READY preflight for these dependencies (PR-C); this store records the
        fingerprint it names and never re-decides it.

        Each Item is frozen with the Draft's pinned PricingSnapshot and the membership and facts
        revisions that price was computed from. A ``SINGLE_LISTING_WITH_OPTIONS`` unit sends every
        open Item of the Draft and nothing else (R3); a ``SEPARATE_LISTINGS`` unit sends one Item;
        a ``SELECTED_OFFERS`` unit sends the Items the caller selected.
        """
        draft = self._draft_row(spec.draft_id)
        require_bound(self.session, draft.marketplace_key, draft.marketplace_account_id)
        if spec.draft_revision != draft.draft_revision:
            raise RegistrationConflictError(
                "REGISTER_DRAFT_STALE", "a Snapshot freezes only the current Draft revision"
            )
        if not valid_listing_identity(spec.listing_identity):
            raise InputValidationError(
                "REGISTER_LISTING_IDENTITY_INVALID", "the listing identity is not a seller code"
            )
        if not spec.items:
            raise InputValidationError("REGISTER_SNAPSHOT_EMPTY", "a Snapshot sends an Item")
        if draft.listing_shape == ListingShape.SEPARATE_LISTINGS and len(spec.items) != 1:
            raise InputValidationError(
                "REGISTER_SEPARATE_LISTING_UNIT",
                "a separate listing is one provider-listing unit with exactly one Item",
            )
        pins = {row.item_id: row.pricing_snapshot_id for row in self._open_items(spec.draft_id)}
        sent = [i.item_id for i in spec.items]
        if len(set(sent)) != len(sent) or not set(sent) <= set(pins):
            raise InputValidationError(
                "REGISTER_SNAPSHOT_ITEMS", "a Snapshot sends distinct open items of its Draft"
            )
        if draft.listing_shape == ListingShape.SINGLE_LISTING_WITH_OPTIONS and (
            uncovered_single_listing(pins, {i: pins[i] for i in sent})
        ):
            raise InputValidationError(
                "REGISTER_SINGLE_LISTING_COVERAGE",
                "a single listing with options sends every open Item of its Draft revision",
            )
        snapshot = RegistrationSnapshot(
            registration_snapshot_id=str(uuid.uuid4()),
            draft_id=draft.draft_id,
            draft_revision=draft.draft_revision,
            marketplace_key=draft.marketplace_key,
            marketplace_account_id=draft.marketplace_account_id,
            listing_shape=draft.listing_shape,
            listing_identity=spec.listing_identity,
            preflight_rule_version=spec.preflight_rule_version,
            preflight_fingerprint=spec.preflight_fingerprint,
            category_mapping_revision=spec.category_mapping_revision,
            taxonomy_revision=spec.taxonomy_revision,
            policy_revisions_json=_json(dict(spec.policy_revisions)),
            detail_composition_revision=spec.detail_composition_revision,
            sanitizer_profile_version=spec.sanitizer_profile_version,
            payload_hash=sanitized_digest(spec.payload),
            payload_json=_json(dict(spec.payload)),
            created_by=created_by,
            correlation_id=correlation_id,
            created_at=self._clock.now(),
        )
        self.session.add(snapshot)
        self.session.flush()
        for ordinal, item_spec in enumerate(spec.items):
            item = self.session.get(ProductItem, item_spec.item_id)
            price = self.session.get(PricingSnapshot, pins[item_spec.item_id])
            # An open draft item names an existing Item and an existing price (foreign keys).
            assert item is not None and price is not None
            self.session.add(
                RegistrationItemSnapshot(
                    item_snapshot_id=str(uuid.uuid4()),
                    registration_snapshot_id=snapshot.registration_snapshot_id,
                    registration_item_key=registration_item_key(
                        spec.listing_identity, item.product_group_id, item.composition_signature
                    ),
                    ordinal=ordinal,
                    item_id=item.item_id,
                    group_id_at_registration=item.product_group_id,
                    group_membership_revision_id=price.membership_revision_id,
                    listing_composition_id=item.composition_id,
                    composition_signature=item.composition_signature,
                    source_product_facts_revision_id_at_registration=(
                        price.source_product_facts_revision_id
                    ),
                    pricing_snapshot_id_at_registration=price.pricing_snapshot_id,
                    source_snapshot_json=_json(dict(item_spec.source_snapshot)),
                    publication_assets_json=_json([dict(a) for a in item_spec.publication_assets]),
                    outbound_values_json=_json(dict(item_spec.outbound_values)),
                )
            )
            self.session.flush()
        record = self.snapshot(snapshot.registration_snapshot_id)
        assert record is not None
        self._event(
            AuditEventType.REGISTRATION_SNAPSHOT_FROZEN,
            "freeze_snapshot",
            created_by,
            correlation_id,
            record.registration_snapshot_id,
            {
                "draft_id": record.draft_id,
                "draft_revision": record.draft_revision,
                "listing_shape": record.listing_shape.value,
                "payload_hash": record.payload_hash,
                "preflight_fingerprint": record.preflight_fingerprint,
                "registration_item_keys": [i.registration_item_key for i in record.items],
            },
        )
        return record

    def snapshot(self, registration_snapshot_id: str) -> SnapshotRecord | None:
        row = self.session.get(RegistrationSnapshot, registration_snapshot_id)
        if row is None:
            return None
        items = self.session.scalars(
            select(RegistrationItemSnapshot)
            .where(RegistrationItemSnapshot.registration_snapshot_id == registration_snapshot_id)
            .order_by(RegistrationItemSnapshot.ordinal)
        ).all()
        return SnapshotRecord(
            registration_snapshot_id=row.registration_snapshot_id,
            draft_id=row.draft_id,
            draft_revision=row.draft_revision,
            marketplace_key=row.marketplace_key,
            marketplace_account_id=row.marketplace_account_id,
            listing_shape=ListingShape(row.listing_shape),
            listing_identity=row.listing_identity,
            preflight_fingerprint=row.preflight_fingerprint,
            payload_hash=row.payload_hash,
            items=tuple(
                ItemSnapshotRecord(
                    item_snapshot_id=i.item_snapshot_id,
                    registration_item_key=i.registration_item_key,
                    ordinal=i.ordinal,
                    item_id=i.item_id,
                    group_id_at_registration=i.group_id_at_registration,
                    group_membership_revision_id=i.group_membership_revision_id,
                    listing_composition_id=i.listing_composition_id,
                    composition_signature=i.composition_signature,
                    source_product_facts_revision_id=(
                        i.source_product_facts_revision_id_at_registration
                    ),
                    pricing_snapshot_id=i.pricing_snapshot_id_at_registration,
                )
                for i in items
            ),
        )

    def snapshot_payload(self, registration_snapshot_id: str) -> Mapping[str, Any] | None:
        """The frozen canonical payload of one Snapshot — what was actually sent (§6).

        It is read-only truth: the wire projection and the read-back comparison both expect this
        exact document, never the current Draft.
        """
        row = self.session.get(RegistrationSnapshot, registration_snapshot_id)
        if row is None:
            return None
        payload = json.loads(row.payload_json)
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------ batches and intents (§8)

    def create_batch(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        *,
        created_by: str,
        correlation_id: str,
    ) -> str:
        _require_text(created_by=created_by)
        require_bound(self.session, marketplace_key, marketplace_account_id)
        row = RegistrationBatch(
            registration_batch_id=str(uuid.uuid4()),
            marketplace_key=marketplace_key,
            marketplace_account_id=marketplace_account_id,
            created_by=created_by,
            correlation_id=correlation_id,
            created_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        return row.registration_batch_id

    def create_intent(
        self,
        registration_batch_id: str,
        registration_snapshot_id: str,
        *,
        created_by: str,
        correlation_id: str,
    ) -> IntentRecord:
        """The one CREATE Intent of an exact Snapshot. Asking again returns the same Intent: a
        retry, a restart or a concurrent request never opens a second one (§8)."""
        existing = self.session.scalar(
            select(RegistrationIntent).where(
                RegistrationIntent.registration_snapshot_id == registration_snapshot_id,
                RegistrationIntent.operation == Operation.CREATE.value,
            )
        )
        if existing is not None:
            return _intent_record(existing)
        snapshot = self.session.get(RegistrationSnapshot, registration_snapshot_id)
        batch = self.session.get(RegistrationBatch, registration_batch_id)
        if snapshot is None or batch is None:
            raise NotFoundError("REGISTER_NOT_FOUND", "the Snapshot or the batch does not exist")
        if (batch.marketplace_key, batch.marketplace_account_id) != (
            snapshot.marketplace_key,
            snapshot.marketplace_account_id,
        ):
            raise InputValidationError(
                "REGISTER_SCOPE_MISMATCH", "a batch and its Snapshots share one marketplace account"
            )
        require_bound(self.session, snapshot.marketplace_key, snapshot.marketplace_account_id)
        self._require_current_unit(snapshot)
        blocking = self.conflicting_intents(registration_snapshot_id)
        if blocking:
            raise RegistrationConflictError(
                "REGISTER_UNRESOLVED_CONFLICT",
                "an unresolved CREATE overlaps this conflict scope; reconcile it first",
                details={"intent_ids": list(blocking)},
            )
        now = self._clock.now()
        row = RegistrationIntent(
            intent_id=str(uuid.uuid4()),
            registration_batch_id=registration_batch_id,
            registration_snapshot_id=registration_snapshot_id,
            marketplace_key=snapshot.marketplace_key,
            marketplace_account_id=snapshot.marketplace_account_id,
            operation=Operation.CREATE.value,
            idempotency_key=idempotency_key(
                snapshot.marketplace_key,
                snapshot.marketplace_account_id,
                Operation.CREATE,
                registration_snapshot_id,
            ),
            state=IntentState.PREPARED.value,
            verification_state=VerificationState.NOT_VERIFIED.value,
            created_by=created_by,
            correlation_id=correlation_id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        self.session.flush()
        self._event(
            AuditEventType.REGISTRATION_INTENT_RECORDED,
            "create_intent",
            created_by,
            correlation_id,
            row.intent_id,
            {
                "registration_snapshot_id": registration_snapshot_id,
                "registration_batch_id": registration_batch_id,
                "operation": row.operation,
                "idempotency_key": row.idempotency_key,
                "state": row.state,
            },
        )
        return _intent_record(row)

    def conflicting_intents(self, registration_snapshot_id: str) -> tuple[str, ...]:
        """The unresolved CREATE Intents whose conflict scope overlaps this Snapshot (§10, R2).

        Overlap is the same marketplace and canonical account, and either the same listing
        identity or a shared group, where each side's groups are widened by the MERGE and SPLIT
        lineage that connects them. An unclear successor therefore counts as overlapping
        (fail-closed). One real account has one canonical id, so no label escapes the scope.
        """
        snapshot = self.session.get(RegistrationSnapshot, registration_snapshot_id)
        if snapshot is None:
            raise NotFoundError("REGISTER_NOT_FOUND", "the Snapshot does not exist")
        return tuple(
            conflict.intent_id
            for conflict in self.conflicts_for(
                snapshot.marketplace_key,
                snapshot.marketplace_account_id,
                self._groups_of(registration_snapshot_id),
                snapshot.listing_identity,
                exclude_snapshot_id=registration_snapshot_id,
            )
        )

    def conflicts_for(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        groups: Iterable[str],
        listing_identity: str,
        *,
        exclude_snapshot_id: str | None = None,
    ) -> tuple["ConflictRecord", ...]:
        """The R2 conflict scope of a provider-listing unit, prospective or frozen: every CREATE
        Intent that is ``SENT`` or ``UNKNOWN`` in this marketplace and canonical account and
        shares the listing identity or a group, widened by lineage. The M5 PR-C preflight reads
        it before any Snapshot exists; the database trigger remains the durable barrier."""
        mine = self._lineage(frozenset(groups))
        candidates = self.session.scalars(
            select(RegistrationIntent).where(
                RegistrationIntent.operation == Operation.CREATE.value,
                RegistrationIntent.marketplace_key == marketplace_key,
                RegistrationIntent.marketplace_account_id == marketplace_account_id,
                RegistrationIntent.state.in_([s.value for s in BLOCKING_STATES]),
            )
        ).all()
        blocking = []
        for intent in candidates:
            if intent.registration_snapshot_id == exclude_snapshot_id:
                continue
            other = self.session.get(RegistrationSnapshot, intent.registration_snapshot_id)
            assert other is not None
            if other.listing_identity == listing_identity or mine & self._lineage(
                self._groups_of(other.registration_snapshot_id)
            ):
                blocking.append(ConflictRecord(intent.intent_id, IntentState(intent.state)))
        return tuple(sorted(blocking, key=lambda c: c.intent_id))

    def live_registrations(
        self, marketplace_key: str, marketplace_account_id: str, groups: Iterable[str]
    ) -> tuple[str, ...]:
        """The ACTIVE, verified registrations of this account with an Item whose current group
        the unit's groups reach by lineage (§13). An externally removed one is history only."""
        reach = self._lineage(frozenset(groups))
        rows = self.session.execute(
            select(
                MarketplaceRegistration.registration_id,
                MarketplaceRegistrationItem.current_group_id,
            )
            .join(
                MarketplaceRegistrationItem,
                MarketplaceRegistrationItem.registration_id
                == MarketplaceRegistration.registration_id,
            )
            .where(
                MarketplaceRegistration.marketplace_key == marketplace_key,
                MarketplaceRegistration.marketplace_account_id == marketplace_account_id,
                MarketplaceRegistration.lifecycle_state == RegistrationLifecycle.ACTIVE.value,
            )
        ).all()
        return tuple(sorted({registration for registration, group in rows if group in reach}))

    def pricing_pin(self, pricing_snapshot_id: str) -> PricingSnapshotRecord | None:
        """The exact M4 PricingSnapshot a Draft Item pins, read, never priced."""
        return PricingUnit(self.session, self._clock).snapshot(pricing_snapshot_id)

    def unit_generation(self, draft_id: str, unit_key: Iterable[tuple[str, str]]) -> int:
        """How many Snapshots of this Draft for exactly this unit (its Item keys) an Intent
        already names: each may have reached the marketplace (§7)."""
        wanted = sorted(unit_key)
        generation = 0
        for snapshot in self.session.scalars(
            select(RegistrationSnapshot).where(RegistrationSnapshot.draft_id == draft_id)
        ):
            keys = sorted(
                (group, signature)
                for group, signature in self.session.execute(
                    select(
                        RegistrationItemSnapshot.group_id_at_registration,
                        RegistrationItemSnapshot.composition_signature,
                    ).where(
                        RegistrationItemSnapshot.registration_snapshot_id
                        == snapshot.registration_snapshot_id
                    )
                ).all()
            )
            named = self.session.scalar(
                select(func.count())
                .select_from(RegistrationIntent)
                .where(
                    RegistrationIntent.registration_snapshot_id == snapshot.registration_snapshot_id
                )
            )
            if keys == wanted and named:
                generation += 1
        return generation

    def matching_snapshot(
        self,
        draft_id: str,
        draft_revision: int,
        listing_identity: str,
        preflight_fingerprint: str,
        payload_hash: str,
    ) -> SnapshotRecord | None:
        """An already frozen Snapshot of exactly this preparation, if any: freezing the same
        preparation again returns it rather than a second Snapshot."""
        row = self.session.scalars(
            select(RegistrationSnapshot).where(
                RegistrationSnapshot.draft_id == draft_id,
                RegistrationSnapshot.draft_revision == draft_revision,
                RegistrationSnapshot.listing_identity == listing_identity,
                RegistrationSnapshot.preflight_fingerprint == preflight_fingerprint,
                RegistrationSnapshot.payload_hash == payload_hash,
            )
        ).first()
        return None if row is None else self.snapshot(row.registration_snapshot_id)

    def intent(self, intent_id: str) -> IntentRecord | None:
        row = self.session.get(RegistrationIntent, intent_id)
        return None if row is None else _intent_record(row)

    def batch_summary(self, registration_batch_id: str) -> BatchSummary:
        """Derived from the batch's Intents; nothing about it is stored (§12)."""
        states = self.session.scalars(
            select(RegistrationIntent.state).where(
                RegistrationIntent.registration_batch_id == registration_batch_id
            )
        ).all()
        return summarize(IntentState(state) for state in states)

    # ------------------------------------------------------------------ attempts (§9, §10)

    def start_attempt(
        self,
        intent_id: str,
        *,
        sanitized_request: Mapping[str, Any],
        sanitizer_profile_version: str,
        correlation_id: str,
    ) -> AttemptRecord:
        """Open the next attempt and move the Intent to SENT. Only a PREPARED Intent, or one
        proven not applied, may be sent; an UNKNOWN is reconciled first, never resent."""
        intent = self._intent_row(intent_id)
        if intent.state not in (IntentState.PREPARED.value, IntentState.FAILED.value):
            raise RegistrationConflictError(
                "REGISTER_NOT_SENDABLE",
                "only a PREPARED Intent or one proven not applied may be sent",
                details={"state": intent.state},
            )
        _require_text(sanitizer_profile_version=sanitizer_profile_version)
        attempt_no = (
            self.session.scalar(
                select(func.max(RegistrationAttempt.attempt_no)).where(
                    RegistrationAttempt.intent_id == intent_id
                )
            )
            or 0
        ) + 1
        row = RegistrationAttempt(
            attempt_id=str(uuid.uuid4()),
            intent_id=intent_id,
            attempt_no=attempt_no,
            request_payload_hash=sanitized_digest(sanitized_request),
            sanitizer_profile_version=sanitizer_profile_version,
            started_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        intent.state = IntentState.SENT.value
        intent.remote_outcome = None
        intent.updated_at = self._clock.now()
        self.session.flush()
        self._attempt_event("start_attempt", row, intent, correlation_id)
        return _attempt_record(row)

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        remote_outcome: RemoteOutcome,
        correlation_id: str,
        marketplace_product_id: str | None = None,
        response_status: int | None = None,
        sanitized_response: Mapping[str, Any] | None = None,
        error_class: ErrorClass | None = None,
        error_code: str | None = None,
    ) -> IntentRecord:
        """Close the open attempt with the outcome the caller proved, and move its Intent:
        applied to SENT with its provider identity, proven not applied to FAILED, UNKNOWN to
        UNKNOWN. A 2xx is never CONFIRMED here: only a verification is (§11)."""
        row = self.session.get(RegistrationAttempt, attempt_id)
        if row is None or row.finished_at is not None:
            raise RegistrationConflictError("REGISTER_ATTEMPT_NOT_OPEN", "the attempt is not open")
        outcome = RemoteOutcome(remote_outcome)
        if (outcome is RemoteOutcome.APPLIED_PROVEN) != (marketplace_product_id is not None):
            raise InputValidationError(
                "REGISTER_PROVIDER_IDENTITY",
                "an applied outcome names its provider identity, and only an applied one does",
            )
        row.finished_at = self._clock.now()
        row.remote_outcome = outcome.value
        row.response_status = response_status
        row.response_digest = (
            None if sanitized_response is None else sanitized_digest(sanitized_response)
        )
        row.error_class = None if error_class is None else ErrorClass(error_class).value
        row.error_code = error_code
        self.session.flush()
        intent = self._intent_row(row.intent_id)
        self._settle(intent, outcome, marketplace_product_id)
        self._attempt_event("finish_attempt", row, intent, correlation_id)
        return _intent_record(intent)

    def resolve_unknown(
        self,
        intent_id: str,
        *,
        outcome: RemoteOutcome,
        resolved_by: ResolvedBy,
        evidence_kind: ResolutionEvidence,
        sanitized_evidence: Mapping[str, Any],
        correlation_id: str,
        actor: str,
        marketplace_product_id: str | None = None,
    ) -> IntentRecord:
        """Settle an UNKNOWN Intent with machine or provider evidence (§10, B3).

        ``resolved_by = USER`` names the operator who recorded or accepted the evidence; the
        evidence itself is always one of :class:`ResolutionEvidence`, which has no operator
        assertion. An operator's word alone therefore can never reach this method's write.
        """
        intent = self._intent_row(intent_id)
        if intent.state != IntentState.UNKNOWN.value:
            raise RegistrationConflictError("REGISTER_NOT_UNKNOWN", "only an UNKNOWN is resolved")
        proven = RemoteOutcome(outcome)
        if proven is RemoteOutcome.UNKNOWN:
            raise InputValidationError(
                "REGISTER_RESOLUTION_UNPROVEN", "an unresolved ambiguity stays UNKNOWN"
            )
        if (proven is RemoteOutcome.APPLIED_PROVEN) != (marketplace_product_id is not None):
            raise InputValidationError(
                "REGISTER_PROVIDER_IDENTITY",
                "an applied outcome names its provider identity, and only an applied one does",
            )
        evidence = ResolutionEvidence(evidence_kind)
        attempt = self._latest_attempt(intent_id)
        assert attempt is not None and attempt.remote_outcome == RemoteOutcome.UNKNOWN.value
        attempt.resolved_outcome = proven.value
        attempt.resolved_by = ResolvedBy(resolved_by).value
        attempt.resolution_evidence_kind = evidence.value
        attempt.resolution_evidence_digest = sanitized_digest(sanitized_evidence)
        attempt.resolved_at = self._clock.now()
        self.session.flush()
        self._settle(intent, proven, marketplace_product_id)
        self._event(
            AuditEventType.REGISTRATION_OUTCOME_RESOLVED,
            "resolve_unknown",
            actor,
            correlation_id,
            intent_id,
            {
                "attempt_id": attempt.attempt_id,
                "resolved_outcome": proven.value,
                "resolved_by": attempt.resolved_by,
                "evidence_kind": evidence.value,
                "evidence_digest": attempt.resolution_evidence_digest,
                "state": intent.state,
            },
        )
        return _intent_record(intent)

    def attempts(self, intent_id: str) -> tuple[AttemptRecord, ...]:
        rows = self.session.scalars(
            select(RegistrationAttempt)
            .where(RegistrationAttempt.intent_id == intent_id)
            .order_by(RegistrationAttempt.attempt_no)
        ).all()
        return tuple(_attempt_record(row) for row in rows)

    def active_job(self, job_type: str, intent_id: str, states: Sequence[str]) -> str | None:
        """The job of ``job_type`` that is still working on this Intent, if any.

        One Intent has at most one live CREATE job: the job system already owns when that job
        next runs, so a second job would bypass its retry schedule. The states that count as
        live are handed in, because they are the job system's to define and never this owner's.
        """
        return self.session.scalar(
            select(Job.job_id)
            .where(
                Job.job_type == job_type,
                Job.target_ref == f"intent:{intent_id}",
                Job.state.in_(tuple(states)),
            )
            .order_by(Job.created_at)
            .limit(1)
        )

    def scope_attempts(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        *,
        operation: Operation = Operation.CREATE,
        limit: int = 50,
    ) -> tuple[AttemptRecord, ...]:
        """The most recent attempts of one marketplace, canonical account and operation, newest
        first.

        The failure budget of an execution scope is counted from exactly this history, so the
        history must be the **same scope**: the attempts of one operation, which is the one
        endpoint group that operation sends to (ADR-0014 §9, §26). M5 has only ``CREATE``, and
        this filter keeps it that way — a later operation's attempts can never spend the CREATE
        budget, and no second authoritative state is introduced.
        """
        rows = self.session.scalars(
            select(RegistrationAttempt)
            .join(RegistrationIntent, RegistrationIntent.intent_id == RegistrationAttempt.intent_id)
            .where(
                RegistrationIntent.marketplace_key == marketplace_key,
                RegistrationIntent.marketplace_account_id == marketplace_account_id,
                RegistrationIntent.operation == Operation(operation).value,
            )
            .order_by(RegistrationAttempt.started_at.desc(), RegistrationAttempt.attempt_no.desc())
            .limit(limit)
        ).all()
        return tuple(_attempt_record(row) for row in rows)

    def jobs_with_open_attempts(
        self, job_type: str, terminal_states: Sequence[str], *, limit: int = 500
    ) -> tuple[str, ...]:
        """The jobs of ``job_type`` that a still-open attempt of this owner is waiting on.

        Registration rows carry no ``job_id``; a CREATE job names its Intent in ``target_ref``
        (``intent:<intent_id>``), so the join is on that. The terminal-state filter is applied in
        the query, before the bound, so a page of results can never hide the rest behind jobs that
        are still running (``JobDefinition.unsettled_owned_jobs``).
        """
        rows = self.session.scalars(
            select(Job.job_id)
            .join(
                RegistrationAttempt,
                Job.target_ref == "intent:" + RegistrationAttempt.intent_id,
            )
            .where(
                Job.job_type == job_type,
                Job.state.in_(tuple(terminal_states)),
                RegistrationAttempt.finished_at.is_(None),
            )
            .order_by(RegistrationAttempt.started_at)
            .limit(limit)
        ).all()
        return tuple(dict.fromkeys(rows))

    # ------------------------------------------------------------------ execution scope (§26)

    def execution_scope(
        self, marketplace_key: str, marketplace_account_id: str, endpoint_group: str
    ) -> ScopeRecord:
        """This scope's send brake. A scope that was never paused is ACTIVE with no boundary."""
        row = self.session.get(
            RegistrationExecutionScope,
            (marketplace_key, marketplace_account_id, endpoint_group),
        )
        if row is None:
            return ScopeRecord.active(marketplace_key, marketplace_account_id, endpoint_group)
        return _scope_record(row)

    def pause_scope(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        endpoint_group: str,
        *,
        reason: ScopePauseReason,
        policy_version: str,
        error_class: ErrorClass | None = None,
        actor: str,
        correlation_id: str,
    ) -> ScopeRecord:
        """Engage this scope's send brake for a cause proven by durable execution evidence (§26).

        **Idempotent for the same cause**: a scope already paused by this reason keeps its first
        boundary, and nothing is recorded twice. A different cause replaces the reason and dates
        the pause now, because that is a new brake for a new reason.
        """
        _require_text(endpoint_group=endpoint_group, policy_version=policy_version)
        _require_label(actor=actor)
        cause = ScopePauseReason(reason)
        measured = None if error_class is None else ErrorClass(error_class)
        if not pause_class_allowed(cause, measured):
            # A durable row never pairs a reason with a class that did not cause it: an AUTH brake
            # is an AUTH failure, a POLICY brake a POLICY_BLOCKED one, and a spent budget is
            # neither (§26). The schema repeats this, so no write path can store the pair.
            raise InputValidationError(
                "REGISTER_SCOPE_CAUSE_MISMATCH",
                "the recorded class is not a cause of this brake",
                details={
                    "pause_reason": cause.value,
                    "pause_error_class": None if measured is None else measured.value,
                },
            )
        row = self.session.get(
            RegistrationExecutionScope,
            (marketplace_key, marketplace_account_id, endpoint_group),
        )
        now = self._clock.now()
        if row is not None and row.state == ExecutionScopeState.PAUSED.value:
            if row.pause_reason == cause.value:
                return _scope_record(row)
        elif row is None:
            row = RegistrationExecutionScope(
                marketplace_key=marketplace_key,
                marketplace_account_id=marketplace_account_id,
                endpoint_group=endpoint_group,
                state=ExecutionScopeState.ACTIVE.value,
                resume_generation=0,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
        row.state = ExecutionScopeState.PAUSED.value
        row.pause_reason = cause.value
        row.pause_error_class = None if measured is None else measured.value
        # A brake is never dated before the release it follows: the row's own history only moves
        # forward, and a recorded release is never made to look later than it was.
        row.paused_at = now if row.resumed_at is None else max(now, row.resumed_at)
        row.pause_policy_version = policy_version
        row.updated_at = now
        self.session.flush()
        self._scope_event(
            AuditEventType.REGISTRATION_EXECUTION_SCOPE_PAUSED,
            "pause_scope",
            row,
            actor,
            correlation_id,
        )
        return _scope_record(row)

    def resume_scope(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        endpoint_group: str,
        *,
        actor: str,
        reason: str,
        correlation_id: str,
        allowed_reasons: frozenset[ScopePauseReason],
        at: datetime | None = None,
    ) -> ScopeRecord:
        """Release this scope's send brake, and move its durable boundary (§26).

        A resume claims nothing about the provider: it says the automatic brake is released and
        the next send re-runs the complete send-time gate. It never deletes or rewrites one
        recorded attempt — the budget simply counts what happened *after* ``resumed_at``.

        ``allowed_reasons`` is the caller's authority, and it is **required**: the automatic path
        may release only an `AUTH` pause (a fresh authentication proof), and an operator may
        release only a `POLICY` or `FAILURE_BUDGET` one (`OPERATOR_RESUMABLE`). Neither can reach
        the other's cause, whatever the caller believes. ``at`` is the accepted release time — the
        proof's own time for the automatic path — and must not predate the pause it releases.
        """
        _require_label(actor=actor, reason=reason)
        row = self.session.get(
            RegistrationExecutionScope,
            (marketplace_key, marketplace_account_id, endpoint_group),
        )
        if row is None or row.state != ExecutionScopeState.PAUSED.value:
            raise RegistrationConflictError(
                "REGISTER_SCOPE_NOT_PAUSED",
                "only a paused execution scope is resumed",
                details={"endpoint_group": endpoint_group},
            )
        permitted = {ScopePauseReason(r).value for r in allowed_reasons}
        if row.pause_reason not in permitted:
            raise RegistrationConflictError(
                "REGISTER_SCOPE_RESUME_NOT_PERMITTED",
                "this release does not answer the cause that paused the scope",
                details={
                    "pause_reason": row.pause_reason,
                    "allowed_reasons": sorted(permitted),
                },
            )
        now = self._clock.now()
        boundary = now if at is None else at
        if row.paused_at is not None and boundary < row.paused_at:
            raise InputValidationError(
                "REGISTER_SCOPE_RELEASE_NOT_NEWER",
                "a release older than the pause it answers is not a release",
            )
        row.state = ExecutionScopeState.ACTIVE.value
        row.pause_reason = None
        row.pause_error_class = None
        row.paused_at = None
        row.pause_policy_version = None
        row.resume_generation += 1
        row.resumed_at = boundary
        row.resumed_by = actor
        row.resume_reason = reason
        row.updated_at = now
        self.session.flush()
        self._scope_event(
            AuditEventType.REGISTRATION_EXECUTION_SCOPE_RESUMED,
            "resume_scope",
            row,
            actor,
            correlation_id,
        )
        return _scope_record(row)

    # ------------------------------------------------------------------ verification (§11)

    def record_mismatch(
        self,
        intent_id: str,
        *,
        comparison_contract_version: str,
        normalizer_version: str,
        sanitized_comparison: Mapping[str, Any],
        actor: str,
        correlation_id: str,
    ) -> IntentRecord:
        """The read-back of an applied listing does not match its Snapshot. The whole Intent stays
        not CONFIRMED, keeps its provider identity, and is never retried as a CREATE (§11, R3)."""
        intent = self._applied_intent(intent_id)
        self._verify(
            intent,
            VerificationState.MISMATCH,
            comparison_contract_version,
            normalizer_version,
            sanitized_comparison,
        )
        self._verification_event(intent, actor, correlation_id)
        return _intent_record(intent)

    def confirm_registration(
        self,
        intent_id: str,
        *,
        comparison_contract_version: str,
        normalizer_version: str,
        sanitized_readback: Mapping[str, Any],
        published_state: str,
        option_ids: Mapping[str, str | None],
        created_by: str,
        correlation_id: str,
    ) -> RegistrationRecord:
        """The read-back matched the Snapshot: CONFIRM the Intent and record the durable
        registration and its Items, together (§11). ``option_ids`` names every sent
        ``registration_item_key`` exactly once; the correspondence is by key, never by label."""
        intent = self._applied_intent(intent_id)
        snapshot = self.snapshot(intent.registration_snapshot_id)
        assert snapshot is not None
        if set(option_ids) != {i.registration_item_key for i in snapshot.items}:
            raise InputValidationError(
                "REGISTER_ITEM_CORRESPONDENCE",
                "a registration names exactly the Items its Snapshot sent, by item key",
            )
        _require_text(published_state=published_state)
        self._verify(
            intent,
            VerificationState.PASS,
            comparison_contract_version,
            normalizer_version,
            sanitized_readback,
        )
        assert intent.verified_at is not None and intent.marketplace_product_id is not None
        registration = MarketplaceRegistration(
            registration_id=str(uuid.uuid4()),
            intent_id=intent.intent_id,
            registration_snapshot_id=intent.registration_snapshot_id,
            marketplace_key=intent.marketplace_key,
            marketplace_account_id=intent.marketplace_account_id,
            marketplace_product_id=intent.marketplace_product_id,
            seller_product_code=snapshot.listing_identity,
            published_state=published_state,
            lifecycle_state=RegistrationLifecycle.ACTIVE.value,
            comparison_contract_version=comparison_contract_version,
            normalizer_version=normalizer_version,
            readback_evidence_digest=intent.verification_evidence_digest,
            verified_at=intent.verified_at,
            last_readback_at=intent.verified_at,
            created_by=created_by,
            correlation_id=correlation_id,
            created_at=self._clock.now(),
        )
        self.session.add(registration)
        self.session.flush()
        now = self._clock.now()
        for item in snapshot.items:
            self.session.add(
                MarketplaceRegistrationItem(
                    registration_item_id=str(uuid.uuid4()),
                    registration_id=registration.registration_id,
                    item_snapshot_id=item.item_snapshot_id,
                    registration_item_key=item.registration_item_key,
                    current_group_id=item.group_id_at_registration,
                    listing_composition_id=item.listing_composition_id,
                    marketplace_option_id=option_ids[item.registration_item_key],
                    created_at=now,
                )
            )
        self.session.flush()
        self._verification_event(intent, created_by, correlation_id)
        record = self.registration(registration.registration_id)
        assert record is not None
        return record

    # ------------------------------------------------------------------ registrations (§14)

    def record_readback(
        self, registration_id: str, *, actor: str, correlation_id: str
    ) -> RegistrationRecord:
        """An explicit reconcile of a known registration read it back again (§14). Recurring
        monitoring is M6 OPERATE and never happens here."""
        row = self._active_registration(registration_id)
        row.last_readback_at = self._clock.now()
        self.session.flush()
        self._event(
            AuditEventType.REGISTRATION_VERIFICATION_RECORDED,
            "record_readback",
            actor,
            correlation_id,
            registration_id,
            {"lifecycle_state": row.lifecycle_state},
        )
        record = self.registration(registration_id)
        assert record is not None
        return record

    def record_external_absence(
        self,
        registration_id: str,
        *,
        evidence_kind: AbsenceEvidence,
        sanitized_evidence: Mapping[str, Any],
        recorded_by: str,
        correlation_id: str,
    ) -> RegistrationRecord:
        """Provider evidence proves the listing absent (§14, R4). The registration, its Snapshot,
        its Attempts and its read-back evidence all stay; only the lifecycle becomes the terminal
        ``EXTERNALLY_REMOVED``. An operator's assertion is not an :class:`AbsenceEvidence`."""
        row = self._active_registration(registration_id)
        kind = AbsenceEvidence(evidence_kind)
        _require_text(recorded_by=recorded_by)
        row.lifecycle_state = RegistrationLifecycle.EXTERNALLY_REMOVED.value
        row.absence_observed_at = self._clock.now()
        row.absence_evidence_kind = kind.value
        row.absence_evidence_digest = sanitized_digest(sanitized_evidence)
        row.absence_recorded_by = recorded_by
        self.session.flush()
        self._event(
            AuditEventType.REGISTRATION_EXTERNAL_ABSENCE_RECORDED,
            "record_external_absence",
            recorded_by,
            correlation_id,
            registration_id,
            {
                "marketplace_product_id": row.marketplace_product_id,
                "evidence_kind": kind.value,
                "evidence_digest": row.absence_evidence_digest,
            },
        )
        record = self.registration(registration_id)
        assert record is not None
        return record

    def registration(self, registration_id: str) -> RegistrationRecord | None:
        row = self.session.get(MarketplaceRegistration, registration_id)
        if row is None:
            return None
        items = self.session.scalars(
            select(MarketplaceRegistrationItem)
            .where(MarketplaceRegistrationItem.registration_id == registration_id)
            .order_by(MarketplaceRegistrationItem.registration_item_key)
        ).all()
        return RegistrationRecord(
            registration_id=row.registration_id,
            intent_id=row.intent_id,
            registration_snapshot_id=row.registration_snapshot_id,
            marketplace_key=row.marketplace_key,
            marketplace_account_id=row.marketplace_account_id,
            marketplace_product_id=row.marketplace_product_id,
            seller_product_code=row.seller_product_code,
            published_state=row.published_state,
            lifecycle_state=RegistrationLifecycle(row.lifecycle_state),
            items=tuple(
                RegistrationItemRecord(
                    registration_item_id=i.registration_item_id,
                    registration_item_key=i.registration_item_key,
                    item_snapshot_id=i.item_snapshot_id,
                    current_group_id=i.current_group_id,
                    listing_composition_id=i.listing_composition_id,
                    current_source_binding_id=i.current_source_binding_id,
                    marketplace_option_id=i.marketplace_option_id,
                )
                for i in items
            ),
        )

    # ------------------------------------------------------------------ duplicates (§13)

    def record_duplicate_override(
        self,
        marketplace_key: str,
        marketplace_account_id: str,
        product_group_id: str,
        *,
        reason: str,
        approved_by: str,
        correlation_id: str,
        listing_composition_id: str | None = None,
    ) -> OverrideRecord:
        """An operator's explicit decision to allow an intentional duplicate listing in one
        canonical marketplace account. It never releases an unresolved UNKNOWN or an unproven
        removal."""
        _require_text(reason=reason, approved_by=approved_by)
        require_bound(self.session, marketplace_key, marketplace_account_id)
        row = DuplicateOverride(
            override_id=str(uuid.uuid4()),
            marketplace_key=marketplace_key,
            marketplace_account_id=marketplace_account_id,
            product_group_id=product_group_id,
            listing_composition_id=listing_composition_id,
            reason=reason,
            approved_by=approved_by,
            correlation_id=correlation_id,
            created_at=self._clock.now(),
        )
        self.session.add(row)
        self.session.flush()
        self._override_event("record_duplicate_override", row, approved_by, correlation_id)
        return _override_record(row)

    def revoke_duplicate_override(
        self, override_id: str, *, revoked_by: str, reason: str, correlation_id: str
    ) -> OverrideRecord:
        row = self.session.get(DuplicateOverride, override_id)
        if row is None or row.revoked_at is not None:
            raise RegistrationConflictError(
                "REGISTER_OVERRIDE_NOT_ACTIVE", "only an active override is revoked"
            )
        _require_text(revoked_by=revoked_by, reason=reason)
        row.revoked_at = self._clock.now()
        row.revoked_by = revoked_by
        row.revoke_reason = reason
        self.session.flush()
        self._override_event("revoke_duplicate_override", row, revoked_by, correlation_id)
        return _override_record(row)

    def active_overrides(
        self, marketplace_key: str, marketplace_account_id: str, product_group_id: str
    ) -> tuple[OverrideRecord, ...]:
        rows = self.session.scalars(
            select(DuplicateOverride).where(
                DuplicateOverride.marketplace_key == marketplace_key,
                DuplicateOverride.marketplace_account_id == marketplace_account_id,
                DuplicateOverride.product_group_id == product_group_id,
                DuplicateOverride.revoked_at.is_(None),
            )
        ).all()
        return tuple(_override_record(row) for row in rows)

    # ------------------------------------------------------------------ internals

    def _draft_row(self, draft_id: str) -> RegistrationDraft:
        row = self.session.get(RegistrationDraft, draft_id)
        if row is None:
            raise NotFoundError("REGISTER_DRAFT_NOT_FOUND", "the draft does not exist")
        return row

    def _open_items(self, draft_id: str) -> list[RegistrationDraftItem]:
        return list(
            self.session.scalars(
                select(RegistrationDraftItem)
                .where(
                    RegistrationDraftItem.draft_id == draft_id,
                    RegistrationDraftItem.removed_at.is_(None),
                )
                .order_by(RegistrationDraftItem.ordinal)
            ).all()
        )

    def _require_price(
        self, draft: RegistrationDraft, item_id: str, pricing_snapshot_id: str
    ) -> None:
        """The price is an exact M4 PricingSnapshot of this Item for the Draft's marketplace and
        canonical account; an account-free price applies to every account (ADR-0013 §7)."""
        price = self.session.get(PricingSnapshot, pricing_snapshot_id)
        if (
            price is None
            or price.item_id != item_id
            or price.marketplace_key != draft.marketplace_key
            or price.account_id not in (None, draft.marketplace_account_id)
        ):
            raise InputValidationError(
                "REGISTER_DRAFT_PRICE_CONTEXT",
                "a Draft Item is priced by an exact M4 snapshot of that Item and Draft target",
            )

    def _require_current_unit(self, snapshot: RegistrationSnapshot) -> None:
        """An Intent opens only from a Snapshot of its Draft's current revision whose Items are
        still open with their pinned prices; a single listing covers every open Item (R3)."""
        draft = self._draft_row(snapshot.draft_id)
        pins = {row.item_id: row.pricing_snapshot_id for row in self._open_items(draft.draft_id)}
        sent = {
            row.item_id: row.pricing_snapshot_id_at_registration
            for row in self.session.scalars(
                select(RegistrationItemSnapshot).where(
                    RegistrationItemSnapshot.registration_snapshot_id
                    == snapshot.registration_snapshot_id
                )
            )
        }
        if (
            (snapshot.draft_revision, snapshot.listing_shape)
            != (draft.draft_revision, draft.listing_shape)
            or any(pins.get(item_id) != price for item_id, price in sent.items())
            or (
                snapshot.listing_shape == ListingShape.SINGLE_LISTING_WITH_OPTIONS
                and uncovered_single_listing(pins, sent)
            )
        ):
            raise RegistrationConflictError(
                "REGISTER_DRAFT_STALE",
                "an Intent opens only from a Snapshot of its Draft's current revision",
            )

    def _advance(
        self,
        draft: RegistrationDraft,
        actor: str,
        correlation_id: str,
        action: str,
        item_id: str | None,
    ) -> DraftRecord:
        draft.draft_revision += 1
        draft.updated_at = self._clock.now()
        self.session.flush()
        details: dict[str, Any] = {
            "listing_shape": draft.listing_shape,
            "draft_revision": draft.draft_revision,
        }
        if item_id is not None:
            details["item_id"] = item_id
            pinned = next(
                (r for r in self._open_items(draft.draft_id) if r.item_id == item_id), None
            )
            if pinned is not None:
                details["pricing_snapshot_id"] = pinned.pricing_snapshot_id
        self._event(
            AuditEventType.REGISTRATION_DRAFT_RECORDED,
            action,
            actor,
            correlation_id,
            draft.draft_id,
            details,
        )
        return self._draft_record(draft)

    def _draft_record(self, row: RegistrationDraft) -> DraftRecord:
        return DraftRecord(
            draft_id=row.draft_id,
            marketplace_key=row.marketplace_key,
            marketplace_account_id=row.marketplace_account_id,
            listing_shape=ListingShape(row.listing_shape),
            draft_revision=row.draft_revision,
            items=tuple(
                DraftItemRecord(
                    draft_item_id=i.draft_item_id,
                    item_id=i.item_id,
                    product_group_id=i.product_group_id,
                    composition_signature=i.composition_signature,
                    pricing_snapshot_id=i.pricing_snapshot_id,
                    ordinal=i.ordinal,
                )
                for i in self._open_items(row.draft_id)
            ),
        )

    def _groups_of(self, registration_snapshot_id: str) -> frozenset[str]:
        return frozenset(
            self.session.scalars(
                select(RegistrationItemSnapshot.group_id_at_registration).where(
                    RegistrationItemSnapshot.registration_snapshot_id == registration_snapshot_id
                )
            ).all()
        )

    def _lineage(self, groups: frozenset[str]) -> frozenset[str]:
        """``groups`` and every group a MERGE or SPLIT connects to them, transitively. A split
        with an unclear successor names all its successors, so each one overlaps (fail-closed)."""
        events = [
            set(json.loads(e.predecessor_group_ids)) | set(json.loads(e.successor_group_ids))
            for e in self.session.scalars(select(GroupChangeEvent)).all()
        ]
        reached = set(groups)
        changed = True
        while changed:
            changed = False
            for connected in events:
                if connected & reached and not connected <= reached:
                    reached |= connected
                    changed = True
        return frozenset(reached)

    def _intent_row(self, intent_id: str) -> RegistrationIntent:
        row = self.session.get(RegistrationIntent, intent_id)
        if row is None:
            raise NotFoundError("REGISTER_INTENT_NOT_FOUND", "the Intent does not exist")
        return row

    def _latest_attempt(self, intent_id: str) -> RegistrationAttempt | None:
        return self.session.scalar(
            select(RegistrationAttempt)
            .where(RegistrationAttempt.intent_id == intent_id)
            .order_by(RegistrationAttempt.attempt_no.desc())
            .limit(1)
        )

    def _settle(
        self,
        intent: RegistrationIntent,
        outcome: RemoteOutcome,
        marketplace_product_id: str | None,
    ) -> None:
        if outcome is RemoteOutcome.APPLIED_PROVEN:
            intent.state = IntentState.SENT.value
            intent.marketplace_product_id = marketplace_product_id
        elif outcome is RemoteOutcome.NOT_APPLIED_PROVEN:
            intent.state = IntentState.FAILED.value
        else:
            intent.state = IntentState.UNKNOWN.value
        intent.remote_outcome = outcome.value
        intent.updated_at = self._clock.now()
        self.session.flush()

    def _applied_intent(self, intent_id: str) -> RegistrationIntent:
        intent = self._intent_row(intent_id)
        if (
            intent.state != IntentState.SENT.value
            or intent.remote_outcome != RemoteOutcome.APPLIED_PROVEN.value
        ):
            raise RegistrationConflictError(
                "REGISTER_NOT_APPLIED",
                "only an applied, unconfirmed CREATE is verified",
                details={"state": intent.state},
            )
        return intent

    def _verify(
        self,
        intent: RegistrationIntent,
        verification: VerificationState,
        comparison_contract_version: str,
        normalizer_version: str,
        sanitized_evidence: Mapping[str, Any],
    ) -> None:
        _require_text(
            comparison_contract_version=comparison_contract_version,
            normalizer_version=normalizer_version,
        )
        intent.verification_state = verification.value
        intent.comparison_contract_version = comparison_contract_version
        intent.normalizer_version = normalizer_version
        intent.verification_evidence_digest = sanitized_digest(sanitized_evidence)
        intent.verified_at = self._clock.now()
        if verification is VerificationState.PASS:
            intent.state = IntentState.CONFIRMED.value
        intent.updated_at = self._clock.now()
        self.session.flush()

    def _active_registration(self, registration_id: str) -> MarketplaceRegistration:
        row = self.session.get(MarketplaceRegistration, registration_id)
        if row is None or row.lifecycle_state != RegistrationLifecycle.ACTIVE.value:
            raise RegistrationConflictError(
                "REGISTER_REGISTRATION_NOT_ACTIVE", "only an ACTIVE registration changes"
            )
        return row

    def _event(
        self,
        event_type: AuditEventType,
        action: str,
        actor: str,
        correlation_id: str,
        target_ref: str,
        details: Mapping[str, Any],
    ) -> None:
        self._audit.append(
            AuditEntry(
                event_type=event_type,
                action=action,
                actor=actor,
                outcome=AuditOutcome.RECORDED,
                target_ref=target_ref,
                details=details,
                correlation_id=correlation_id,
            ),
            session=self.session,
        )

    def _attempt_event(
        self,
        action: str,
        attempt: RegistrationAttempt,
        intent: RegistrationIntent,
        correlation_id: str,
    ) -> None:
        self._event(
            AuditEventType.REGISTRATION_ATTEMPT_RECORDED,
            action,
            intent.created_by,
            correlation_id,
            intent.intent_id,
            {
                "attempt_id": attempt.attempt_id,
                "attempt_no": attempt.attempt_no,
                "request_payload_hash": attempt.request_payload_hash,
                "remote_outcome": attempt.remote_outcome,
                "error_class": attempt.error_class,
                "state": intent.state,
            },
        )

    def _verification_event(
        self, intent: RegistrationIntent, actor: str, correlation_id: str
    ) -> None:
        self._event(
            AuditEventType.REGISTRATION_VERIFICATION_RECORDED,
            "record_verification",
            actor,
            correlation_id,
            intent.intent_id,
            {
                "verification_state": intent.verification_state,
                "comparison_contract_version": intent.comparison_contract_version,
                "normalizer_version": intent.normalizer_version,
                "evidence_digest": intent.verification_evidence_digest,
                "state": intent.state,
            },
        )

    def _scope_event(
        self,
        event_type: AuditEventType,
        action: str,
        row: RegistrationExecutionScope,
        actor: str,
        correlation_id: str,
    ) -> None:
        """The history of one brake transition. The row itself stays the authoritative state."""
        self._event(
            event_type,
            action,
            actor,
            correlation_id,
            f"scope:{row.marketplace_key}:{row.marketplace_account_id}:{row.endpoint_group}",
            {
                "marketplace_key": row.marketplace_key,
                "marketplace_account_id": row.marketplace_account_id,
                "endpoint_group": row.endpoint_group,
                "state": row.state,
                "pause_reason": row.pause_reason,
                "pause_error_class": row.pause_error_class,
                "pause_policy_version": row.pause_policy_version,
                "resume_generation": row.resume_generation,
                "resume_reason": row.resume_reason,
            },
        )

    def _override_event(
        self, action: str, row: DuplicateOverride, actor: str, correlation_id: str
    ) -> None:
        self._event(
            AuditEventType.REGISTRATION_DUPLICATE_OVERRIDE_RECORDED,
            action,
            actor,
            correlation_id,
            row.override_id,
            {
                "marketplace_key": row.marketplace_key,
                "product_group_id": row.product_group_id,
                "listing_composition_id": row.listing_composition_id,
                "active": row.revoked_at is None,
            },
        )


# ---------------------------------------------------------------- helpers


def _json(value: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_text(**values: str) -> None:
    empty = sorted(name for name, value in values.items() if not value or not value.strip())
    if empty:
        raise InputValidationError("REGISTER_VALUE_MISSING", f"required: {', '.join(empty)}")


# An actor or reason recorded on the execution-scope brake is a plain label, never operator prose
# and never wire content: two independent layers, a deny-by-default grammar and the outbound
# sanitizer, keep a URL, a token or a payload value out of the durable row (§15, §26).
_SCOPE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}[A-Za-z0-9]$")


def _require_label(**values: str) -> None:
    bad = sorted(
        name
        for name, value in values.items()
        if not isinstance(value, str) or not _SCOPE_LABEL.fullmatch(value) or problems(value)
    )
    if bad:
        raise InputValidationError(
            "REGISTER_SCOPE_LABEL_UNSAFE",
            f"a plain label is required: {', '.join(bad)}",
        )


def _intent_record(row: RegistrationIntent) -> IntentRecord:
    return IntentRecord(
        intent_id=row.intent_id,
        registration_batch_id=row.registration_batch_id,
        registration_snapshot_id=row.registration_snapshot_id,
        marketplace_key=row.marketplace_key,
        marketplace_account_id=row.marketplace_account_id,
        operation=Operation(row.operation),
        idempotency_key=row.idempotency_key,
        state=IntentState(row.state),
        remote_outcome=None if row.remote_outcome is None else RemoteOutcome(row.remote_outcome),
        marketplace_product_id=row.marketplace_product_id,
        verification_state=VerificationState(row.verification_state),
    )


def _attempt_record(row: RegistrationAttempt) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=row.attempt_id,
        intent_id=row.intent_id,
        attempt_no=row.attempt_no,
        request_payload_hash=row.request_payload_hash,
        finished=row.finished_at is not None,
        remote_outcome=None if row.remote_outcome is None else RemoteOutcome(row.remote_outcome),
        resolved_outcome=(
            None if row.resolved_outcome is None else RemoteOutcome(row.resolved_outcome)
        ),
        resolved_by=None if row.resolved_by is None else ResolvedBy(row.resolved_by),
        resolution_evidence_kind=(
            None
            if row.resolution_evidence_kind is None
            else ResolutionEvidence(row.resolution_evidence_kind)
        ),
        error_class=None if row.error_class is None else ErrorClass(row.error_class),
        error_code=row.error_code,
        started_at=row.started_at,
    )


def _scope_record(row: RegistrationExecutionScope) -> ScopeRecord:
    return ScopeRecord(
        marketplace_key=row.marketplace_key,
        marketplace_account_id=row.marketplace_account_id,
        endpoint_group=row.endpoint_group,
        state=ExecutionScopeState(row.state),
        pause_reason=None if row.pause_reason is None else ScopePauseReason(row.pause_reason),
        pause_error_class=(
            None if row.pause_error_class is None else ErrorClass(row.pause_error_class)
        ),
        paused_at=row.paused_at,
        pause_policy_version=row.pause_policy_version,
        resume_generation=row.resume_generation,
        resumed_at=row.resumed_at,
        resumed_by=row.resumed_by,
        resume_reason=row.resume_reason,
    )


def _override_record(row: DuplicateOverride) -> OverrideRecord:
    return OverrideRecord(
        override_id=row.override_id,
        marketplace_key=row.marketplace_key,
        marketplace_account_id=row.marketplace_account_id,
        product_group_id=row.product_group_id,
        listing_composition_id=row.listing_composition_id,
        active=row.revoked_at is None,
    )
