"""Persistence of the M5 registration foundation (Issue #89 PR-B, ADR-0014).

Ten tables, in the order migration 0016 creates them:

- ``registration_drafts``: a Draft, scoped ``marketplace × account × draft_id`` (§2). Its only
  mutable state is the listing shape and the revision counter; readiness is never stored (§3).
- ``registration_draft_items``: a Draft's references to existing M4 Items. An open item is unique
  per Draft by Item and by ``group + composition_signature``; removal closes it, never deletes it.
- ``registration_snapshots``: the immutable Snapshot of one provider-listing unit (§6).
- ``registration_item_snapshots``: the immutable per-Item values that unit sent (§6, §7).
- ``registration_batches``: a group of Intents. No status column: its summary is derived (§12).
- ``registration_intents``: one CREATE Intent per exact Snapshot (§8), with its outcome and its
  verification (§10, §11).
- ``registration_attempts``: append-only execution history, finished once and resolved at most
  once, and only by machine or provider evidence (§9, §10, B3).
- ``marketplace_registrations`` and ``marketplace_registration_items``: the durable result after
  verification (§11), and a proven external absence (§14, R4).
- ``duplicate_overrides``: an operator's intentional-duplicate decision, scoped
  ``marketplace × account × group`` (§13).

CHECK constraints repeat the single-row invariants. The cross-row invariants — a Snapshot matches
its Draft and exactly the M4 truth it names, an Intent never opens inside an unresolved conflict
scope, an outcome is backed by its attempt, a registration follows a verified Intent — are the
triggers of migration 0016, so no write path can bypass them.

Every digest column holds the SHA-256 of a sanitized canonical representation, never of wire bytes
(§15, B4). ``group_membership_revision_id`` has no foreign key here on purpose: only the product
store may name the membership tables (a repository rule), so migration 0016's trigger checks that
the revision exists and belongs to the Item's group.
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy import text as sql
from sqlalchemy.orm import Mapped, mapped_column

from app.connect.marketplace.capability import RemoteOutcome
from app.core.errors import ErrorClass
from app.db.base import Base
from app.db.types import UTCDateTime
from app.register.model import (
    AbsenceEvidence,
    IntentState,
    ListingShape,
    Operation,
    RegistrationLifecycle,
    ResolutionEvidence,
    ResolvedBy,
    VerificationState,
)


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(str(v)) for v in values)})"


def _hex64(column: str) -> str:
    return f"length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _present(column: str) -> str:
    return f"{column} <> ''"


def _json_object(column: str) -> str:
    return f"json_valid({column}) AND json_type({column}) = 'object'"


def _json_array(column: str, *, minimum: int) -> str:
    return (
        f"json_valid({column}) AND json_type({column}) = 'array'"
        f" AND json_array_length({column}) >= {minimum}"
    )


def _listing_identity(column: str) -> str:
    return (
        f"length({column}) BETWEEN 8 AND 64 AND {column} NOT GLOB '*[^A-Za-z0-9_-]*'"
        f" AND substr({column}, 1, 1) NOT IN ('_', '-')"
    )


def _item_key(column: str) -> str:
    return (
        f"length({column}) = 37 AND substr({column}, 1, 5) = 'rik1-'"
        f" AND substr({column}, 6) NOT GLOB '*[^0-9a-f]*'"
    )


class RegistrationDraft(Base):
    __tablename__ = "registration_drafts"
    __table_args__ = (
        CheckConstraint(_present("marketplace_key"), name="marketplace_key_present"),
        CheckConstraint(_present("account_id"), name="account_id_present"),
        CheckConstraint(_in("listing_shape", ListingShape), name="listing_shape_valid"),
        CheckConstraint("draft_revision >= 1", name="draft_revision_positive"),
        CheckConstraint(_present("created_by"), name="created_by_present"),
    )

    draft_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(64))
    listing_shape: Mapped[str] = mapped_column(String(40))
    draft_revision: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RegistrationDraftItem(Base):
    __tablename__ = "registration_draft_items"
    __table_args__ = (
        Index(
            "ux_registration_draft_items_open_item",
            "draft_id",
            "item_id",
            unique=True,
            sqlite_where=sql("removed_at IS NULL"),
        ),
        Index(
            "ux_registration_draft_items_open_key",
            "draft_id",
            "product_group_id",
            "composition_signature",
            unique=True,
            sqlite_where=sql("removed_at IS NULL"),
        ),
        Index(
            "ux_registration_draft_items_open_ordinal",
            "draft_id",
            "ordinal",
            unique=True,
            sqlite_where=sql("removed_at IS NULL"),
        ),
        CheckConstraint(_hex64("composition_signature"), name="signature_hex"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint(_present("added_by"), name="added_by_present"),
        CheckConstraint("(removed_at IS NULL) = (removed_by IS NULL)", name="removal_recorded"),
        CheckConstraint("removed_by IS NULL OR removed_by <> ''", name="removed_by_present"),
        CheckConstraint("removed_at IS NULL OR removed_at >= added_at", name="removal_ordered"),
    )

    draft_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(36), ForeignKey("registration_drafts.draft_id"))
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_items.item_id"))
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    composition_signature: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    added_by: Mapped[str] = mapped_column(String(64))
    added_at: Mapped[datetime] = mapped_column(UTCDateTime)
    removed_by: Mapped[str | None] = mapped_column(String(64))
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class RegistrationSnapshot(Base):
    __tablename__ = "registration_snapshots"
    __table_args__ = (
        CheckConstraint(_present("marketplace_key"), name="marketplace_key_present"),
        CheckConstraint(_present("account_id"), name="account_id_present"),
        CheckConstraint(_in("listing_shape", ListingShape), name="listing_shape_valid"),
        CheckConstraint("draft_revision >= 1", name="draft_revision_positive"),
        CheckConstraint(_listing_identity("listing_identity"), name="listing_identity_format"),
        CheckConstraint(_present("preflight_rule_version"), name="preflight_rule_version_present"),
        CheckConstraint(_hex64("preflight_fingerprint"), name="preflight_fingerprint_hex"),
        CheckConstraint(
            _present("category_mapping_revision"), name="category_mapping_revision_present"
        ),
        CheckConstraint(_present("taxonomy_revision"), name="taxonomy_revision_present"),
        CheckConstraint(_json_object("policy_revisions_json"), name="policy_revisions_is_object"),
        CheckConstraint(
            _present("detail_composition_revision"), name="detail_composition_revision_present"
        ),
        CheckConstraint(
            _present("sanitizer_profile_version"), name="sanitizer_profile_version_present"
        ),
        CheckConstraint(_hex64("payload_hash"), name="payload_hash_hex"),
        CheckConstraint(_json_object("payload_json"), name="payload_is_object"),
        CheckConstraint(_present("created_by"), name="created_by_present"),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
    )

    registration_snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(36), ForeignKey("registration_drafts.draft_id"))
    draft_revision: Mapped[int] = mapped_column(Integer)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(64))
    listing_shape: Mapped[str] = mapped_column(String(40))
    listing_identity: Mapped[str] = mapped_column(String(64))
    preflight_rule_version: Mapped[str] = mapped_column(String(64))
    preflight_fingerprint: Mapped[str] = mapped_column(String(64))
    category_mapping_revision: Mapped[str] = mapped_column(String(64))
    taxonomy_revision: Mapped[str] = mapped_column(String(64))
    policy_revisions_json: Mapped[str] = mapped_column(Text)
    detail_composition_revision: Mapped[str] = mapped_column(String(64))
    sanitizer_profile_version: Mapped[str] = mapped_column(String(64))
    # SHA-256 of the sanitized canonical evidence representation (§15), never of wire bytes.
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class RegistrationItemSnapshot(Base):
    __tablename__ = "registration_item_snapshots"
    __table_args__ = (
        UniqueConstraint("registration_snapshot_id", "registration_item_key"),
        UniqueConstraint("registration_snapshot_id", "item_id"),
        UniqueConstraint("registration_snapshot_id", "ordinal"),
        CheckConstraint(_item_key("registration_item_key"), name="item_key_format"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint(_hex64("composition_signature"), name="signature_hex"),
        CheckConstraint(_json_object("source_snapshot_json"), name="source_snapshot_is_object"),
        CheckConstraint(
            _json_array("publication_assets_json", minimum=1), name="publication_assets_present"
        ),
        CheckConstraint(_json_object("outbound_values_json"), name="outbound_values_is_object"),
    )

    item_snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    registration_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_snapshots.registration_snapshot_id")
    )
    registration_item_key: Mapped[str] = mapped_column(String(40))
    ordinal: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[str] = mapped_column(String(36), ForeignKey("product_items.item_id"))
    group_id_at_registration: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    group_membership_revision_id: Mapped[str] = mapped_column(String(36))
    listing_composition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listing_compositions.composition_id")
    )
    composition_signature: Mapped[str] = mapped_column(String(64))
    source_product_facts_revision_id_at_registration: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_facts_revisions.revision_id")
    )
    pricing_snapshot_id_at_registration: Mapped[str] = mapped_column(
        String(36), ForeignKey("pricing_snapshots.pricing_snapshot_id")
    )
    source_snapshot_json: Mapped[str] = mapped_column(Text)
    publication_assets_json: Mapped[str] = mapped_column(Text)
    outbound_values_json: Mapped[str] = mapped_column(Text)


class RegistrationBatch(Base):
    __tablename__ = "registration_batches"
    __table_args__ = (
        CheckConstraint(_present("marketplace_key"), name="marketplace_key_present"),
        CheckConstraint(_present("account_id"), name="account_id_present"),
        CheckConstraint(_present("created_by"), name="created_by_present"),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
    )

    registration_batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


_STATE_AGREES = (
    "(state <> 'PREPARED' OR (remote_outcome IS NULL AND verification_state = 'NOT_VERIFIED'))"
    " AND (state <> 'UNKNOWN' OR remote_outcome = 'UNKNOWN')"
    " AND (state <> 'FAILED' OR remote_outcome = 'NOT_APPLIED_PROVEN')"
    " AND (state <> 'SENT' OR remote_outcome IS NULL OR remote_outcome = 'APPLIED_PROVEN')"
    " AND (state <> 'CONFIRMED'"
    " OR (remote_outcome = 'APPLIED_PROVEN' AND verification_state = 'PASS'))"
    " AND (verification_state <> 'PASS' OR state = 'CONFIRMED')"
)
_VERIFIED = (
    "verification_state = 'NOT_VERIFIED' OR (remote_outcome = 'APPLIED_PROVEN'"
    " AND comparison_contract_version <> '' AND normalizer_version <> ''"
    f" AND {_hex64('verification_evidence_digest')} AND verified_at IS NOT NULL)"
)


class RegistrationIntent(Base):
    __tablename__ = "registration_intents"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        UniqueConstraint("registration_snapshot_id", "operation"),
        Index("ix_registration_intents_scope", "marketplace_key", "account_id", "state"),
        CheckConstraint(_present("marketplace_key"), name="marketplace_key_present"),
        CheckConstraint(_present("account_id"), name="account_id_present"),
        CheckConstraint(_in("operation", Operation), name="operation_create_only"),
        CheckConstraint(_hex64("idempotency_key"), name="idempotency_key_hex"),
        CheckConstraint(_in("state", IntentState), name="state_valid"),
        CheckConstraint(
            f"remote_outcome IS NULL OR {_in('remote_outcome', RemoteOutcome)}",
            name="remote_outcome_valid",
        ),
        CheckConstraint(_in("verification_state", VerificationState), name="verification_valid"),
        CheckConstraint(_STATE_AGREES, name="state_agrees_with_outcome"),
        CheckConstraint(
            "(marketplace_product_id IS NOT NULL) = (remote_outcome IS 'APPLIED_PROVEN')",
            name="provider_identity_when_applied",
        ),
        CheckConstraint(
            "marketplace_product_id IS NULL OR marketplace_product_id <> ''",
            name="provider_identity_present",
        ),
        CheckConstraint(_VERIFIED, name="verification_recorded"),
        CheckConstraint(_present("created_by"), name="created_by_present"),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
    )

    intent_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    registration_batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_batches.registration_batch_id")
    )
    registration_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_snapshots.registration_snapshot_id")
    )
    marketplace_key: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20))
    remote_outcome: Mapped[str | None] = mapped_column(String(20))
    marketplace_product_id: Mapped[str | None] = mapped_column(String(64))
    verification_state: Mapped[str] = mapped_column(String(20))
    comparison_contract_version: Mapped[str | None] = mapped_column(String(64))
    normalizer_version: Mapped[str | None] = mapped_column(String(64))
    # SHA-256 of the sanitized read-back comparison evidence (§15), never of wire bytes.
    verification_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


_OPEN_IS_BARE = (
    "finished_at IS NOT NULL OR (remote_outcome IS NULL AND response_status IS NULL"
    " AND response_digest IS NULL AND error_class IS NULL AND error_code IS NULL)"
)
_RESOLUTION_COMPLETE = (
    "(resolved_outcome IS NULL) = (resolved_by IS NULL)"
    " AND (resolved_outcome IS NULL) = (resolution_evidence_kind IS NULL)"
    " AND (resolved_outcome IS NULL) = (resolution_evidence_digest IS NULL)"
    " AND (resolved_outcome IS NULL) = (resolved_at IS NULL)"
)


class RegistrationAttempt(Base):
    __tablename__ = "registration_attempts"
    __table_args__ = (
        UniqueConstraint("intent_id", "attempt_no"),
        Index(
            "ux_registration_attempts_one_open",
            "intent_id",
            unique=True,
            sqlite_where=sql("finished_at IS NULL"),
        ),
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(_hex64("request_payload_hash"), name="request_payload_hash_hex"),
        CheckConstraint(
            _present("sanitizer_profile_version"), name="sanitizer_profile_version_present"
        ),
        CheckConstraint(_OPEN_IS_BARE, name="open_attempt_is_bare"),
        CheckConstraint(
            "finished_at IS NULL OR remote_outcome IS NOT NULL", name="finished_has_outcome"
        ),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="finish_ordered"),
        CheckConstraint(
            f"remote_outcome IS NULL OR {_in('remote_outcome', RemoteOutcome)}",
            name="remote_outcome_valid",
        ),
        CheckConstraint(
            f"error_class IS NULL OR {_in('error_class', ErrorClass)}", name="error_class_valid"
        ),
        CheckConstraint("error_code IS NULL OR error_code <> ''", name="error_code_present"),
        CheckConstraint(
            f"response_digest IS NULL OR ({_hex64('response_digest')})", name="response_digest_hex"
        ),
        CheckConstraint(_RESOLUTION_COMPLETE, name="resolution_complete"),
        CheckConstraint(
            "resolved_outcome IS NULL OR (remote_outcome = 'UNKNOWN'"
            " AND resolved_outcome IN ('APPLIED_PROVEN', 'NOT_APPLIED_PROVEN'))",
            name="resolves_only_unknown",
        ),
        CheckConstraint(
            f"resolved_by IS NULL OR {_in('resolved_by', ResolvedBy)}", name="resolved_by_valid"
        ),
        CheckConstraint(
            "resolution_evidence_kind IS NULL"
            f" OR {_in('resolution_evidence_kind', ResolutionEvidence)}",
            name="resolution_evidence_valid",
        ),
        CheckConstraint(
            f"resolution_evidence_digest IS NULL OR ({_hex64('resolution_evidence_digest')})",
            name="resolution_evidence_digest_hex",
        ),
        CheckConstraint(
            "(resolved_by IS NOT 'READ_BACK' OR resolution_evidence_kind = 'PROVIDER_READ_BACK')"
            " AND (resolved_by IS NOT 'LOOKUP' OR resolution_evidence_kind = 'PROVIDER_LOOKUP')",
            name="resolver_matches_evidence",
        ),
        CheckConstraint(
            "resolution_evidence_kind IS NOT 'TRANSMISSION_PRECLUDED'"
            " OR resolved_outcome = 'NOT_APPLIED_PROVEN'",
            name="precluded_proves_absence_only",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(36), ForeignKey("registration_intents.intent_id"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    # SHA-256 of the sanitized canonical request representation (§15), never of wire bytes.
    request_payload_hash: Mapped[str] = mapped_column(String(64))
    sanitizer_profile_version: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    error_class: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(64))
    remote_outcome: Mapped[str | None] = mapped_column(String(20))
    resolved_outcome: Mapped[str | None] = mapped_column(String(20))
    resolved_by: Mapped[str | None] = mapped_column(String(20))
    resolution_evidence_kind: Mapped[str | None] = mapped_column(String(30))
    resolution_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


_REMOVED = (
    "(lifecycle_state = 'EXTERNALLY_REMOVED') = (absence_observed_at IS NOT NULL)"
    " AND (absence_observed_at IS NULL) = (absence_evidence_kind IS NULL)"
    " AND (absence_observed_at IS NULL) = (absence_evidence_digest IS NULL)"
    " AND (absence_observed_at IS NULL) = (absence_recorded_by IS NULL)"
)


class MarketplaceRegistration(Base):
    __tablename__ = "marketplace_registrations"
    __table_args__ = (
        UniqueConstraint("intent_id"),
        UniqueConstraint("marketplace_key", "marketplace_product_id"),
        CheckConstraint(_present("marketplace_key"), name="marketplace_key_present"),
        CheckConstraint(_present("account_id"), name="account_id_present"),
        CheckConstraint(_present("marketplace_product_id"), name="provider_identity_present"),
        CheckConstraint(_listing_identity("seller_product_code"), name="seller_code_format"),
        CheckConstraint(_present("published_state"), name="published_state_present"),
        CheckConstraint(_in("lifecycle_state", RegistrationLifecycle), name="lifecycle_valid"),
        CheckConstraint(
            _present("comparison_contract_version"), name="comparison_contract_version_present"
        ),
        CheckConstraint(_present("normalizer_version"), name="normalizer_version_present"),
        CheckConstraint(_hex64("readback_evidence_digest"), name="readback_evidence_digest_hex"),
        CheckConstraint("last_readback_at >= verified_at", name="readback_ordered"),
        CheckConstraint(_REMOVED, name="absence_recorded"),
        CheckConstraint(
            f"absence_evidence_kind IS NULL OR {_in('absence_evidence_kind', AbsenceEvidence)}",
            name="absence_evidence_valid",
        ),
        CheckConstraint(
            f"absence_evidence_digest IS NULL OR ({_hex64('absence_evidence_digest')})",
            name="absence_evidence_digest_hex",
        ),
        CheckConstraint(
            "absence_recorded_by IS NULL OR absence_recorded_by <> ''",
            name="absence_recorded_by_present",
        ),
        CheckConstraint(_present("created_by"), name="created_by_present"),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
    )

    registration_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(36), ForeignKey("registration_intents.intent_id"))
    registration_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_snapshots.registration_snapshot_id")
    )
    marketplace_key: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(64))
    marketplace_product_id: Mapped[str] = mapped_column(String(64))
    # The provider-listing unit's listing identity as sent (§7). Its wire field is PR-D's.
    seller_product_code: Mapped[str] = mapped_column(String(64))
    published_state: Mapped[str] = mapped_column(String(40))
    lifecycle_state: Mapped[str] = mapped_column(String(30))
    comparison_contract_version: Mapped[str] = mapped_column(String(64))
    normalizer_version: Mapped[str] = mapped_column(String(64))
    readback_evidence_digest: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_readback_at: Mapped[datetime] = mapped_column(UTCDateTime)
    absence_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    absence_evidence_kind: Mapped[str | None] = mapped_column(String(30))
    absence_evidence_digest: Mapped[str | None] = mapped_column(String(64))
    absence_recorded_by: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class MarketplaceRegistrationItem(Base):
    __tablename__ = "marketplace_registration_items"
    __table_args__ = (
        UniqueConstraint("registration_id", "registration_item_key"),
        UniqueConstraint("item_snapshot_id"),
        CheckConstraint(_item_key("registration_item_key"), name="item_key_format"),
        CheckConstraint(
            "marketplace_option_id IS NULL OR marketplace_option_id <> ''",
            name="option_identity_present",
        ),
    )

    registration_item_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    registration_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("marketplace_registrations.registration_id")
    )
    item_snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("registration_item_snapshots.item_snapshot_id")
    )
    registration_item_key: Mapped[str] = mapped_column(String(40))
    current_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    listing_composition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("listing_compositions.composition_id")
    )
    current_source_binding_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_bindings.binding_id")
    )
    marketplace_option_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class DuplicateOverride(Base):
    __tablename__ = "duplicate_overrides"
    __table_args__ = (
        CheckConstraint(_present("marketplace_key"), name="marketplace_key_present"),
        CheckConstraint(_present("account_id"), name="account_id_present"),
        CheckConstraint(_present("reason"), name="reason_present"),
        CheckConstraint(_present("approved_by"), name="approved_by_present"),
        CheckConstraint(_present("correlation_id"), name="correlation_present"),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)"
            " AND (revoked_at IS NULL) = (revoke_reason IS NULL)",
            name="revocation_recorded",
        ),
        CheckConstraint(
            "revoke_reason IS NULL OR (revoke_reason <> '' AND revoked_by <> '')",
            name="revocation_present",
        ),
    )

    override_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    marketplace_key: Mapped[str] = mapped_column(String(40))
    account_id: Mapped[str] = mapped_column(String(64))
    product_group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("product_groups.product_group_id")
    )
    listing_composition_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("listing_compositions.composition_id")
    )
    reason: Mapped[str] = mapped_column(Text)
    approved_by: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_by: Mapped[str | None] = mapped_column(String(64))
    revoke_reason: Mapped[str | None] = mapped_column(Text)
