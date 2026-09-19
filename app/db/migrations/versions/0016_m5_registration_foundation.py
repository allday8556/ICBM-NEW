"""M5 PR-B: the registration foundation — Drafts, immutable Snapshots, Batches, Intents,
append-only Attempts, verified Registrations and duplicate overrides.

Revision ID: 0016_m5_registration_foundation
Revises: 0015_m4_quantity_offers
Create Date: 2026-09-19

Issue #89 PR-B, under ADR-0014. It is additive: no M0–M4 table, row or trigger is touched, and
nothing is backfilled. No marketplace request exists behind any of it.

**The account scope** (review 5255746944, blocker 2). ``seller_entities`` and
``marketplace_accounts`` are the canonical ``SellerEntity`` / ``MarketplaceAccount`` (Canonical v3.1
§9) with the ICBM-owned ``marketplace_account_id`` (``ACCOUNT_IDENTITY.md`` §2). An account is
inserted only while its marketplace connection's committed binding names the same provider
identity, one provider identity has one account per marketplace, and both tables are
append-only. Every registration row carries ``(marketplace_key, marketplace_account_id)`` as a
foreign key, and no Draft, Snapshot, Batch, Intent or override opens for an account that is not
bound to its committed identity. The provider's wire ``account_id`` names nothing here.

**Where invariants live.** The CHECK literals are frozen with this revision and a test compares
them with the ORM models. The triggers enforce the cross-row invariants:
- a draft item is exactly an M4 Item's identity, pinned to the exact PricingSnapshot of that Item
  in the Draft's marketplace and canonical account (§2, blocker 1); a Snapshot freezes its Draft's
  current revision and scope; each Item Snapshot is an open item of that Draft, with its exact M4
  identity, the Draft's pinned price and the membership and facts revisions of that price (§6);
- a ``SEPARATE_LISTINGS`` Snapshot holds exactly one Item, and a Snapshot is frozen once an Intent
  names it (§2, §12);
- an Intent opens only from a Snapshot of its Draft's current revision whose Items are still open
  with their pinned prices, and **a ``SINGLE_LISTING_WITH_OPTIONS`` Snapshot sends exactly every
  open Item of its Draft** (R3, blocker 3); ``SELECTED_OFFERS`` may send a chosen subset;
- **a new CREATE Intent never opens while a CREATE Intent that is ``SENT`` or ``UNKNOWN`` overlaps
  its marketplace × account × group or listing identity** (§10, R2). The registration store adds
  the merge and split lineage of those groups, fail-closed;
- an Intent moves only along the frozen transitions; an outcome is backed by its latest attempt,
  and leaving ``UNKNOWN`` needs a resolution backed by machine or provider evidence (§9, §10, B3);
- an attempt is appended in order, only for a ``PREPARED`` or proven-not-applied Intent, is
  finished once and resolved at most once (§9);
- a registration follows only its ``CONFIRMED`` Intent and the exact verification it passed (§11);
  it changes only by a newer read-back or a proven external absence (§14, R4);
- one active duplicate override per scope, revoked at most once (§13).

**What can change after a row is written.** A Draft's shape and revision; a draft item's removal;
an Intent's state, outcome and verification along its transitions; an attempt's finish and
resolution; a registration's read-back time and external absence; a registration item's current
group and binding; an override's revocation. Everything else rejects UPDATE, and every table
rejects DELETE.

**Downgrade fails closed.** It refuses while any of these tables holds a row: registration and
account truth is never silently destroyed.
"""

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_m5_registration_foundation"
down_revision: str | None = "0015_m4_quantity_offers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SELLERS = "seller_entities"
ACCOUNTS = "marketplace_accounts"
DRAFTS = "registration_drafts"
DRAFT_ITEMS = "registration_draft_items"
SNAPSHOTS = "registration_snapshots"
ITEM_SNAPSHOTS = "registration_item_snapshots"
BATCHES = "registration_batches"
INTENTS = "registration_intents"
ATTEMPTS = "registration_attempts"
REGISTRATIONS = "marketplace_registrations"
REGISTRATION_ITEMS = "marketplace_registration_items"
OVERRIDES = "duplicate_overrides"
# Creation order; dropped in reverse.
TABLES = (
    SELLERS,
    ACCOUNTS,
    DRAFTS,
    DRAFT_ITEMS,
    SNAPSHOTS,
    ITEM_SNAPSHOTS,
    BATCHES,
    INTENTS,
    ATTEMPTS,
    REGISTRATIONS,
    REGISTRATION_ITEMS,
    OVERRIDES,
)
FULLY_APPEND_ONLY = (SELLERS, ACCOUNTS, SNAPSHOTS, ITEM_SNAPSHOTS, BATCHES)
# Tables whose new rows open registration state in one canonical account's scope.
ACCOUNT_SCOPED = (DRAFTS, SNAPSHOTS, BATCHES, INTENTS, OVERRIDES)
CONNECTIONS = "marketplace_connections"
ITEMS = "product_items"
GROUPS = "product_groups"
MEMBERSHIP = "group_membership_revisions"
COMPOSITIONS = "listing_compositions"
REVISIONS = "product_facts_revisions"
PRICING = "pricing_snapshots"
BINDINGS = "source_bindings"

# Vocabularies frozen with this revision (app.register.model, app.core.errors).
_SHAPES = ("SINGLE_LISTING_WITH_OPTIONS", "SEPARATE_LISTINGS", "SELECTED_OFFERS")
_OPERATIONS = ("CREATE",)
_STATES = ("PREPARED", "SENT", "CONFIRMED", "UNKNOWN", "FAILED")
_OUTCOMES = ("APPLIED_PROVEN", "NOT_APPLIED_PROVEN", "UNKNOWN")
_VERIFICATIONS = ("NOT_VERIFIED", "PASS", "MISMATCH")
_LIFECYCLES = ("ACTIVE", "EXTERNALLY_REMOVED")
_RESOLVERS = ("READ_BACK", "LOOKUP", "USER")
_RESOLUTION_EVIDENCE = (
    "PROVIDER_READ_BACK",
    "PROVIDER_LOOKUP",
    "TRANSMISSION_PRECLUDED",
    "REVIEWED_MACHINE_PROOF",
)
_ABSENCE_EVIDENCE = ("PROVIDER_READ_BACK", "PROVIDER_LOOKUP")
_ERROR_CLASSES = (
    "TRANSIENT",
    "RATE_LIMITED",
    "AUTH",
    "VALIDATION",
    "POLICY_BLOCKED",
    "NOT_FOUND",
    "CONFLICT",
    "DUPLICATE",
    "REVIEW_REQUIRED",
    "FATAL",
    "UNKNOWN",
)


def _in(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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


def _check(table: str, expression: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expression, name=op.f(f"ck_{table}_{name}"))


def _fk(table: str, column: str, target: str, target_column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.{target_column}"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _account_fk(table: str) -> sa.ForeignKeyConstraint:
    """The canonical marketplace account that scopes a registration row."""
    return sa.ForeignKeyConstraint(
        ["marketplace_key", "marketplace_account_id"],
        [f"{ACCOUNTS}.marketplace_key", f"{ACCOUNTS}.marketplace_account_id"],
        name=op.f(f"fk_{table}_marketplace_key_{ACCOUNTS}"),
    )


def _trigger(name: str, event: str, table: str, body: str) -> None:
    op.execute(f"CREATE TRIGGER {name} BEFORE {event} ON {table} BEGIN {body} END")


def _raise(message: str, condition: str) -> str:
    return f"SELECT RAISE(ABORT, '{message}') WHERE {condition};"


def _unchanged(columns: Iterable[str]) -> str:
    return " AND ".join(f"NEW.{column} IS OLD.{column}" for column in columns)


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
_REMOVED = (
    "(lifecycle_state = 'EXTERNALLY_REMOVED') = (absence_observed_at IS NOT NULL)"
    " AND (absence_observed_at IS NULL) = (absence_evidence_kind IS NULL)"
    " AND (absence_observed_at IS NULL) = (absence_evidence_digest IS NULL)"
    " AND (absence_observed_at IS NULL) = (absence_recorded_by IS NULL)"
)
_ACCOUNT_ID_FORMAT = (
    "length(marketplace_account_id) = 36 AND substr(marketplace_account_id, 1, 4) = 'mpa-'"
    " AND substr(marketplace_account_id, 5) NOT GLOB '*[^0-9a-f]*'"
)


def upgrade() -> None:
    _create_accounts()
    _create_drafts()
    _create_snapshots()
    _create_execution()
    _create_registrations()
    _create_overrides()
    _install_triggers()


def _create_accounts() -> None:
    op.create_table(
        SELLERS,
        sa.Column("seller_entity_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(SELLERS, "created_by <> ''", "created_by_present"),
        _check(SELLERS, "correlation_id <> ''", "correlation_present"),
        sa.PrimaryKeyConstraint("seller_entity_id", name=op.f(f"pk_{SELLERS}")),
    )
    op.create_table(
        ACCOUNTS,
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("seller_entity_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("provider_account_uid", sa.String(length=100), nullable=False),
        sa.Column("established_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("established_at", sa.DateTime(), nullable=False),
        _check(ACCOUNTS, _ACCOUNT_ID_FORMAT, "account_id_format"),
        _check(ACCOUNTS, "provider_account_uid <> ''", "provider_identity_present"),
        _check(ACCOUNTS, "established_by <> ''", "established_by_present"),
        _check(ACCOUNTS, "correlation_id <> ''", "correlation_present"),
        _fk(ACCOUNTS, "seller_entity_id", SELLERS, "seller_entity_id"),
        _fk(ACCOUNTS, "marketplace_key", CONNECTIONS, "marketplace_key"),
        sa.PrimaryKeyConstraint("marketplace_account_id", name=op.f(f"pk_{ACCOUNTS}")),
        _unique(ACCOUNTS, "marketplace_key", "marketplace_account_id"),
        _unique(ACCOUNTS, "marketplace_key", "provider_account_uid"),
    )


def _create_drafts() -> None:
    op.create_table(
        DRAFTS,
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("listing_shape", sa.String(length=40), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        _check(DRAFTS, _present("marketplace_key"), "marketplace_key_present"),
        _check(DRAFTS, _in("listing_shape", _SHAPES), "listing_shape_valid"),
        _check(DRAFTS, "draft_revision >= 1", "draft_revision_positive"),
        _check(DRAFTS, _present("created_by"), "created_by_present"),
        _account_fk(DRAFTS),
        sa.PrimaryKeyConstraint("draft_id", name=op.f(f"pk_{DRAFTS}")),
    )
    op.create_table(
        DRAFT_ITEMS,
        sa.Column("draft_item_id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("composition_signature", sa.String(length=64), nullable=False),
        sa.Column("pricing_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("added_by", sa.String(length=64), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.Column("removed_by", sa.String(length=64), nullable=True),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        _check(DRAFT_ITEMS, _hex64("composition_signature"), "signature_hex"),
        _check(DRAFT_ITEMS, "ordinal >= 0", "ordinal_non_negative"),
        _check(DRAFT_ITEMS, _present("added_by"), "added_by_present"),
        _check(DRAFT_ITEMS, "(removed_at IS NULL) = (removed_by IS NULL)", "removal_recorded"),
        _check(DRAFT_ITEMS, "removed_by IS NULL OR removed_by <> ''", "removed_by_present"),
        _check(DRAFT_ITEMS, "removed_at IS NULL OR removed_at >= added_at", "removal_ordered"),
        _fk(DRAFT_ITEMS, "draft_id", DRAFTS, "draft_id"),
        _fk(DRAFT_ITEMS, "item_id", ITEMS, "item_id"),
        _fk(DRAFT_ITEMS, "product_group_id", GROUPS, "product_group_id"),
        _fk(DRAFT_ITEMS, "pricing_snapshot_id", PRICING, "pricing_snapshot_id"),
        sa.PrimaryKeyConstraint("draft_item_id", name=op.f(f"pk_{DRAFT_ITEMS}")),
    )
    for name, columns in (
        ("ux_registration_draft_items_open_item", ["draft_id", "item_id"]),
        (
            "ux_registration_draft_items_open_key",
            ["draft_id", "product_group_id", "composition_signature"],
        ),
        ("ux_registration_draft_items_open_ordinal", ["draft_id", "ordinal"]),
    ):
        op.create_index(
            name, DRAFT_ITEMS, columns, unique=True, sqlite_where=sa.text("removed_at IS NULL")
        )


def _create_snapshots() -> None:
    op.create_table(
        SNAPSHOTS,
        sa.Column("registration_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("listing_shape", sa.String(length=40), nullable=False),
        sa.Column("listing_identity", sa.String(length=64), nullable=False),
        sa.Column("preflight_rule_version", sa.String(length=64), nullable=False),
        sa.Column("preflight_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("category_mapping_revision", sa.String(length=64), nullable=False),
        sa.Column("taxonomy_revision", sa.String(length=64), nullable=False),
        sa.Column("policy_revisions_json", sa.Text(), nullable=False),
        sa.Column("detail_composition_revision", sa.String(length=64), nullable=False),
        sa.Column("sanitizer_profile_version", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(SNAPSHOTS, _present("marketplace_key"), "marketplace_key_present"),
        _account_fk(SNAPSHOTS),
        _check(SNAPSHOTS, _in("listing_shape", _SHAPES), "listing_shape_valid"),
        _check(SNAPSHOTS, "draft_revision >= 1", "draft_revision_positive"),
        _check(SNAPSHOTS, _listing_identity("listing_identity"), "listing_identity_format"),
        _check(SNAPSHOTS, _present("preflight_rule_version"), "preflight_rule_version_present"),
        _check(SNAPSHOTS, _hex64("preflight_fingerprint"), "preflight_fingerprint_hex"),
        _check(
            SNAPSHOTS, _present("category_mapping_revision"), "category_mapping_revision_present"
        ),
        _check(SNAPSHOTS, _present("taxonomy_revision"), "taxonomy_revision_present"),
        _check(SNAPSHOTS, _json_object("policy_revisions_json"), "policy_revisions_is_object"),
        _check(
            SNAPSHOTS,
            _present("detail_composition_revision"),
            "detail_composition_revision_present",
        ),
        _check(
            SNAPSHOTS, _present("sanitizer_profile_version"), "sanitizer_profile_version_present"
        ),
        _check(SNAPSHOTS, _hex64("payload_hash"), "payload_hash_hex"),
        _check(SNAPSHOTS, _json_object("payload_json"), "payload_is_object"),
        _check(SNAPSHOTS, _present("created_by"), "created_by_present"),
        _check(SNAPSHOTS, _present("correlation_id"), "correlation_present"),
        _fk(SNAPSHOTS, "draft_id", DRAFTS, "draft_id"),
        sa.PrimaryKeyConstraint("registration_snapshot_id", name=op.f(f"pk_{SNAPSHOTS}")),
    )
    op.create_table(
        ITEM_SNAPSHOTS,
        sa.Column("item_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("registration_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("registration_item_key", sa.String(length=40), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.String(length=36), nullable=False),
        sa.Column("group_id_at_registration", sa.String(length=36), nullable=False),
        sa.Column("group_membership_revision_id", sa.String(length=36), nullable=False),
        sa.Column("listing_composition_id", sa.String(length=36), nullable=False),
        sa.Column("composition_signature", sa.String(length=64), nullable=False),
        sa.Column(
            "source_product_facts_revision_id_at_registration",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("pricing_snapshot_id_at_registration", sa.String(length=36), nullable=False),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("publication_assets_json", sa.Text(), nullable=False),
        sa.Column("outbound_values_json", sa.Text(), nullable=False),
        _check(ITEM_SNAPSHOTS, _item_key("registration_item_key"), "item_key_format"),
        _check(ITEM_SNAPSHOTS, "ordinal >= 0", "ordinal_non_negative"),
        _check(ITEM_SNAPSHOTS, _hex64("composition_signature"), "signature_hex"),
        _check(ITEM_SNAPSHOTS, _json_object("source_snapshot_json"), "source_snapshot_is_object"),
        _check(
            ITEM_SNAPSHOTS,
            _json_array("publication_assets_json", minimum=1),
            "publication_assets_present",
        ),
        _check(ITEM_SNAPSHOTS, _json_object("outbound_values_json"), "outbound_values_is_object"),
        _fk(ITEM_SNAPSHOTS, "registration_snapshot_id", SNAPSHOTS, "registration_snapshot_id"),
        _fk(ITEM_SNAPSHOTS, "item_id", ITEMS, "item_id"),
        _fk(ITEM_SNAPSHOTS, "group_id_at_registration", GROUPS, "product_group_id"),
        _fk(ITEM_SNAPSHOTS, "listing_composition_id", COMPOSITIONS, "composition_id"),
        _fk(
            ITEM_SNAPSHOTS,
            "source_product_facts_revision_id_at_registration",
            REVISIONS,
            "revision_id",
        ),
        _fk(ITEM_SNAPSHOTS, "pricing_snapshot_id_at_registration", PRICING, "pricing_snapshot_id"),
        sa.PrimaryKeyConstraint("item_snapshot_id", name=op.f(f"pk_{ITEM_SNAPSHOTS}")),
        _unique(ITEM_SNAPSHOTS, "registration_snapshot_id", "registration_item_key"),
        _unique(ITEM_SNAPSHOTS, "registration_snapshot_id", "item_id"),
        _unique(ITEM_SNAPSHOTS, "registration_snapshot_id", "ordinal"),
    )


def _create_execution() -> None:
    op.create_table(
        BATCHES,
        sa.Column("registration_batch_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(BATCHES, _present("marketplace_key"), "marketplace_key_present"),
        _account_fk(BATCHES),
        _check(BATCHES, _present("created_by"), "created_by_present"),
        _check(BATCHES, _present("correlation_id"), "correlation_present"),
        sa.PrimaryKeyConstraint("registration_batch_id", name=op.f(f"pk_{BATCHES}")),
    )
    op.create_table(
        INTENTS,
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("registration_batch_id", sa.String(length=36), nullable=False),
        sa.Column("registration_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("remote_outcome", sa.String(length=20), nullable=True),
        sa.Column("marketplace_product_id", sa.String(length=64), nullable=True),
        sa.Column("verification_state", sa.String(length=20), nullable=False),
        sa.Column("comparison_contract_version", sa.String(length=64), nullable=True),
        sa.Column("normalizer_version", sa.String(length=64), nullable=True),
        sa.Column("verification_evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        _check(INTENTS, _present("marketplace_key"), "marketplace_key_present"),
        _account_fk(INTENTS),
        _check(INTENTS, _in("operation", _OPERATIONS), "operation_create_only"),
        _check(INTENTS, _hex64("idempotency_key"), "idempotency_key_hex"),
        _check(INTENTS, _in("state", _STATES), "state_valid"),
        _check(
            INTENTS,
            f"remote_outcome IS NULL OR {_in('remote_outcome', _OUTCOMES)}",
            "remote_outcome_valid",
        ),
        _check(INTENTS, _in("verification_state", _VERIFICATIONS), "verification_valid"),
        _check(INTENTS, _STATE_AGREES, "state_agrees_with_outcome"),
        _check(
            INTENTS,
            "(marketplace_product_id IS NOT NULL) = (remote_outcome IS 'APPLIED_PROVEN')",
            "provider_identity_when_applied",
        ),
        _check(
            INTENTS,
            "marketplace_product_id IS NULL OR marketplace_product_id <> ''",
            "provider_identity_present",
        ),
        _check(INTENTS, _VERIFIED, "verification_recorded"),
        _check(INTENTS, _present("created_by"), "created_by_present"),
        _check(INTENTS, _present("correlation_id"), "correlation_present"),
        _fk(INTENTS, "registration_batch_id", BATCHES, "registration_batch_id"),
        _fk(INTENTS, "registration_snapshot_id", SNAPSHOTS, "registration_snapshot_id"),
        sa.PrimaryKeyConstraint("intent_id", name=op.f(f"pk_{INTENTS}")),
        _unique(INTENTS, "idempotency_key"),
        _unique(INTENTS, "registration_snapshot_id", "operation"),
    )
    op.create_index(
        "ix_registration_intents_scope",
        INTENTS,
        ["marketplace_key", "marketplace_account_id", "state"],
    )
    op.create_table(
        ATTEMPTS,
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("sanitizer_profile_version", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_digest", sa.String(length=64), nullable=True),
        sa.Column("error_class", sa.String(length=20), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("remote_outcome", sa.String(length=20), nullable=True),
        sa.Column("resolved_outcome", sa.String(length=20), nullable=True),
        sa.Column("resolved_by", sa.String(length=20), nullable=True),
        sa.Column("resolution_evidence_kind", sa.String(length=30), nullable=True),
        sa.Column("resolution_evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        _check(ATTEMPTS, "attempt_no >= 1", "attempt_no_positive"),
        _check(ATTEMPTS, _hex64("request_payload_hash"), "request_payload_hash_hex"),
        _check(
            ATTEMPTS, _present("sanitizer_profile_version"), "sanitizer_profile_version_present"
        ),
        _check(ATTEMPTS, _OPEN_IS_BARE, "open_attempt_is_bare"),
        _check(
            ATTEMPTS, "finished_at IS NULL OR remote_outcome IS NOT NULL", "finished_has_outcome"
        ),
        _check(ATTEMPTS, "finished_at IS NULL OR finished_at >= started_at", "finish_ordered"),
        _check(
            ATTEMPTS,
            f"remote_outcome IS NULL OR {_in('remote_outcome', _OUTCOMES)}",
            "remote_outcome_valid",
        ),
        _check(
            ATTEMPTS,
            f"error_class IS NULL OR {_in('error_class', _ERROR_CLASSES)}",
            "error_class_valid",
        ),
        _check(ATTEMPTS, "error_code IS NULL OR error_code <> ''", "error_code_present"),
        _check(
            ATTEMPTS,
            f"response_digest IS NULL OR ({_hex64('response_digest')})",
            "response_digest_hex",
        ),
        _check(ATTEMPTS, _RESOLUTION_COMPLETE, "resolution_complete"),
        _check(
            ATTEMPTS,
            "resolved_outcome IS NULL OR (remote_outcome = 'UNKNOWN'"
            " AND resolved_outcome IN ('APPLIED_PROVEN', 'NOT_APPLIED_PROVEN'))",
            "resolves_only_unknown",
        ),
        _check(
            ATTEMPTS,
            f"resolved_by IS NULL OR {_in('resolved_by', _RESOLVERS)}",
            "resolved_by_valid",
        ),
        _check(
            ATTEMPTS,
            "resolution_evidence_kind IS NULL"
            f" OR {_in('resolution_evidence_kind', _RESOLUTION_EVIDENCE)}",
            "resolution_evidence_valid",
        ),
        _check(
            ATTEMPTS,
            f"resolution_evidence_digest IS NULL OR ({_hex64('resolution_evidence_digest')})",
            "resolution_evidence_digest_hex",
        ),
        _check(
            ATTEMPTS,
            "(resolved_by IS NOT 'READ_BACK' OR resolution_evidence_kind = 'PROVIDER_READ_BACK')"
            " AND (resolved_by IS NOT 'LOOKUP' OR resolution_evidence_kind = 'PROVIDER_LOOKUP')",
            "resolver_matches_evidence",
        ),
        _check(
            ATTEMPTS,
            "resolution_evidence_kind IS NOT 'TRANSMISSION_PRECLUDED'"
            " OR resolved_outcome = 'NOT_APPLIED_PROVEN'",
            "precluded_proves_absence_only",
        ),
        _fk(ATTEMPTS, "intent_id", INTENTS, "intent_id"),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f(f"pk_{ATTEMPTS}")),
        _unique(ATTEMPTS, "intent_id", "attempt_no"),
    )
    op.create_index(
        "ux_registration_attempts_one_open",
        ATTEMPTS,
        ["intent_id"],
        unique=True,
        sqlite_where=sa.text("finished_at IS NULL"),
    )


def _create_registrations() -> None:
    op.create_table(
        REGISTRATIONS,
        sa.Column("registration_id", sa.String(length=36), nullable=False),
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("registration_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("marketplace_product_id", sa.String(length=64), nullable=False),
        sa.Column("seller_product_code", sa.String(length=64), nullable=False),
        sa.Column("published_state", sa.String(length=40), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=30), nullable=False),
        sa.Column("comparison_contract_version", sa.String(length=64), nullable=False),
        sa.Column("normalizer_version", sa.String(length=64), nullable=False),
        sa.Column("readback_evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.Column("last_readback_at", sa.DateTime(), nullable=False),
        sa.Column("absence_observed_at", sa.DateTime(), nullable=True),
        sa.Column("absence_evidence_kind", sa.String(length=30), nullable=True),
        sa.Column("absence_evidence_digest", sa.String(length=64), nullable=True),
        sa.Column("absence_recorded_by", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(REGISTRATIONS, _present("marketplace_key"), "marketplace_key_present"),
        _account_fk(REGISTRATIONS),
        _check(REGISTRATIONS, _present("marketplace_product_id"), "provider_identity_present"),
        _check(REGISTRATIONS, _listing_identity("seller_product_code"), "seller_code_format"),
        _check(REGISTRATIONS, _present("published_state"), "published_state_present"),
        _check(REGISTRATIONS, _in("lifecycle_state", _LIFECYCLES), "lifecycle_valid"),
        _check(
            REGISTRATIONS,
            _present("comparison_contract_version"),
            "comparison_contract_version_present",
        ),
        _check(REGISTRATIONS, _present("normalizer_version"), "normalizer_version_present"),
        _check(REGISTRATIONS, _hex64("readback_evidence_digest"), "readback_evidence_digest_hex"),
        _check(REGISTRATIONS, "last_readback_at >= verified_at", "readback_ordered"),
        _check(REGISTRATIONS, _REMOVED, "absence_recorded"),
        _check(
            REGISTRATIONS,
            f"absence_evidence_kind IS NULL OR {_in('absence_evidence_kind', _ABSENCE_EVIDENCE)}",
            "absence_evidence_valid",
        ),
        _check(
            REGISTRATIONS,
            f"absence_evidence_digest IS NULL OR ({_hex64('absence_evidence_digest')})",
            "absence_evidence_digest_hex",
        ),
        _check(
            REGISTRATIONS,
            "absence_recorded_by IS NULL OR absence_recorded_by <> ''",
            "absence_recorded_by_present",
        ),
        _check(REGISTRATIONS, _present("created_by"), "created_by_present"),
        _check(REGISTRATIONS, _present("correlation_id"), "correlation_present"),
        _fk(REGISTRATIONS, "intent_id", INTENTS, "intent_id"),
        _fk(REGISTRATIONS, "registration_snapshot_id", SNAPSHOTS, "registration_snapshot_id"),
        sa.PrimaryKeyConstraint("registration_id", name=op.f(f"pk_{REGISTRATIONS}")),
        _unique(REGISTRATIONS, "intent_id"),
        _unique(
            REGISTRATIONS, "marketplace_key", "marketplace_account_id", "marketplace_product_id"
        ),
    )
    op.create_table(
        REGISTRATION_ITEMS,
        sa.Column("registration_item_id", sa.String(length=36), nullable=False),
        sa.Column("registration_id", sa.String(length=36), nullable=False),
        sa.Column("item_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("registration_item_key", sa.String(length=40), nullable=False),
        sa.Column("current_group_id", sa.String(length=36), nullable=False),
        sa.Column("listing_composition_id", sa.String(length=36), nullable=False),
        sa.Column("current_source_binding_id", sa.String(length=36), nullable=True),
        sa.Column("marketplace_option_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _check(REGISTRATION_ITEMS, _item_key("registration_item_key"), "item_key_format"),
        _check(
            REGISTRATION_ITEMS,
            "marketplace_option_id IS NULL OR marketplace_option_id <> ''",
            "option_identity_present",
        ),
        _fk(REGISTRATION_ITEMS, "registration_id", REGISTRATIONS, "registration_id"),
        _fk(REGISTRATION_ITEMS, "item_snapshot_id", ITEM_SNAPSHOTS, "item_snapshot_id"),
        _fk(REGISTRATION_ITEMS, "current_group_id", GROUPS, "product_group_id"),
        _fk(REGISTRATION_ITEMS, "listing_composition_id", COMPOSITIONS, "composition_id"),
        _fk(REGISTRATION_ITEMS, "current_source_binding_id", BINDINGS, "binding_id"),
        sa.PrimaryKeyConstraint("registration_item_id", name=op.f(f"pk_{REGISTRATION_ITEMS}")),
        _unique(REGISTRATION_ITEMS, "registration_id", "registration_item_key"),
        _unique(REGISTRATION_ITEMS, "item_snapshot_id"),
    )


def _create_overrides() -> None:
    op.create_table(
        OVERRIDES,
        sa.Column("override_id", sa.String(length=36), nullable=False),
        sa.Column("marketplace_key", sa.String(length=40), nullable=False),
        sa.Column("marketplace_account_id", sa.String(length=40), nullable=False),
        sa.Column("product_group_id", sa.String(length=36), nullable=False),
        sa.Column("listing_composition_id", sa.String(length=36), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=64), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        _check(OVERRIDES, _present("marketplace_key"), "marketplace_key_present"),
        _account_fk(OVERRIDES),
        _check(OVERRIDES, _present("reason"), "reason_present"),
        _check(OVERRIDES, _present("approved_by"), "approved_by_present"),
        _check(OVERRIDES, _present("correlation_id"), "correlation_present"),
        _check(
            OVERRIDES,
            "(revoked_at IS NULL) = (revoked_by IS NULL)"
            " AND (revoked_at IS NULL) = (revoke_reason IS NULL)",
            "revocation_recorded",
        ),
        _check(
            OVERRIDES,
            "revoke_reason IS NULL OR (revoke_reason <> '' AND revoked_by <> '')",
            "revocation_present",
        ),
        _fk(OVERRIDES, "product_group_id", GROUPS, "product_group_id"),
        _fk(OVERRIDES, "listing_composition_id", COMPOSITIONS, "composition_id"),
        sa.PrimaryKeyConstraint("override_id", name=op.f(f"pk_{OVERRIDES}")),
    )


def _install_triggers() -> None:
    for table in FULLY_APPEND_ONLY:
        _trigger(f"trg_{table}_no_update", "UPDATE", table, _raise(f"{table} is immutable", "1"))
    for table in TABLES:
        _trigger(
            f"trg_{table}_no_delete", "DELETE", table, _raise(f"{table} is never deleted", "1")
        )
    _install_account_rules()
    _install_draft_rules()
    _install_snapshot_rules()
    _install_intent_rules()
    _install_attempt_rules()
    _install_registration_rules()
    _install_override_rules()


def _install_account_rules() -> None:
    # ACCOUNT_IDENTITY §5: an account is established only from a complete committed binding (the
    # 0006 CHECK makes a non-null provider identity a complete binding unit).
    _trigger(
        f"trg_{ACCOUNTS}_bound",
        "INSERT",
        ACCOUNTS,
        _raise(
            f"{ACCOUNTS}: an account is established only from its committed binding",
            f"NOT EXISTS (SELECT 1 FROM {CONNECTIONS} c"
            " WHERE c.marketplace_key = NEW.marketplace_key"
            " AND c.provider_account_uid = NEW.provider_account_uid)",
        ),
    )
    # ACCOUNT_IDENTITY §4: no registration state opens for an unbound or mismatched account.
    unbound = (
        f"NOT EXISTS (SELECT 1 FROM {ACCOUNTS} a JOIN {CONNECTIONS} c"
        " ON c.marketplace_key = a.marketplace_key"
        " WHERE a.marketplace_account_id = NEW.marketplace_account_id"
        " AND a.marketplace_key = NEW.marketplace_key"
        " AND c.provider_account_uid = a.provider_account_uid)"
    )
    for table in ACCOUNT_SCOPED:
        _trigger(
            f"trg_{table}_account_bound",
            "INSERT",
            table,
            _raise(f"{table}: the marketplace account is not bound to its identity", unbound),
        )


def _install_draft_rules() -> None:
    _trigger(
        f"trg_{DRAFTS}_update",
        "UPDATE",
        DRAFTS,
        _raise(
            f"{DRAFTS}: the draft identity and scope are immutable",
            "NOT ("
            + _unchanged(("draft_id", "marketplace_key", "marketplace_account_id", "created_by"))
            + " AND NEW.created_at IS OLD.created_at)",
        )
        + _raise(
            f"{DRAFTS}: every change advances the revision by one",
            "NEW.draft_revision <> OLD.draft_revision + 1",
        ),
    )
    _trigger(
        f"trg_{DRAFT_ITEMS}_identity",
        "INSERT",
        DRAFT_ITEMS,
        _raise(
            f"{DRAFT_ITEMS}: the group and signature are the Item identity",
            f"NOT EXISTS (SELECT 1 FROM {ITEMS} i WHERE i.item_id = NEW.item_id"
            " AND i.product_group_id = NEW.product_group_id"
            " AND i.composition_signature = NEW.composition_signature)",
        )
        + _raise(
            f"{DRAFT_ITEMS}: the price is the exact M4 snapshot of this Item and draft target",
            f"NOT EXISTS (SELECT 1 FROM {PRICING} p JOIN {DRAFTS} d ON d.draft_id = NEW.draft_id"
            " WHERE p.pricing_snapshot_id = NEW.pricing_snapshot_id AND p.item_id = NEW.item_id"
            " AND p.marketplace_key = d.marketplace_key"
            " AND (p.account_id IS NULL OR p.account_id = d.marketplace_account_id))",
        )
        + _raise(
            f"{DRAFT_ITEMS}: an item is added open",
            "NEW.removed_at IS NOT NULL OR NEW.removed_by IS NOT NULL",
        ),
    )
    _trigger(
        f"trg_{DRAFT_ITEMS}_remove_only",
        "UPDATE",
        DRAFT_ITEMS,
        _raise(
            f"{DRAFT_ITEMS}: a draft item is only ever removed",
            "NOT (OLD.removed_at IS NULL AND NEW.removed_at IS NOT NULL AND "
            + _unchanged(
                (
                    "draft_item_id",
                    "draft_id",
                    "item_id",
                    "product_group_id",
                    "composition_signature",
                    "pricing_snapshot_id",
                    "ordinal",
                    "added_by",
                    "added_at",
                )
            )
            + ")",
        ),
    )


def _install_snapshot_rules() -> None:
    this_snapshot = f"(SELECT %s FROM {SNAPSHOTS} WHERE registration_snapshot_id = NEW.%s)"
    _trigger(
        f"trg_{SNAPSHOTS}_draft",
        "INSERT",
        SNAPSHOTS,
        _raise(
            f"{SNAPSHOTS}: the snapshot scope is its draft scope",
            f"NOT EXISTS (SELECT 1 FROM {DRAFTS} d WHERE d.draft_id = NEW.draft_id"
            " AND d.marketplace_key = NEW.marketplace_key"
            " AND d.marketplace_account_id = NEW.marketplace_account_id"
            " AND d.listing_shape = NEW.listing_shape)",
        )
        + _raise(
            f"{SNAPSHOTS}: a snapshot freezes the current draft revision",
            f"NEW.draft_revision IS NOT (SELECT draft_revision FROM {DRAFTS}"
            " WHERE draft_id = NEW.draft_id)",
        ),
    )
    _trigger(
        f"trg_{ITEM_SNAPSHOTS}_exact",
        "INSERT",
        ITEM_SNAPSHOTS,
        _raise(
            f"{ITEM_SNAPSHOTS}: a snapshot named by an intent is frozen",
            f"EXISTS (SELECT 1 FROM {INTENTS}"
            " WHERE registration_snapshot_id = NEW.registration_snapshot_id)",
        )
        + _raise(
            f"{ITEM_SNAPSHOTS}: the item is an open item of the snapshot draft",
            f"NOT EXISTS (SELECT 1 FROM {DRAFT_ITEMS} d JOIN {SNAPSHOTS} s"
            " ON s.draft_id = d.draft_id WHERE s.registration_snapshot_id"
            " = NEW.registration_snapshot_id AND d.item_id = NEW.item_id"
            " AND d.removed_at IS NULL)",
        )
        + _raise(
            f"{ITEM_SNAPSHOTS}: group, composition and signature are the Item identity",
            f"NOT EXISTS (SELECT 1 FROM {ITEMS} i WHERE i.item_id = NEW.item_id"
            " AND i.product_group_id = NEW.group_id_at_registration"
            " AND i.composition_id = NEW.listing_composition_id"
            " AND i.composition_signature = NEW.composition_signature)",
        )
        + _raise(
            f"{ITEM_SNAPSHOTS}: the membership revision belongs to the group",
            f"NOT EXISTS (SELECT 1 FROM {MEMBERSHIP} m"
            " WHERE m.membership_revision_id = NEW.group_membership_revision_id"
            " AND m.product_group_id = NEW.group_id_at_registration)",
        )
        + _raise(
            f"{ITEM_SNAPSHOTS}: the price is the pinned exact M4 snapshot of this Item and target",
            f"NOT EXISTS (SELECT 1 FROM {DRAFT_ITEMS} d JOIN {SNAPSHOTS} s"
            f" ON s.draft_id = d.draft_id JOIN {PRICING} p"
            " ON p.pricing_snapshot_id = d.pricing_snapshot_id"
            " WHERE s.registration_snapshot_id = NEW.registration_snapshot_id"
            " AND d.item_id = NEW.item_id AND d.removed_at IS NULL"
            " AND d.pricing_snapshot_id = NEW.pricing_snapshot_id_at_registration"
            " AND p.item_id = NEW.item_id AND p.marketplace_key = s.marketplace_key"
            " AND (p.account_id IS NULL OR p.account_id = s.marketplace_account_id)"
            " AND p.membership_revision_id = NEW.group_membership_revision_id"
            " AND p.source_product_facts_revision_id"
            " = NEW.source_product_facts_revision_id_at_registration)",
        )
        + _raise(
            f"{ITEM_SNAPSHOTS}: a separate listing holds exactly one Item",
            this_snapshot
            % ("listing_shape", "registration_snapshot_id")
            + " = 'SEPARATE_LISTINGS' AND EXISTS (SELECT 1 FROM "
            f"{ITEM_SNAPSHOTS} WHERE registration_snapshot_id = NEW.registration_snapshot_id)",
        ),
    )


def _install_intent_rules() -> None:
    new_listing = (
        f"(SELECT listing_identity FROM {SNAPSHOTS}"
        " WHERE registration_snapshot_id = NEW.registration_snapshot_id)"
    )
    overlapping_unresolved = (
        f"EXISTS (SELECT 1 FROM {INTENTS} o JOIN {SNAPSHOTS} os"
        " ON os.registration_snapshot_id = o.registration_snapshot_id"
        " WHERE o.operation = 'CREATE' AND o.marketplace_key = NEW.marketplace_key"
        " AND o.marketplace_account_id = NEW.marketplace_account_id"
        " AND o.state IN ('SENT', 'UNKNOWN')"
        f" AND (os.listing_identity = {new_listing}"
        f" OR EXISTS (SELECT 1 FROM {ITEM_SNAPSHOTS} oi JOIN {ITEM_SNAPSHOTS} ni"
        " ON ni.group_id_at_registration = oi.group_id_at_registration"
        " WHERE oi.registration_snapshot_id = o.registration_snapshot_id"
        " AND ni.registration_snapshot_id = NEW.registration_snapshot_id)))"
    )
    _trigger(
        f"trg_{INTENTS}_open",
        "INSERT",
        INTENTS,
        _raise(f"{INTENTS}: an intent opens PREPARED", "NEW.state <> 'PREPARED'")
        + _raise(
            f"{INTENTS}: the intent scope is its snapshot scope",
            f"NOT EXISTS (SELECT 1 FROM {SNAPSHOTS} s"
            " WHERE s.registration_snapshot_id = NEW.registration_snapshot_id"
            " AND s.marketplace_key = NEW.marketplace_key"
            " AND s.marketplace_account_id = NEW.marketplace_account_id)",
        )
        + _raise(
            f"{INTENTS}: the intent scope is its batch scope",
            f"NOT EXISTS (SELECT 1 FROM {BATCHES} b"
            " WHERE b.registration_batch_id = NEW.registration_batch_id"
            " AND b.marketplace_key = NEW.marketplace_key"
            " AND b.marketplace_account_id = NEW.marketplace_account_id)",
        )
        + _raise(
            f"{INTENTS}: a snapshot sends at least one Item",
            f"NOT EXISTS (SELECT 1 FROM {ITEM_SNAPSHOTS}"
            " WHERE registration_snapshot_id = NEW.registration_snapshot_id)",
        )
        + _raise(
            f"{INTENTS}: an intent opens from a snapshot of the current draft revision",
            f"NOT EXISTS (SELECT 1 FROM {SNAPSHOTS} s JOIN {DRAFTS} d ON d.draft_id = s.draft_id"
            " WHERE s.registration_snapshot_id = NEW.registration_snapshot_id"
            " AND s.draft_revision = d.draft_revision AND s.listing_shape = d.listing_shape)",
        )
        + _raise(
            f"{INTENTS}: every sent Item is still open with its pinned price",
            f"EXISTS (SELECT 1 FROM {ITEM_SNAPSHOTS} i JOIN {SNAPSHOTS} s"
            " ON s.registration_snapshot_id = i.registration_snapshot_id"
            " WHERE i.registration_snapshot_id = NEW.registration_snapshot_id"
            f" AND NOT EXISTS (SELECT 1 FROM {DRAFT_ITEMS} d WHERE d.draft_id = s.draft_id"
            " AND d.item_id = i.item_id AND d.removed_at IS NULL"
            " AND d.pricing_snapshot_id = i.pricing_snapshot_id_at_registration))",
        )
        + _raise(
            f"{INTENTS}: a single listing sends every open Item of its draft",
            f"EXISTS (SELECT 1 FROM {SNAPSHOTS} s JOIN {DRAFT_ITEMS} d ON d.draft_id = s.draft_id"
            " WHERE s.registration_snapshot_id = NEW.registration_snapshot_id"
            " AND s.listing_shape = 'SINGLE_LISTING_WITH_OPTIONS' AND d.removed_at IS NULL"
            f" AND NOT EXISTS (SELECT 1 FROM {ITEM_SNAPSHOTS} i"
            " WHERE i.registration_snapshot_id = s.registration_snapshot_id"
            " AND i.item_id = d.item_id"
            " AND i.pricing_snapshot_id_at_registration = d.pricing_snapshot_id))",
        )
        + _raise(
            f"{INTENTS}: an unresolved CREATE blocks its conflict scope",
            f"NEW.operation = 'CREATE' AND {overlapping_unresolved}",
        ),
    )
    latest = f"(SELECT MAX(attempt_no) FROM {ATTEMPTS} WHERE intent_id = NEW.intent_id)"
    backed = (
        f"(NOT EXISTS (SELECT 1 FROM {ATTEMPTS} WHERE intent_id = NEW.intent_id)"
        " AND NEW.remote_outcome = 'NOT_APPLIED_PROVEN')"
        f" OR EXISTS (SELECT 1 FROM {ATTEMPTS} a WHERE a.intent_id = NEW.intent_id"
        f" AND a.attempt_no = {latest} AND a.finished_at IS NOT NULL"
        " AND COALESCE(a.resolved_outcome, a.remote_outcome) = NEW.remote_outcome)"
    )
    _trigger(
        f"trg_{INTENTS}_transition",
        "UPDATE",
        INTENTS,
        _raise(
            f"{INTENTS}: the intent identity is immutable",
            "NOT ("
            + _unchanged(
                (
                    "intent_id",
                    "registration_batch_id",
                    "registration_snapshot_id",
                    "marketplace_key",
                    "marketplace_account_id",
                    "operation",
                    "idempotency_key",
                    "created_by",
                    "correlation_id",
                    "created_at",
                )
            )
            + ")",
        )
        + _raise(
            f"{INTENTS}: state transition not allowed",
            "NOT ((OLD.state = 'PREPARED' AND NEW.state IN ('SENT', 'FAILED'))"
            " OR (OLD.state = 'SENT' AND NEW.state IN ('SENT', 'CONFIRMED', 'UNKNOWN', 'FAILED'))"
            " OR (OLD.state = 'UNKNOWN' AND NEW.state IN ('SENT', 'FAILED'))"
            " OR (OLD.state = 'FAILED' AND NEW.state = 'SENT'))",
        )
        + _raise(
            f"{INTENTS}: an applied outcome and its provider identity never change",
            "OLD.remote_outcome = 'APPLIED_PROVEN' AND (NEW.remote_outcome IS NOT 'APPLIED_PROVEN'"
            " OR NEW.marketplace_product_id IS NOT OLD.marketplace_product_id)",
        )
        + _raise(
            f"{INTENTS}: a recorded verification is never withdrawn",
            "OLD.verification_state <> 'NOT_VERIFIED' AND NEW.verification_state = 'NOT_VERIFIED'",
        )
        + _raise(
            f"{INTENTS}: sending needs one open attempt",
            "NEW.state = 'SENT' AND OLD.state IN ('PREPARED', 'FAILED') AND NOT EXISTS ("
            f"SELECT 1 FROM {ATTEMPTS} WHERE intent_id = NEW.intent_id AND finished_at IS NULL)",
        )
        + _raise(
            f"{INTENTS}: an outcome is backed by the latest attempt",
            "NEW.remote_outcome IS NOT NULL AND NEW.remote_outcome IS NOT OLD.remote_outcome"
            f" AND NOT ({backed})",
        )
        + _raise(
            f"{INTENTS}: leaving UNKNOWN needs a resolution backed by evidence",
            "OLD.state = 'UNKNOWN' AND NEW.state <> 'UNKNOWN' AND NOT EXISTS ("
            f"SELECT 1 FROM {ATTEMPTS} a WHERE a.intent_id = NEW.intent_id"
            f" AND a.attempt_no = {latest} AND a.resolved_outcome = NEW.remote_outcome"
            " AND a.resolution_evidence_kind IS NOT NULL)",
        ),
    )


def _install_attempt_rules() -> None:
    identity = (
        "attempt_id",
        "intent_id",
        "attempt_no",
        "request_payload_hash",
        "sanitizer_profile_version",
        "started_at",
    )
    finish = (
        "finished_at",
        "response_status",
        "response_digest",
        "error_class",
        "error_code",
        "remote_outcome",
    )
    _trigger(
        f"trg_{ATTEMPTS}_append",
        "INSERT",
        ATTEMPTS,
        _raise(
            f"{ATTEMPTS}: attempts are appended in order",
            f"NEW.attempt_no <> (SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM {ATTEMPTS}"
            " WHERE intent_id = NEW.intent_id)",
        )
        + _raise(
            f"{ATTEMPTS}: an attempt starts open",
            "NEW.finished_at IS NOT NULL OR NEW.resolved_outcome IS NOT NULL",
        )
        + _raise(
            f"{ATTEMPTS}: only a PREPARED or proven-not-applied intent is sent",
            f"(SELECT state FROM {INTENTS} WHERE intent_id = NEW.intent_id)"
            " IS NOT 'PREPARED' AND (SELECT state FROM"
            f" {INTENTS} WHERE intent_id = NEW.intent_id) IS NOT 'FAILED'",
        )
        + _raise(
            f"{ATTEMPTS}: never resend after an outcome that is not proven not applied",
            f"EXISTS (SELECT 1 FROM {ATTEMPTS} a WHERE a.intent_id = NEW.intent_id"
            " AND a.attempt_no = NEW.attempt_no - 1"
            " AND COALESCE(a.resolved_outcome, a.remote_outcome) IS NOT 'NOT_APPLIED_PROVEN')",
        ),
    )
    finished = (
        "OLD.finished_at IS NULL AND NEW.finished_at IS NOT NULL AND NEW.resolved_outcome IS NULL"
        f" AND {_unchanged(identity)}"
    )
    resolved = (
        "OLD.finished_at IS NOT NULL AND OLD.remote_outcome = 'UNKNOWN'"
        " AND OLD.resolved_outcome IS NULL AND NEW.resolved_outcome IS NOT NULL"
        f" AND {_unchanged(identity)} AND {_unchanged(finish)}"
    )
    _trigger(
        f"trg_{ATTEMPTS}_finish_or_resolve",
        "UPDATE",
        ATTEMPTS,
        _raise(
            f"{ATTEMPTS}: an attempt is finished once and resolved at most once",
            f"NOT (({finished}) OR ({resolved}))",
        ),
    )


def _install_registration_rules() -> None:
    _trigger(
        f"trg_{REGISTRATIONS}_verified",
        "INSERT",
        REGISTRATIONS,
        _raise(
            f"{REGISTRATIONS}: a registration follows its CONFIRMED intent",
            f"NOT EXISTS (SELECT 1 FROM {INTENTS} i WHERE i.intent_id = NEW.intent_id"
            " AND i.state = 'CONFIRMED'"
            " AND i.registration_snapshot_id = NEW.registration_snapshot_id"
            " AND i.marketplace_key = NEW.marketplace_key"
            " AND i.marketplace_account_id = NEW.marketplace_account_id"
            " AND i.marketplace_product_id = NEW.marketplace_product_id"
            " AND i.comparison_contract_version = NEW.comparison_contract_version"
            " AND i.normalizer_version = NEW.normalizer_version"
            " AND i.verification_evidence_digest = NEW.readback_evidence_digest)",
        )
        + _raise(
            f"{REGISTRATIONS}: the seller code is the listing identity sent",
            f"NEW.seller_product_code IS NOT (SELECT listing_identity FROM {SNAPSHOTS}"
            " WHERE registration_snapshot_id = NEW.registration_snapshot_id)",
        )
        + _raise(
            f"{REGISTRATIONS}: a registration starts ACTIVE", "NEW.lifecycle_state <> 'ACTIVE'"
        ),
    )
    core = (
        "registration_id",
        "intent_id",
        "registration_snapshot_id",
        "marketplace_key",
        "marketplace_account_id",
        "marketplace_product_id",
        "seller_product_code",
        "published_state",
        "comparison_contract_version",
        "normalizer_version",
        "readback_evidence_digest",
        "verified_at",
        "created_by",
        "correlation_id",
        "created_at",
    )
    _trigger(
        f"trg_{REGISTRATIONS}_lifecycle",
        "UPDATE",
        REGISTRATIONS,
        _raise(
            f"{REGISTRATIONS}: only a newer read-back or a proven external absence is recorded",
            f"NOT ({_unchanged(core)} AND OLD.lifecycle_state = 'ACTIVE'"
            " AND ((NEW.lifecycle_state = 'EXTERNALLY_REMOVED'"
            " AND NEW.last_readback_at IS OLD.last_readback_at)"
            " OR (NEW.lifecycle_state = 'ACTIVE' AND NEW.last_readback_at >= OLD.last_readback_at)))",
        ),
    )
    _trigger(
        f"trg_{REGISTRATION_ITEMS}_sent",
        "INSERT",
        REGISTRATION_ITEMS,
        _raise(
            f"{REGISTRATION_ITEMS}: the item is one the registration snapshot sent",
            f"NOT EXISTS (SELECT 1 FROM {ITEM_SNAPSHOTS} s JOIN {REGISTRATIONS} r"
            " ON r.registration_snapshot_id = s.registration_snapshot_id"
            " WHERE r.registration_id = NEW.registration_id"
            " AND s.item_snapshot_id = NEW.item_snapshot_id"
            " AND s.registration_item_key = NEW.registration_item_key"
            " AND s.listing_composition_id = NEW.listing_composition_id"
            " AND s.group_id_at_registration = NEW.current_group_id)",
        ),
    )
    _trigger(
        f"trg_{REGISTRATION_ITEMS}_pointers",
        "UPDATE",
        REGISTRATION_ITEMS,
        _raise(
            f"{REGISTRATION_ITEMS}: only the current group and binding pointers move",
            "NOT ("
            + _unchanged(
                (
                    "registration_item_id",
                    "registration_id",
                    "item_snapshot_id",
                    "registration_item_key",
                    "listing_composition_id",
                    "marketplace_option_id",
                    "created_at",
                )
            )
            + ")",
        ),
    )


def _install_override_rules() -> None:
    _trigger(
        f"trg_{OVERRIDES}_one_active",
        "INSERT",
        OVERRIDES,
        _raise(f"{OVERRIDES}: an override starts active", "NEW.revoked_at IS NOT NULL")
        + _raise(
            f"{OVERRIDES}: one active override per scope",
            f"EXISTS (SELECT 1 FROM {OVERRIDES} o WHERE o.revoked_at IS NULL"
            " AND o.marketplace_key = NEW.marketplace_key"
            " AND o.marketplace_account_id = NEW.marketplace_account_id"
            " AND o.product_group_id = NEW.product_group_id"
            " AND o.listing_composition_id IS NEW.listing_composition_id)",
        ),
    )
    _trigger(
        f"trg_{OVERRIDES}_revoke_only",
        "UPDATE",
        OVERRIDES,
        _raise(
            f"{OVERRIDES}: an override is only ever revoked",
            "NOT (OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL AND "
            + _unchanged(
                (
                    "override_id",
                    "marketplace_key",
                    "marketplace_account_id",
                    "product_group_id",
                    "listing_composition_id",
                    "reason",
                    "approved_by",
                    "correlation_id",
                    "created_at",
                )
            )
            + ")",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        held = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if held:
            raise RuntimeError(
                f"cannot drop {table}: {held} row(s) are held; registration and account state"
                " is never silently destroyed"
            )
    for table in reversed(TABLES):
        op.drop_table(table)
