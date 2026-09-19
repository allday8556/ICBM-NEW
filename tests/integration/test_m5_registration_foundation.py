"""M5 PR-B: the registration foundation (Issue #89, ADR-0014), on a migrated database.

Every rule is proven twice where it can be: through the registration store, and by the raw write
that would break it, which the database refuses on its own (migration 0016). No supplier,
marketplace or AI provider is contacted; every marketplace, account, identity and value is
invented, and no provider response exists anywhere in this module.
"""

import contextlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import CheckConstraint

from app.collect.facts import FieldFact, QuantityTier, QuantityTiersValue
from app.config import AppConfig
from app.connect.marketplace.capability import RemoteOutcome
from app.container import Container
from app.core.errors import ErrorClass, InputValidationError, NotFoundError
from app.db.database import Database, create_sqlite_engine
from app.db.metadata import metadata
from app.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.register.model import (
    AbsenceEvidence,
    BatchSummary,
    IntentState,
    ListingShape,
    RegistrationConflictError,
    RegistrationLifecycle,
    ResolutionEvidence,
    ResolvedBy,
    VerificationState,
    idempotency_key,
    registration_item_key,
    sanitized_digest,
)
from app.register.service import RegisterService
from app.register.store import (
    ItemSnapshotSpec,
    RegistrationStore,
    RegistrationUnit,
    SnapshotRecord,
    SnapshotSpec,
)
from tests.collect_support import confirmed
from tests.product_support import Collections, context, count, product, raw
from tests.support import FakeClock

pytestmark = pytest.mark.integration

MARKET = "market_a"
ACCOUNT = "account-1"
AT = "2026-09-19 00:00:00"
CID = "cid-register"
OPERATOR = "operator-1"
REGISTRATION_TABLES = (
    "registration_drafts",
    "registration_draft_items",
    "registration_snapshots",
    "registration_item_snapshots",
    "registration_batches",
    "registration_intents",
    "registration_attempts",
    "marketplace_registrations",
    "marketplace_registration_items",
    "duplicate_overrides",
)


@dataclass(frozen=True)
class Priced:
    """One M4 Item priced for ``MARKET``, with the exact truth a Snapshot must name."""

    item_id: str
    group: str
    membership_revision_id: str
    facts_revision_id: str
    pricing_snapshot_id: str


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def store(container: Container) -> RegistrationStore:
    return container.registrations


def _truth(config: AppConfig, item_id: str, snapshot_id: str, group: str) -> Priced:
    with contextlib.closing(raw(config)) as connection:
        membership, facts = connection.execute(
            "SELECT membership_revision_id, source_product_facts_revision_id"
            " FROM pricing_snapshots WHERE pricing_snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    return Priced(item_id, group, membership, facts, snapshot_id)


def _priced(
    container: Container,
    config: AppConfig,
    sources: Collections,
    source_product_id: str = "1234",
) -> Priced:
    run_id, _revision = sources.collect(product(), source_product_id=source_product_id)
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None and result.product_group_id is not None, result
    snapshot = container.pricing.price(result.item_id, context()).snapshot
    assert snapshot is not None
    return _truth(config, result.item_id, snapshot.pricing_snapshot_id, result.product_group_id)


def _tier_fact(*pairs: tuple[int, int]) -> FieldFact:
    return confirmed(
        QuantityTiersValue(
            tiers=tuple(
                QuantityTier(quantity=q, total_price_krw=t, label=f"tier-{q}") for q, t in pairs
            )
        ),
        ".tiers",
    )


def _tiered(
    container: Container, config: AppConfig, sources: Collections, source_product_id: str
) -> dict[int, Priced]:
    fields = product(quantity_tiers=_tier_fact((1, 19900), (2, 37900), (3, 53900)))
    run_id, _revision = sources.collect(fields, source_product_id=source_product_id)
    result = container.materializer.materialize_run(run_id)
    group = str(result.product_group_id)
    priced = {}
    for item in container.products.product(group).items:
        snapshot = container.pricing.price(item.item_id, context()).snapshot
        assert snapshot is not None
        priced[item.composition.quantity] = _truth(
            config, item.item_id, snapshot.pricing_snapshot_id, group
        )
    return priced


def _draft(
    store: RegistrationStore,
    items: list[Priced],
    shape: ListingShape = ListingShape.SINGLE_LISTING_WITH_OPTIONS,
    account: str = ACCOUNT,
) -> str:
    with store.transaction() as unit:
        draft = unit.create_draft(MARKET, account, shape, created_by=OPERATOR, correlation_id=CID)
        for item in items:
            unit.add_draft_item(draft.draft_id, item.item_id, added_by=OPERATOR, correlation_id=CID)
        return draft.draft_id


def _spec(
    store: RegistrationStore,
    draft_id: str,
    items: list[Priced],
    listing_identity: str | None = None,
) -> SnapshotSpec:
    draft = store.draft(draft_id)
    assert draft is not None
    return SnapshotSpec(
        draft_id=draft_id,
        draft_revision=draft.draft_revision,
        listing_identity=listing_identity or f"icbm-{uuid.uuid4().hex}",
        preflight_rule_version="preflight-test-1",
        preflight_fingerprint="a" * 64,
        category_mapping_revision="category-test-1",
        taxonomy_revision="taxonomy-test-1",
        policy_revisions={"shipping_template": "shipping-test-1"},
        detail_composition_revision="detail-test-1",
        sanitizer_profile_version="sanitizer-test-1",
        payload={"name": "invented listing name", "items": len(items)},
        items=[
            ItemSnapshotSpec(
                item_id=item.item_id,
                group_membership_revision_id=item.membership_revision_id,
                source_product_facts_revision_id=item.facts_revision_id,
                pricing_snapshot_id=item.pricing_snapshot_id,
                source_snapshot={"binding": "copied at registration"},
                publication_assets=[{"artifact_sha256": "b" * 64, "provider_asset": "asset-1"}],
                outbound_values={"option_value": f"invented option {n}"},
            )
            for n, item in enumerate(items)
        ],
    )


def _freeze(
    store: RegistrationStore,
    items: list[Priced],
    shape: ListingShape = ListingShape.SINGLE_LISTING_WITH_OPTIONS,
    listing_identity: str | None = None,
) -> SnapshotRecord:
    draft_id = _draft(store, items, shape)
    with store.transaction() as unit:
        return unit.freeze_snapshot(
            _spec(store, draft_id, items, listing_identity), created_by=OPERATOR, correlation_id=CID
        )


def _intent(store: RegistrationStore, snapshot_id: str) -> str:
    with store.transaction() as unit:
        batch = unit.create_batch(MARKET, ACCOUNT, created_by=OPERATOR, correlation_id=CID)
        return unit.create_intent(
            batch, snapshot_id, created_by=OPERATOR, correlation_id=CID
        ).intent_id


def _send(unit: RegistrationUnit, intent_id: str) -> str:
    return unit.start_attempt(
        intent_id,
        sanitized_request={"request": "sanitized"},
        sanitizer_profile_version="sanitizer-test-1",
        correlation_id=CID,
    ).attempt_id


def _finish(
    store: RegistrationStore,
    intent_id: str,
    outcome: RemoteOutcome,
    product_id: str | None = None,
) -> None:
    with store.transaction() as unit:
        attempt = _send(unit, intent_id)
        unit.finish_attempt(
            attempt,
            remote_outcome=outcome,
            marketplace_product_id=product_id,
            correlation_id=CID,
            error_class=None if outcome is RemoteOutcome.APPLIED_PROVEN else ErrorClass.TRANSIENT,
        )


def _confirm(store: RegistrationStore, intent_id: str) -> str:
    intent = store.intent(intent_id)
    assert intent is not None
    snapshot = store.snapshot(intent.registration_snapshot_id)
    assert snapshot is not None
    with store.transaction() as unit:
        return unit.confirm_registration(
            intent_id,
            comparison_contract_version="comparison-test-1",
            normalizer_version="normalizer-test-1",
            sanitized_readback={"read_back": "sanitized"},
            published_state="SALE",
            option_ids={i.registration_item_key: f"opt-{i.ordinal}" for i in snapshot.items},
            created_by=OPERATOR,
            correlation_id=CID,
        ).registration_id


def _refused(config: AppConfig, sql: str, *args: object, match: str) -> None:
    with (
        contextlib.closing(raw(config)) as connection,
        pytest.raises(sqlite3.IntegrityError, match=match),
    ):
        connection.execute(sql, args)
        connection.commit()


def _one(config: AppConfig, sql: str, *args: object) -> tuple[object, ...]:
    with contextlib.closing(raw(config)) as connection:
        return tuple(connection.execute(sql, args).fetchone())


# ---------------------------------------------------------------- schema


def test_the_m5_checks_match_the_orm(config: AppConfig) -> None:
    # The CHECK expressions of 0016 are frozen literals; they must equal the models' expressions.
    with contextlib.closing(raw(config)) as connection:
        for table in REGISTRATION_TABLES:
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()[0]
            checks = [
                c for c in metadata.tables[table].constraints if isinstance(c, CheckConstraint)
            ]
            assert checks, table
            for check in checks:
                assert f"CHECK ({check.sqltext})" in ddl, f"{table}: {check.name}"


def test_every_registration_table_rejects_delete(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    snapshot = _freeze(store, [item])
    _intent(store, snapshot.registration_snapshot_id)
    for table in REGISTRATION_TABLES:
        with contextlib.closing(raw(config)) as connection:
            held = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if held:
            _refused(config, f"DELETE FROM {table}", match="never deleted")


# ---------------------------------------------------------------- drafts (§2)


def test_a_draft_holds_existing_items_once_by_item_key(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    draft_id = _draft(store, [item])
    with store.transaction() as unit, pytest.raises(InputValidationError):
        unit.add_draft_item(draft_id, item.item_id, added_by=OPERATOR, correlation_id=CID)
    with store.transaction() as unit, pytest.raises(NotFoundError):
        unit.add_draft_item(draft_id, str(uuid.uuid4()), added_by=OPERATOR, correlation_id=CID)
    signature = _one(
        config, "SELECT composition_signature FROM product_items WHERE item_id = ?", item.item_id
    )[0]
    insert = (
        "INSERT INTO registration_draft_items (draft_item_id, draft_id, item_id, product_group_id,"
        " composition_signature, ordinal, added_by, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )
    # The same group + composition signature twice in one Draft (§2).
    _refused(
        config,
        insert,
        str(uuid.uuid4()), draft_id, item.item_id, item.group, signature, 7, OPERATOR, AT,
        match="UNIQUE",
    )  # fmt: skip
    # An identity that is not the Item's own.
    other = _priced(container, config, sources, "5678")
    _refused(
        config,
        insert,
        str(uuid.uuid4()), draft_id, item.item_id, other.group, signature, 8, OPERATOR, AT,
        match="the group and signature are the Item identity",
    )  # fmt: skip


def test_the_draft_scope_is_fixed_and_every_change_advances_its_revision(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    draft_id = _draft(store, [item])
    assert store.draft(draft_id).draft_revision == 2  # type: ignore[union-attr]
    with store.transaction() as unit:
        unit.remove_draft_item(draft_id, item.item_id, removed_by=OPERATOR, correlation_id=CID)
        draft = unit.change_listing_shape(
            draft_id, ListingShape.SEPARATE_LISTINGS, changed_by=OPERATOR, correlation_id=CID
        )
    assert (draft.draft_revision, draft.items, draft.listing_shape) == (
        4,
        (),
        ListingShape.SEPARATE_LISTINGS,
    )
    # The removed item stays as history; it can be added again as a new open item.
    assert count(config, "registration_draft_items", "removed_at IS NOT NULL") == 1
    _refused(
        config,
        "UPDATE registration_drafts SET account_id = 'other', draft_revision = draft_revision + 1"
        " WHERE draft_id = ?",
        draft_id,
        match="the draft identity and scope are immutable",
    )
    _refused(
        config,
        "UPDATE registration_drafts SET draft_revision = draft_revision + 2 WHERE draft_id = ?",
        draft_id,
        match="every change advances the revision by one",
    )
    _refused(
        config,
        "UPDATE registration_draft_items SET ordinal = 9 WHERE draft_id = ?",
        draft_id,
        match="only ever removed",
    )


# ---------------------------------------------------------------- snapshots (§6, §7)


def test_a_snapshot_freezes_exact_m4_truth_with_deterministic_item_keys(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    identity = "icbm-listing-0001"
    snapshot = _freeze(store, [item], listing_identity=identity)
    (frozen,) = snapshot.items
    signature = _one(
        config, "SELECT composition_signature FROM product_items WHERE item_id = ?", item.item_id
    )[0]
    assert frozen.registration_item_key == registration_item_key(identity, item.group, signature)
    assert frozen.registration_item_key.startswith("rik1-")
    assert (frozen.group_id_at_registration, frozen.pricing_snapshot_id) == (
        item.group,
        item.pricing_snapshot_id,
    )
    # §15 (B4): the durable payload digest is of the sanitized canonical representation.
    payload = {"name": "invented listing name", "items": 1}
    assert snapshot.payload_hash == sanitized_digest(payload)
    _refused(
        config,
        "UPDATE registration_snapshots SET payload_hash = ? WHERE registration_snapshot_id = ?",
        "c" * 64,
        snapshot.registration_snapshot_id,
        match="is immutable",
    )
    _refused(
        config,
        "UPDATE registration_item_snapshots SET outbound_values_json = '{}'",
        match="is immutable",
    )
    # A later price does not follow into the historical Snapshot (§6).
    container.pricing.price(item.item_id, context(fee_table_version="fee-test-2"))
    assert store.snapshot(snapshot.registration_snapshot_id) == snapshot


def test_a_snapshot_freezes_only_the_current_draft_revision(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    draft_id = _draft(store, [item])
    stale = _spec(store, draft_id, [item])
    with store.transaction() as unit:
        unit.change_listing_shape(
            draft_id,
            ListingShape.SELECTED_OFFERS,
            changed_by=OPERATOR,
            correlation_id=CID,
        )
    with store.transaction() as unit, pytest.raises(RegistrationConflictError):
        unit.freeze_snapshot(stale, created_by=OPERATOR, correlation_id=CID)
    _refused(
        config,
        "INSERT INTO registration_snapshots (registration_snapshot_id, draft_id, draft_revision,"
        " marketplace_key, account_id, listing_shape, listing_identity, preflight_rule_version,"
        " preflight_fingerprint, category_mapping_revision, taxonomy_revision,"
        " policy_revisions_json, detail_composition_revision, sanitizer_profile_version,"
        " payload_hash, payload_json, created_by, correlation_id, created_at)"
        " VALUES (?, ?, 1, ?, ?, 'SELECTED_OFFERS', 'icbm-listing-0002', 'p', ?, 'c', 't', '{}',"
        " 'd', 's', ?, '{}', 'o', 'c', ?)",
        str(uuid.uuid4()), draft_id, MARKET, ACCOUNT, "a" * 64, "a" * 64, AT,
        match="freezes the current draft revision",
    )  # fmt: skip


def test_an_item_snapshot_names_the_exact_price_of_its_item_and_target(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    other = _priced(container, config, sources, "5678")
    elsewhere = container.pricing.price(item.item_id, context(marketplace_key="market_b")).snapshot
    assert elsewhere is not None
    draft_id = _draft(store, [item])
    wrong = {
        "another Item's price": _with_price(item, other.pricing_snapshot_id),
        "a price for another marketplace": _with_price(item, elsewhere.pricing_snapshot_id),
    }
    for why, bad in wrong.items():
        with store.transaction() as unit, pytest.raises(Exception, match="exact M4 snapshot"):
            unit.freeze_snapshot(
                _spec(store, draft_id, [bad]), created_by=OPERATOR, correlation_id=CID
            )
        assert count(config, "registration_snapshots") == 0, why
    membership = _with_membership(item, other.membership_revision_id)
    with store.transaction() as unit, pytest.raises(Exception, match="belongs to the group"):
        unit.freeze_snapshot(
            _spec(store, draft_id, [membership]), created_by=OPERATOR, correlation_id=CID
        )


def _with_price(item: Priced, snapshot_id: str) -> Priced:
    return Priced(
        item.item_id, item.group, item.membership_revision_id, item.facts_revision_id, snapshot_id
    )


def _with_membership(item: Priced, membership_id: str) -> Priced:
    return Priced(
        item.item_id, item.group, membership_id, item.facts_revision_id, item.pricing_snapshot_id
    )


def test_a_separate_listing_is_one_item_per_snapshot(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    items = _tiered(container, config, sources, "tiered-1")
    both = [items[1], items[2]]
    draft_id = _draft(store, both, ListingShape.SEPARATE_LISTINGS)
    with store.transaction() as unit, pytest.raises(InputValidationError):
        unit.freeze_snapshot(_spec(store, draft_id, both), created_by=OPERATOR, correlation_id=CID)
    with store.transaction() as unit:
        first = unit.freeze_snapshot(
            _spec(store, draft_id, [items[1]]), created_by=OPERATOR, correlation_id=CID
        )
    _refused(
        config,
        "INSERT INTO registration_item_snapshots SELECT ?, registration_snapshot_id,"
        " 'rik1-' || substr(registration_item_key, 6, 31) || 'f', 1, item_id,"
        " group_id_at_registration, group_membership_revision_id, listing_composition_id,"
        " composition_signature, source_product_facts_revision_id_at_registration,"
        " pricing_snapshot_id_at_registration, source_snapshot_json, publication_assets_json,"
        " outbound_values_json FROM registration_item_snapshots WHERE registration_snapshot_id = ?",
        str(uuid.uuid4()), first.registration_snapshot_id,
        match="UNIQUE|exactly one Item",
    )  # fmt: skip


def test_a_snapshot_named_by_an_intent_is_frozen(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    items = _tiered(container, config, sources, "tiered-2")
    snapshot = _freeze(store, [items[1]])
    _intent(store, snapshot.registration_snapshot_id)
    _refused(
        config,
        "INSERT INTO registration_item_snapshots SELECT ?, registration_snapshot_id, ?, 1, item_id,"
        " group_id_at_registration, group_membership_revision_id, listing_composition_id,"
        " composition_signature, source_product_facts_revision_id_at_registration,"
        " pricing_snapshot_id_at_registration, source_snapshot_json, publication_assets_json,"
        " outbound_values_json FROM registration_item_snapshots WHERE registration_snapshot_id = ?",
        str(uuid.uuid4()), "rik1-" + "e" * 32, snapshot.registration_snapshot_id,
        match="named by an intent is frozen",
    )  # fmt: skip


# ---------------------------------------------------------------- intents (§8)


def test_one_intent_per_exact_snapshot_across_restarts(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    clock: FakeClock,
) -> None:
    item = _priced(container, config, sources)
    snapshot = _freeze(store, [item])
    intent_id = _intent(store, snapshot.registration_snapshot_id)
    assert _intent(store, snapshot.registration_snapshot_id) == intent_id
    restarted_db = Database(config.database_url)
    try:
        restarted = RegistrationStore(restarted_db, clock, container.audit)
        assert _intent(restarted, snapshot.registration_snapshot_id) == intent_id
    finally:
        restarted_db.dispose()
    intent = store.intent(intent_id)
    assert intent is not None
    assert intent.idempotency_key == idempotency_key(
        MARKET, ACCOUNT, intent.operation, snapshot.registration_snapshot_id
    )
    assert count(config, "registration_intents") == 1
    _refused(
        config,
        "INSERT INTO registration_intents SELECT ?, registration_batch_id,"
        " registration_snapshot_id, marketplace_key, account_id, operation, ?, 'PREPARED', NULL,"
        " NULL, 'NOT_VERIFIED', NULL, NULL, NULL, NULL, created_by, correlation_id, created_at,"
        " updated_at FROM registration_intents",
        str(uuid.uuid4()), "f" * 64,
        match="UNIQUE",
    )  # fmt: skip


def test_concurrent_sends_cannot_compete(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    intent_id = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    with store.transaction() as unit:
        _send(unit, intent_id)
    with store.transaction() as unit, pytest.raises(RegistrationConflictError):
        _send(unit, intent_id)
    _refused(
        config,
        "INSERT INTO registration_attempts (attempt_id, intent_id, attempt_no,"
        " request_payload_hash, sanitizer_profile_version, started_at) VALUES (?, ?, 2, ?, 's', ?)",
        str(uuid.uuid4()), intent_id, "d" * 64, AT,
        match="proven-not-applied intent is sent|UNIQUE",
    )  # fmt: skip


def test_an_intent_opens_prepared_in_its_own_scope(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    snapshot = _freeze(store, [item])
    with store.transaction() as unit:
        other = unit.create_batch(MARKET, "account-2", created_by=OPERATOR, correlation_id=CID)
    with store.transaction() as unit, pytest.raises(InputValidationError):
        unit.create_intent(
            other, snapshot.registration_snapshot_id, created_by=OPERATOR, correlation_id=CID
        )
    insert = (
        "INSERT INTO registration_intents (intent_id, registration_batch_id,"
        " registration_snapshot_id, marketplace_key, account_id, operation, idempotency_key,"
        " state, remote_outcome, marketplace_product_id, verification_state, created_by,"
        " correlation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'CREATE', ?, ?, ?, ?,"
        " 'NOT_VERIFIED', 'o', 'c', ?, ?)"
    )
    _refused(
        config,
        insert,
        str(uuid.uuid4()), other, snapshot.registration_snapshot_id, MARKET, "account-2",
        "1" * 64, "PREPARED", None, None, AT, AT,
        match="the intent scope is its snapshot scope",
    )  # fmt: skip
    with store.transaction() as unit:
        batch = unit.create_batch(MARKET, ACCOUNT, created_by=OPERATOR, correlation_id=CID)
    _refused(
        config,
        insert,
        str(uuid.uuid4()), batch, snapshot.registration_snapshot_id, MARKET, ACCOUNT,
        "2" * 64, "SENT", None, None, AT, AT,
        match="an intent opens PREPARED",
    )  # fmt: skip


# ---------------------------------------------------------------- outcomes (§9, §10)


def test_an_unknown_is_reconciled_never_resent(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    intent_id = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, intent_id, RemoteOutcome.UNKNOWN)
    intent = store.intent(intent_id)
    assert intent is not None and intent.state is IntentState.UNKNOWN
    (attempt,) = store.attempts(intent_id)
    assert attempt.ambiguous_result
    with store.transaction() as unit, pytest.raises(RegistrationConflictError):
        _send(unit, intent_id)
    _refused(
        config,
        "INSERT INTO registration_attempts (attempt_id, intent_id, attempt_no,"
        " request_payload_hash, sanitizer_profile_version, started_at) VALUES (?, ?, 2, ?, 's', ?)",
        str(uuid.uuid4()), intent_id, "d" * 64, AT,
        match="only a PREPARED or proven-not-applied intent is sent",
    )  # fmt: skip
    # Nothing silently turns an UNKNOWN into FAILED.
    _refused(
        config,
        "UPDATE registration_intents SET state = 'FAILED', remote_outcome = 'NOT_APPLIED_PROVEN'"
        " WHERE intent_id = ?",
        intent_id,
        match="backed by the latest attempt|backed by evidence",
    )


def test_a_retry_after_proven_not_applied_is_a_new_attempt_of_the_same_intent(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    intent_id = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, intent_id, RemoteOutcome.NOT_APPLIED_PROVEN)
    assert store.intent(intent_id).state is IntentState.FAILED  # type: ignore[union-attr]
    _finish(store, intent_id, RemoteOutcome.APPLIED_PROVEN, "mp-1")
    attempts = store.attempts(intent_id)
    assert [a.attempt_no for a in attempts] == [1, 2]
    # §15 (B4): each durable request digest is of the sanitized canonical request representation.
    assert {a.request_payload_hash for a in attempts} == {sanitized_digest({"request": "sanitized"})}
    assert (count(config, "registration_snapshots"), count(config, "registration_intents")) == (
        1,
        1,
    )
    intent = store.intent(intent_id)
    assert intent is not None
    # §11: an applied CREATE is not a registration until it is verified.
    assert (intent.state, intent.verification_state) == (
        IntentState.SENT,
        VerificationState.NOT_VERIFIED,
    )
    assert count(config, "marketplace_registrations") == 0


def test_an_outcome_is_backed_by_the_latest_attempt(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    intent_id = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    with store.transaction() as unit:
        _send(unit, intent_id)
    _refused(
        config,
        "UPDATE registration_intents SET state = 'FAILED', remote_outcome = 'NOT_APPLIED_PROVEN'"
        " WHERE intent_id = ?",
        intent_id,
        match="backed by the latest attempt",
    )
    _finish_open(store, intent_id, RemoteOutcome.APPLIED_PROVEN, "mp-2")
    _refused(
        config,
        "UPDATE registration_attempts SET error_code = 'late' WHERE intent_id = ?",
        intent_id,
        match="finished once and resolved at most once",
    )
    _refused(
        config,
        "UPDATE registration_intents SET state = 'CONFIRMED' WHERE intent_id = ?",
        intent_id,
        match="CHECK",
    )


def _finish_open(
    store: RegistrationStore, intent_id: str, outcome: RemoteOutcome, product_id: str | None
) -> None:
    open_attempt = next(a for a in store.attempts(intent_id) if not a.finished)
    with store.transaction() as unit:
        unit.finish_attempt(
            open_attempt.attempt_id,
            remote_outcome=outcome,
            marketplace_product_id=product_id,
            correlation_id=CID,
        )


def test_only_machine_or_provider_evidence_resolves_an_unknown(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    # ADR-0014 §10, B3: USER records or accepts evidence; it is never the evidence.
    assert {e.value for e in ResolutionEvidence} == {
        "PROVIDER_READ_BACK",
        "PROVIDER_LOOKUP",
        "TRANSMISSION_PRECLUDED",
        "REVIEWED_MACHINE_PROOF",
    }
    item = _priced(container, config, sources)
    intent_id = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, intent_id, RemoteOutcome.UNKNOWN)
    resolve = "UPDATE registration_attempts SET resolved_outcome = 'NOT_APPLIED_PROVEN',"
    at = f" resolved_at = '{AT}' WHERE intent_id = ?"
    digest = "'" + "9" * 64 + "'"
    for kind, by, why in (
        ("'OPERATOR_ASSERTION'", "'USER'", "CHECK"),  # the operator's word is no evidence kind
        ("NULL", "'USER'", "CHECK"),  # a resolution always names its evidence
        ("'PROVIDER_LOOKUP'", "'READ_BACK'", "CHECK"),  # the resolver matches the evidence
    ):
        _refused(
            config,
            f"{resolve} resolved_by = {by}, resolution_evidence_kind = {kind},"
            f" resolution_evidence_digest = {digest},{at}",
            intent_id,
            match=why,
        )
    _refused(
        config,
        "UPDATE registration_attempts SET resolved_outcome = 'APPLIED_PROVEN',"
        " resolved_by = 'USER', resolution_evidence_kind = 'TRANSMISSION_PRECLUDED',"
        f" resolution_evidence_digest = {digest},{at}",
        intent_id,
        match="CHECK",
    )  # transmission-precluded evidence proves absence only
    with store.transaction() as unit:
        resolved = unit.resolve_unknown(
            intent_id,
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            resolved_by=ResolvedBy.USER,
            evidence_kind=ResolutionEvidence.PROVIDER_LOOKUP,
            sanitized_evidence={"lookup": "no listing under the listing identity"},
            correlation_id=CID,
            actor=OPERATOR,
        )
    assert resolved.state is IntentState.FAILED
    (attempt,) = store.attempts(intent_id)
    assert (attempt.resolved_by, attempt.resolution_evidence_kind, attempt.ambiguous_result) == (
        ResolvedBy.USER,
        ResolutionEvidence.PROVIDER_LOOKUP,
        False,
    )
    _refused(
        config,
        "UPDATE registration_attempts SET resolved_outcome = 'APPLIED_PROVEN' WHERE intent_id = ?",
        intent_id,
        match="resolved at most once",
    )


# ---------------------------------------------------------------- the conflict scope (§10, R2)


def test_an_unresolved_unknown_blocks_a_new_snapshot_in_its_group(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    first = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, first, RemoteOutcome.UNKNOWN)
    again = _freeze(store, [item])  # a new Draft, a new Snapshot, a new listing identity
    assert store.conflicting_intents(again.registration_snapshot_id) == (first,)
    with pytest.raises(RegistrationConflictError, match="unresolved CREATE"):
        _intent(store, again.registration_snapshot_id)
    with store.transaction() as unit:
        batch = unit.create_batch(MARKET, ACCOUNT, created_by=OPERATOR, correlation_id=CID)
    _refused(
        config,
        "INSERT INTO registration_intents (intent_id, registration_batch_id,"
        " registration_snapshot_id, marketplace_key, account_id, operation, idempotency_key,"
        " state, verification_state, created_by, correlation_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'CREATE', ?, 'PREPARED', 'NOT_VERIFIED', 'o', 'c', ?, ?)",
        str(uuid.uuid4()), batch, again.registration_snapshot_id, MARKET, ACCOUNT, "3" * 64,
        AT, AT,
        match="an unresolved CREATE blocks its conflict scope",
    )  # fmt: skip


def test_an_in_flight_create_blocks_its_scope_and_a_non_overlapping_group_is_free(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    other = _priced(container, config, sources, "5678")
    first = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    with store.transaction() as unit:
        _send(unit, first)  # SENT, outcome not yet known
    with pytest.raises(RegistrationConflictError):
        _intent(store, _freeze(store, [item]).registration_snapshot_id)
    # Another group in the same marketplace account is not blocked (R2).
    assert _intent(store, _freeze(store, [other]).registration_snapshot_id)


def test_the_same_listing_identity_is_one_conflict_scope(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    other = _priced(container, config, sources, "5678")
    identity = "icbm-listing-0003"
    first = _intent(
        store, _freeze(store, [item], listing_identity=identity).registration_snapshot_id
    )
    _finish(store, first, RemoteOutcome.UNKNOWN)
    with pytest.raises(RegistrationConflictError):
        _intent(store, _freeze(store, [other], listing_identity=identity).registration_snapshot_id)


def test_merge_or_split_lineage_widens_the_scope_fail_closed(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    other = _priced(container, config, sources, "5678")
    first = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, first, RemoteOutcome.UNKNOWN)
    # A recorded SPLIT connects the two groups: which successor holds the listing is unclear.
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            "INSERT INTO group_change_events (event_id, event_type, predecessor_group_ids,"
            " successor_group_ids, decided_by, correlation_id, created_at)"
            " VALUES (?, 'SPLIT', ?, ?, 'o', 'c', ?)",
            (
                str(uuid.uuid4()),
                json.dumps([item.group]),
                json.dumps([item.group, other.group]),
                AT,
            ),
        )
        connection.commit()
    blocked = _freeze(store, [other])
    assert store.conflicting_intents(blocked.registration_snapshot_id) == (first,)
    with pytest.raises(RegistrationConflictError):
        _intent(store, blocked.registration_snapshot_id)


def test_a_duplicate_override_never_releases_an_unknown(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    first = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, first, RemoteOutcome.UNKNOWN)
    with store.transaction() as unit:
        unit.record_duplicate_override(
            MARKET,
            ACCOUNT,
            item.group,
            reason="an intentional second listing",
            approved_by=OPERATOR,
            correlation_id=CID,
        )
    with pytest.raises(RegistrationConflictError):
        _intent(store, _freeze(store, [item]).registration_snapshot_id)


def test_only_a_proven_not_applied_resolution_frees_the_scope(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    first = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, first, RemoteOutcome.UNKNOWN)
    with store.transaction() as unit:
        unit.resolve_unknown(
            first,
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            resolved_by=ResolvedBy.LOOKUP,
            evidence_kind=ResolutionEvidence.PROVIDER_LOOKUP,
            sanitized_evidence={"lookup": "absent"},
            correlation_id=CID,
            actor=OPERATOR,
        )
    assert _intent(store, _freeze(store, [item]).registration_snapshot_id)


# ---------------------------------------------------------------- verification (§11, R3)


def test_a_subset_read_back_is_a_mismatch_of_the_whole_intent(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    items = _tiered(container, config, sources, "tiered-3")
    snapshot = _freeze(store, [items[1], items[2], items[3]])
    intent_id = _intent(store, snapshot.registration_snapshot_id)
    _finish(store, intent_id, RemoteOutcome.APPLIED_PROVEN, "mp-3")
    with store.transaction() as unit:
        mismatched = unit.record_mismatch(
            intent_id,
            comparison_contract_version="comparison-test-1",
            normalizer_version="normalizer-test-1",
            sanitized_comparison={"missing": 1},
            actor=OPERATOR,
            correlation_id=CID,
        )
    assert (mismatched.state, mismatched.remote_outcome, mismatched.verification_state) == (
        IntentState.SENT,
        RemoteOutcome.APPLIED_PROVEN,
        VerificationState.MISMATCH,
    )
    assert mismatched.marketplace_product_id == "mp-3"
    # No CREATE resend for the missing Items, and no per-Item registration.
    with store.transaction() as unit, pytest.raises(RegistrationConflictError):
        _send(unit, intent_id)
    subset = {i.registration_item_key: None for i in snapshot.items[:2]}
    with store.transaction() as unit, pytest.raises(InputValidationError):
        unit.confirm_registration(
            intent_id,
            comparison_contract_version="comparison-test-1",
            normalizer_version="normalizer-test-1",
            sanitized_readback={"read_back": "subset"},
            published_state="SALE",
            option_ids=subset,
            created_by=OPERATOR,
            correlation_id=CID,
        )
    assert count(config, "marketplace_registrations") == 0
    assert count(config, "registration_intents") == 1
    # The live, unverified listing still blocks a new CREATE in its scope.
    with pytest.raises(RegistrationConflictError):
        _intent(store, _freeze(store, [items[1]]).registration_snapshot_id)


def test_a_registration_follows_only_a_verified_intent(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    items = _tiered(container, config, sources, "tiered-4")
    snapshot = _freeze(store, [items[1], items[2]])
    intent_id = _intent(store, snapshot.registration_snapshot_id)
    _finish(store, intent_id, RemoteOutcome.APPLIED_PROVEN, "mp-4")
    _refused(
        config,
        "INSERT INTO marketplace_registrations (registration_id, intent_id,"
        " registration_snapshot_id, marketplace_key, account_id, marketplace_product_id,"
        " seller_product_code, published_state, lifecycle_state, comparison_contract_version,"
        " normalizer_version, readback_evidence_digest, verified_at, last_readback_at,"
        " created_by, correlation_id, created_at) VALUES (?, ?, ?, ?, ?, 'mp-4', ?, 'SALE',"
        " 'ACTIVE', 'c', 'n', ?, ?, ?, 'o', 'c', ?)",
        str(uuid.uuid4()), intent_id, snapshot.registration_snapshot_id, MARKET, ACCOUNT,
        snapshot.listing_identity, "8" * 64, AT, AT, AT,
        match="follows its CONFIRMED intent",
    )  # fmt: skip
    registration_id = _confirm(store, intent_id)
    registration = store.registration(registration_id)
    assert registration is not None
    assert registration.seller_product_code == snapshot.listing_identity
    assert registration.lifecycle_state is RegistrationLifecycle.ACTIVE
    assert {i.registration_item_key for i in registration.items} == {
        i.registration_item_key for i in snapshot.items
    }
    assert store.intent(intent_id).state is IntentState.CONFIRMED  # type: ignore[union-attr]
    _refused(
        config,
        "UPDATE registration_intents SET state = 'SENT' WHERE intent_id = ?",
        intent_id,
        match="state transition not allowed",
    )
    _refused(
        config,
        "UPDATE marketplace_registration_items SET registration_item_key = ?",
        "rik1-" + "0" * 32,
        match="only the current group and binding pointers move",
    )


# ---------------------------------------------------------------- partial success (§12, R3)


def test_separate_listings_succeed_or_fail_per_listing_with_a_derived_summary(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    items = _tiered(container, config, sources, "tiered-5")
    draft_id = _draft(store, [items[1], items[2]], ListingShape.SEPARATE_LISTINGS)
    with store.transaction() as unit:
        batch = unit.create_batch(MARKET, ACCOUNT, created_by=OPERATOR, correlation_id=CID)
        units = [
            unit.freeze_snapshot(
                _spec_in(unit, draft_id, items[q]),
                created_by=OPERATOR,
                correlation_id=CID,
            )
            for q in (1, 2)
        ]
        intents = [
            unit.create_intent(
                batch, s.registration_snapshot_id, created_by=OPERATOR, correlation_id=CID
            ).intent_id
            for s in units
        ]
    assert store.batch_summary(batch) is BatchSummary.IN_PROGRESS
    _finish(store, intents[0], RemoteOutcome.APPLIED_PROVEN, "mp-5")
    _confirm(store, intents[0])
    _finish(store, intents[1], RemoteOutcome.NOT_APPLIED_PROVEN)
    assert store.batch_summary(batch) is BatchSummary.PARTIAL
    # The CONFIRMED sibling is never resent; the failed one retries as its own listing.
    with store.transaction() as unit, pytest.raises(RegistrationConflictError):
        _send(unit, intents[0])
    _finish(store, intents[1], RemoteOutcome.APPLIED_PROVEN, "mp-6")
    assert store.intent(intents[0]).state is IntentState.CONFIRMED  # type: ignore[union-attr]
    columns = {c.name for c in metadata.tables["registration_batches"].columns}
    assert not {"state", "status", "summary"} & columns


def _spec_in(unit: RegistrationUnit, draft_id: str, item: Priced) -> SnapshotSpec:
    draft = unit.draft(draft_id)
    assert draft is not None
    return SnapshotSpec(
        draft_id=draft_id,
        draft_revision=draft.draft_revision,
        listing_identity=f"icbm-{uuid.uuid4().hex}",
        preflight_rule_version="preflight-test-1",
        preflight_fingerprint="a" * 64,
        category_mapping_revision="category-test-1",
        taxonomy_revision="taxonomy-test-1",
        policy_revisions={},
        detail_composition_revision="detail-test-1",
        sanitizer_profile_version="sanitizer-test-1",
        payload={"name": "invented"},
        items=[
            ItemSnapshotSpec(
                item_id=item.item_id,
                group_membership_revision_id=item.membership_revision_id,
                source_product_facts_revision_id=item.facts_revision_id,
                pricing_snapshot_id=item.pricing_snapshot_id,
                source_snapshot={"binding": "copied"},
                publication_assets=[{"artifact_sha256": "b" * 64}],
                outbound_values={},
            )
        ],
    )


# ---------------------------------------------------------------- external removal (§14, R4)


def test_a_proven_external_absence_keeps_history_and_frees_a_fresh_registration(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    snapshot = _freeze(store, [item])
    intent_id = _intent(store, snapshot.registration_snapshot_id)
    _finish(store, intent_id, RemoteOutcome.APPLIED_PROVEN, "mp-7")
    registration_id = _confirm(store, intent_id)
    for kind in ("'OPERATOR_ASSERTION'", "NULL"):
        _refused(
            config,
            "UPDATE marketplace_registrations SET lifecycle_state = 'EXTERNALLY_REMOVED',"
            f" absence_observed_at = '{AT}', absence_evidence_kind = {kind},"
            f" absence_evidence_digest = '{'7' * 64}', absence_recorded_by = 'o'",
            match="CHECK",
        )
    with store.transaction() as unit:
        removed = unit.record_external_absence(
            registration_id,
            evidence_kind=AbsenceEvidence.PROVIDER_LOOKUP,
            sanitized_evidence={"lookup": "absent"},
            recorded_by=OPERATOR,
            correlation_id=CID,
        )
    assert removed.lifecycle_state is RegistrationLifecycle.EXTERNALLY_REMOVED
    # History stays: the registration, its Snapshot, its Intent and its attempts.
    assert (
        count(config, "marketplace_registrations"),
        count(config, "registration_snapshots"),
        count(config, "registration_attempts"),
    ) == (1, 1, 1)
    _refused(
        config,
        "UPDATE marketplace_registrations SET lifecycle_state = 'ACTIVE'",
        match="only a newer read-back or a proven external absence",
    )
    # A fresh Snapshot with a new listing identity and a new Intent may register the group again.
    again = _freeze(store, [item])
    assert again.listing_identity != snapshot.listing_identity
    assert _intent(store, again.registration_snapshot_id) != intent_id
    assert store.registration(registration_id) is not None


# ---------------------------------------------------------------- overrides (§13)


def test_one_active_override_per_scope_revoked_at_most_once(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    with store.transaction() as unit:
        override = unit.record_duplicate_override(
            MARKET, ACCOUNT, item.group, reason="intentional", approved_by=OPERATOR,
            correlation_id=CID,
        )  # fmt: skip
    with store.transaction() as unit, pytest.raises(Exception, match="one active override"):
        unit.record_duplicate_override(
            MARKET, ACCOUNT, item.group, reason="again", approved_by=OPERATOR, correlation_id=CID
        )
    with store.transaction() as unit:
        unit.revoke_duplicate_override(
            override.override_id, revoked_by=OPERATOR, reason="done", correlation_id=CID
        )
    with store.transaction() as unit, pytest.raises(RegistrationConflictError):
        unit.revoke_duplicate_override(
            override.override_id, revoked_by=OPERATOR, reason="twice", correlation_id=CID
        )
    _refused(
        config,
        "UPDATE duplicate_overrides SET reason = 'rewritten'",
        match="only ever revoked",
    )
    with store.reading() as unit:
        assert unit.active_overrides(MARKET, ACCOUNT, item.group) == ()


# ---------------------------------------------------------------- boundary and audit


def test_register_stays_zero_and_the_audit_holds_identifiers_only(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    intent_id = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, intent_id, RemoteOutcome.APPLIED_PROVEN, "mp-8")
    _confirm(store, intent_id)
    service = RegisterService()
    assert (service.registration_candidate_count(), service.registration_count()) == (0, 0)
    with contextlib.closing(raw(config)) as connection:
        rows = connection.execute(
            "SELECT event_type, details_json FROM audit_events"
            " WHERE event_type LIKE 'REGISTRATION_%'"
        ).fetchall()
    kinds = {kind for kind, _details in rows}
    assert {
        "REGISTRATION_DRAFT_RECORDED",
        "REGISTRATION_SNAPSHOT_FROZEN",
        "REGISTRATION_INTENT_RECORDED",
        "REGISTRATION_ATTEMPT_RECORDED",
        "REGISTRATION_VERIFICATION_RECORDED",
    } <= kinds
    text = " ".join(details for _kind, details in rows)
    assert "invented" not in text and "sanitized" not in text


# ---------------------------------------------------------------- migration


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_0016_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    url = _url(tmp_path / "icbm.db")
    upgrade_to_head(url)
    command.downgrade(alembic_config(url), "0015_m4_quantity_offers")
    command.upgrade(alembic_config(url), "head")
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        connection.execute(
            "INSERT INTO registration_drafts (draft_id, marketplace_key, account_id,"
            " listing_shape, draft_revision, created_by, created_at, updated_at)"
            " VALUES ('d', ?, ?, 'SEPARATE_LISTINGS', 1, 'o', ?, ?)",
            (MARKET, ACCOUNT, AT, AT),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0015_m4_quantity_offers")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0016_m5_registration_foundation"
    finally:
        engine.dispose()
