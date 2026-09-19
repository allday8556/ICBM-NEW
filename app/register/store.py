"""REGISTER persistence for the M5 foundation (Issue #89 PR-B, ADR-0014).

This store writes and reads the registration rows and nothing more. It never decides:
- a preflight or its fingerprint (PR-C): the caller names them when it freezes a Snapshot;
- a payload, a category or an outbound value (PR-C): the caller gives the sanitized values;
- a marketplace request, a read-back or a comparison (PR-D, PR-E): the caller reports their
  sanitized results and the outcome it proved.

Every cross-row invariant is enforced by the database (migration 0016), so this store is not the
only guard. It adds the domain side:
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
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.marketplace.capability import RemoteOutcome
from app.core.clock import Clock
from app.core.errors import ErrorClass, InputValidationError, NotFoundError
from app.db.database import Database
from app.products.models import GroupChangeEvent, ProductItem
from app.register.model import (
    BLOCKING_STATES,
    AbsenceEvidence,
    BatchSummary,
    IntentState,
    ListingShape,
    Operation,
    RegistrationConflictError,
    RegistrationLifecycle,
    ResolutionEvidence,
    ResolvedBy,
    VerificationState,
    effective_outcome,
    idempotency_key,
    registration_item_key,
    sanitized_digest,
    summarize,
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
    RegistrationIntent,
    RegistrationItemSnapshot,
    RegistrationSnapshot,
)

# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class DraftItemRecord:
    draft_item_id: str
    item_id: str
    product_group_id: str
    composition_signature: str
    ordinal: int


@dataclass(frozen=True)
class DraftRecord:
    draft_id: str
    marketplace_key: str
    account_id: str
    listing_shape: ListingShape
    draft_revision: int
    items: tuple[DraftItemRecord, ...]


@dataclass(frozen=True)
class ItemSnapshotSpec:
    """What the caller (PR-C) froze for one Item. Every mapping is already sanitized."""

    item_id: str
    group_membership_revision_id: str
    source_product_facts_revision_id: str
    pricing_snapshot_id: str
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
    account_id: str
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
    account_id: str
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
    account_id: str
    marketplace_product_id: str
    seller_product_code: str
    published_state: str
    lifecycle_state: RegistrationLifecycle
    items: tuple[RegistrationItemRecord, ...]


@dataclass(frozen=True)
class OverrideRecord:
    override_id: str
    marketplace_key: str
    account_id: str
    product_group_id: str
    listing_composition_id: str | None
    active: bool


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
        account_id: str,
        listing_shape: ListingShape,
        *,
        created_by: str,
        correlation_id: str,
    ) -> DraftRecord:
        _require_text(marketplace_key=marketplace_key, account_id=account_id, created_by=created_by)
        now = self._clock.now()
        row = RegistrationDraft(
            draft_id=str(uuid.uuid4()),
            marketplace_key=marketplace_key,
            account_id=account_id,
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
            {"listing_shape": row.listing_shape, "draft_revision": 1},
        )
        return self._draft_record(row)

    def add_draft_item(
        self, draft_id: str, item_id: str, *, added_by: str, correlation_id: str
    ) -> DraftRecord:
        draft = self._draft_row(draft_id)
        item = self.session.get(ProductItem, item_id)
        if item is None:
            raise NotFoundError("REGISTER_ITEM_NOT_FOUND", "the draft names no existing M4 Item")
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
            ordinal=max((r.ordinal for r in open_items), default=-1) + 1,
            added_by=added_by,
            added_at=now,
        )
        self.session.add(row)
        self.session.flush()
        return self._advance(draft, added_by, correlation_id, "add_draft_item", item_id)

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
        fingerprint it names and never re-decides it."""
        draft = self._draft_row(spec.draft_id)
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
        open_items = {row.item_id for row in self._open_items(spec.draft_id)}
        if (
            len({i.item_id for i in spec.items}) != len(spec.items)
            or not {i.item_id for i in spec.items} <= open_items
        ):
            raise InputValidationError(
                "REGISTER_SNAPSHOT_ITEMS", "a Snapshot sends distinct open items of its Draft"
            )
        snapshot = RegistrationSnapshot(
            registration_snapshot_id=str(uuid.uuid4()),
            draft_id=draft.draft_id,
            draft_revision=draft.draft_revision,
            marketplace_key=draft.marketplace_key,
            account_id=draft.account_id,
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
            assert item is not None  # an open draft item names an existing Item
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
                    group_membership_revision_id=item_spec.group_membership_revision_id,
                    listing_composition_id=item.composition_id,
                    composition_signature=item.composition_signature,
                    source_product_facts_revision_id_at_registration=(
                        item_spec.source_product_facts_revision_id
                    ),
                    pricing_snapshot_id_at_registration=item_spec.pricing_snapshot_id,
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
            account_id=row.account_id,
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

    # ------------------------------------------------------------------ batches and intents (§8)

    def create_batch(
        self, marketplace_key: str, account_id: str, *, created_by: str, correlation_id: str
    ) -> str:
        _require_text(marketplace_key=marketplace_key, account_id=account_id, created_by=created_by)
        row = RegistrationBatch(
            registration_batch_id=str(uuid.uuid4()),
            marketplace_key=marketplace_key,
            account_id=account_id,
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
        if (batch.marketplace_key, batch.account_id) != (
            snapshot.marketplace_key,
            snapshot.account_id,
        ):
            raise InputValidationError(
                "REGISTER_SCOPE_MISMATCH", "a batch and its Snapshots share one marketplace account"
            )
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
            account_id=snapshot.account_id,
            operation=Operation.CREATE.value,
            idempotency_key=idempotency_key(
                snapshot.marketplace_key,
                snapshot.account_id,
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

        Overlap is the same marketplace and account, and either the same listing identity or a
        shared group, where each side's groups are widened by the MERGE and SPLIT lineage that
        connects them. An unclear successor therefore counts as overlapping (fail-closed).
        """
        snapshot = self.session.get(RegistrationSnapshot, registration_snapshot_id)
        if snapshot is None:
            raise NotFoundError("REGISTER_NOT_FOUND", "the Snapshot does not exist")
        mine = self._lineage(self._groups_of(registration_snapshot_id))
        candidates = self.session.scalars(
            select(RegistrationIntent).where(
                RegistrationIntent.operation == Operation.CREATE.value,
                RegistrationIntent.marketplace_key == snapshot.marketplace_key,
                RegistrationIntent.account_id == snapshot.account_id,
                RegistrationIntent.state.in_([s.value for s in BLOCKING_STATES]),
                RegistrationIntent.registration_snapshot_id != registration_snapshot_id,
            )
        ).all()
        blocking = []
        for intent in candidates:
            other = self.session.get(RegistrationSnapshot, intent.registration_snapshot_id)
            assert other is not None
            if other.listing_identity == snapshot.listing_identity or mine & self._lineage(
                self._groups_of(other.registration_snapshot_id)
            ):
                blocking.append(intent.intent_id)
        return tuple(sorted(blocking))

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
            account_id=intent.account_id,
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
            account_id=row.account_id,
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
        account_id: str,
        product_group_id: str,
        *,
        reason: str,
        approved_by: str,
        correlation_id: str,
        listing_composition_id: str | None = None,
    ) -> OverrideRecord:
        """An operator's explicit decision to allow an intentional duplicate listing in one
        marketplace account. It never releases an unresolved UNKNOWN or an unproven removal."""
        _require_text(
            marketplace_key=marketplace_key,
            account_id=account_id,
            reason=reason,
            approved_by=approved_by,
        )
        row = DuplicateOverride(
            override_id=str(uuid.uuid4()),
            marketplace_key=marketplace_key,
            account_id=account_id,
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
        self, marketplace_key: str, account_id: str, product_group_id: str
    ) -> tuple[OverrideRecord, ...]:
        rows = self.session.scalars(
            select(DuplicateOverride).where(
                DuplicateOverride.marketplace_key == marketplace_key,
                DuplicateOverride.account_id == account_id,
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
            account_id=row.account_id,
            listing_shape=ListingShape(row.listing_shape),
            draft_revision=row.draft_revision,
            items=tuple(
                DraftItemRecord(
                    draft_item_id=i.draft_item_id,
                    item_id=i.item_id,
                    product_group_id=i.product_group_id,
                    composition_signature=i.composition_signature,
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


def _intent_record(row: RegistrationIntent) -> IntentRecord:
    return IntentRecord(
        intent_id=row.intent_id,
        registration_batch_id=row.registration_batch_id,
        registration_snapshot_id=row.registration_snapshot_id,
        marketplace_key=row.marketplace_key,
        account_id=row.account_id,
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
    )


def _override_record(row: DuplicateOverride) -> OverrideRecord:
    return OverrideRecord(
        override_id=row.override_id,
        marketplace_key=row.marketplace_key,
        account_id=row.account_id,
        product_group_id=row.product_group_id,
        listing_composition_id=row.listing_composition_id,
        active=row.revoked_at is None,
    )
