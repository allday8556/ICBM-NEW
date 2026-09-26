"""M5 PR-B: the registration foundation (Issue #89, ADR-0014), on a migrated database.

Every rule is proven twice where it can be: through the registration store, and by the raw write
that would break it, which the database refuses on its own (migration 0016). No supplier,
marketplace or AI provider is contacted; every marketplace, account, identity and value is
invented, and no provider response exists anywhere in this module. A committed M2 binding unit
is written directly as the invented state an explicit M2 binding would have left.
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
from app.connect.accounts import AccountBinding
from app.connect.marketplace.capability import RemoteOutcome
from app.container import Container
from app.core.errors import ErrorClass, InputValidationError, NotFoundError, PolicyBlockedError
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
    PreparationInputs,
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
OTHER_MARKET = "market_b"
AT = "2026-09-19 00:00:00"
CID = "cid-register"
OPERATOR = "operator-1"
# The canonical id of MARKET's bound account, minted per test by the autouse ``account`` fixture.
ACCOUNT = ""
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
    # M5 PR-E (migration 0017, ADR-0014 §26): the REGISTER execution-scope send brake.
    "registration_execution_scopes",
    # M5 PR-F (migration 0018, ADR-0014 §27): the operator-authored preparation and the
    # provenance of the Snapshot it froze.
    "registration_preparations",
    "registration_preparation_revisions",
    "registration_preparation_items",
    "registration_snapshot_preparations",
)
ACCOUNT_TABLES = ("seller_entities", "marketplace_accounts")


@dataclass(frozen=True)
class Priced:
    """One M4 Item priced for ``MARKET``, with the exact truth its pinned price names."""

    item_id: str
    group: str
    membership_revision_id: str
    facts_revision_id: str
    pricing_snapshot_id: str


# ---------------------------------------------------------------- canonical accounts


def _bind(config: AppConfig, marketplace_key: str, uid: str | None) -> None:
    """The committed M2 binding unit of ``marketplace_key`` (ACCOUNT_IDENTITY §5), or a later
    explicit rebinding to another invented provider identity. ``None`` leaves a connection that
    was never bound."""
    bound = (1, 1, AT, OPERATOR) if uid is not None else (None, None, None, None)
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            "INSERT INTO marketplace_connections (marketplace_key, credential_generation_hwm,"
            " session_generation_hwm, provider_account_uid, provider_account_id,"
            " bound_credential_generation, bound_session_generation, bound_at, bound_by,"
            " created_at, updated_at) VALUES (?, 1, 1, ?, NULL, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (marketplace_key) DO UPDATE SET"
            " provider_account_uid = excluded.provider_account_uid",
            (marketplace_key, uid, *bound, AT, AT),
        )
        connection.commit()


def _establish(container: Container, config: AppConfig, marketplace_key: str, uid: str) -> str:
    _bind(config, marketplace_key, uid)
    seller = container.accounts.create_seller_entity(created_by=OPERATOR, correlation_id=CID)
    return container.accounts.establish(
        marketplace_key, seller, established_by=OPERATOR, correlation_id=CID
    ).marketplace_account_id


@pytest.fixture(autouse=True)
def account(container: Container, config: AppConfig) -> str:
    global ACCOUNT
    ACCOUNT = _establish(container, config, MARKET, "uid-market-a-1")
    return ACCOUNT


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def store(container: Container) -> RegistrationStore:
    return container.registrations


# ---------------------------------------------------------------- M4 truth


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


def _repriced(container: Container, config: AppConfig, item: Priced, **overrides: object) -> str:
    snapshot = container.pricing.price(item.item_id, context(**overrides)).snapshot
    assert snapshot is not None
    return snapshot.pricing_snapshot_id


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


# ---------------------------------------------------------------- registration steps


def _draft(
    store: RegistrationStore,
    items: list[Priced],
    shape: ListingShape = ListingShape.SINGLE_LISTING_WITH_OPTIONS,
    account: str | None = None,
) -> str:
    with store.transaction() as unit:
        draft = unit.create_draft(
            MARKET, account or ACCOUNT, shape, created_by=OPERATOR, correlation_id=CID
        )
        for item in items:
            unit.add_draft_item(
                draft.draft_id,
                item.item_id,
                item.pricing_snapshot_id,
                added_by=OPERATOR,
                correlation_id=CID,
            )
        return draft.draft_id


def _item_spec(item: Priced, n: int = 0) -> ItemSnapshotSpec:
    return ItemSnapshotSpec(
        item_id=item.item_id,
        source_snapshot={"binding": "copied at registration"},
        publication_assets=[{"artifact_sha256": "b" * 64, "provider_asset": "asset-1"}],
        outbound_values={"option_value": f"invented option {n}"},
    )


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
        items=[_item_spec(item, n) for n, item in enumerate(items)],
    )


def _freeze_in(
    store: RegistrationStore,
    draft_id: str,
    items: list[Priced],
    listing_identity: str | None = None,
) -> SnapshotRecord:
    with store.transaction() as unit:
        return unit.freeze_snapshot(
            _spec(store, draft_id, items, listing_identity), created_by=OPERATOR, correlation_id=CID
        )


def _freeze(
    store: RegistrationStore,
    items: list[Priced],
    shape: ListingShape = ListingShape.SINGLE_LISTING_WITH_OPTIONS,
    listing_identity: str | None = None,
    account: str | None = None,
) -> SnapshotRecord:
    return _freeze_in(store, _draft(store, items, shape, account), items, listing_identity)


def _intent(store: RegistrationStore, snapshot_id: str) -> str:
    snapshot = store.snapshot(snapshot_id)
    assert snapshot is not None
    with store.transaction() as unit:
        batch = unit.create_batch(
            snapshot.marketplace_key,
            snapshot.marketplace_account_id,
            created_by=OPERATOR,
            correlation_id=CID,
        )
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


def _copy_unit(config: AppConfig, snapshot_id: str) -> str:
    """A raw copy of a Snapshot row under a new id and listing identity, without its Items."""
    copy = str(uuid.uuid4())
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            "INSERT INTO registration_snapshots SELECT ?, draft_id, draft_revision,"
            " marketplace_key, marketplace_account_id, listing_shape, ?, preflight_rule_version,"
            " preflight_fingerprint, category_mapping_revision, taxonomy_revision,"
            " policy_revisions_json, detail_composition_revision, sanitizer_profile_version,"
            " payload_hash, payload_json, created_by, correlation_id, created_at"
            " FROM registration_snapshots WHERE registration_snapshot_id = ?",
            (copy, f"icbm-{uuid.uuid4().hex}", snapshot_id),
        )
        connection.commit()
    return copy


_COPY_ITEM = (
    "INSERT INTO registration_item_snapshots SELECT ?, ?, ?, ordinal, item_id,"
    " group_id_at_registration, COALESCE(?, group_membership_revision_id),"
    " listing_composition_id, composition_signature,"
    " source_product_facts_revision_id_at_registration, pricing_snapshot_id_at_registration,"
    " source_snapshot_json, publication_assets_json, outbound_values_json"
    " FROM registration_item_snapshots WHERE registration_snapshot_id = ? AND item_id = ?"
)


def _copy_item_args(
    source: str, item_id: str, target: str, membership: str | None = None
) -> tuple[object, ...]:
    return (str(uuid.uuid4()), target, f"rik1-{uuid.uuid4().hex}", membership, source, item_id)


_RAW_INTENT = (
    "INSERT INTO registration_intents (intent_id, registration_batch_id,"
    " registration_snapshot_id, marketplace_key, marketplace_account_id, operation,"
    " idempotency_key, state, verification_state, created_by, correlation_id, created_at,"
    " updated_at) VALUES (?, ?, ?, ?, ?, 'CREATE', ?, 'PREPARED', 'NOT_VERIFIED', 'o', 'c', ?, ?)"
)


def _raw_intent_args(batch: str, snapshot_id: str, account: str) -> tuple[object, ...]:
    return (str(uuid.uuid4()), batch, snapshot_id, MARKET, account, uuid.uuid4().hex * 2, AT, AT)


def _batch(store: RegistrationStore, account: str | None = None) -> str:
    with store.transaction() as unit:
        return unit.create_batch(
            MARKET, account or ACCOUNT, created_by=OPERATOR, correlation_id=CID
        )


# ---------------------------------------------------------------- schema


def test_the_m5_checks_match_the_orm(config: AppConfig) -> None:
    # The CHECK expressions of 0016 are frozen literals; they must equal the models' expressions.
    with contextlib.closing(raw(config)) as connection:
        for table in REGISTRATION_TABLES + ACCOUNT_TABLES:
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
    for table in REGISTRATION_TABLES + ACCOUNT_TABLES:
        with contextlib.closing(raw(config)) as connection:
            held = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if held:
            _refused(config, f"DELETE FROM {table}", match="never deleted")


# ------------------------------------------------ the execution-scope brake (§26, migration 0017)

SCOPES = "registration_execution_scopes"
# M5 PR-F (migration 0018, §27): the preparation owner and the Snapshot provenance it records.
PREPARATION_TABLES = (
    "registration_preparations",
    "registration_preparation_revisions",
    "registration_preparation_items",
    "registration_snapshot_preparations",
)
# Gate 1 G1-A (migration 0019, ADR-0015 §2): later tables that step down with an earlier owner.
TARGET_POLICY_TABLES = (
    "registration_target_policies",
    "registration_target_policy_revisions",
    "registration_target_policy_current",
    # Gate 1 G1-B (migration 0020, ADR-0015 §3).
    "registration_category_metadata",
    "registration_category_metadata_revisions",
    "registration_category_metadata_current",
)
# Gate 2 G2-A (migration 0021, ADR-0016): the ReviewItem owner, after the registration tables.
REVIEW_TABLES = ("review_items", "review_item_events", "review_coverage")
# Adaptive Collector P2 (migration 0024, ADR-0017 §3, §7): after the review tables.
ADAPTIVE_TABLES = (
    "adaptive_profile_revisions",
    "adaptive_profile_pins",
    "adaptive_profile_lint",
    "adaptive_profile_transitions",
    "adaptive_validation_samples",
    "adaptive_validation_runs",
    "adaptive_validation_run_samples",
    "adaptive_shadow_switch_entries",
    "adaptive_shadow_records",
    "adaptive_evidence_windows",
    "adaptive_evidence_window_events",
    "adaptive_shadow_ledger_events",
    # Gate 3 area 1 (ADR-0018 §12).
    "live_grants",
    "protected_write_brakes",
    "asset_upload_attempts",
    "restore_drills",
    "retention_proofs",
)
GROUP = "product_registration"
_SCOPE_COLUMNS = (
    "marketplace_key, marketplace_account_id, endpoint_group, state, pause_reason,"
    " pause_error_class, paused_at, pause_policy_version, resume_generation, resumed_at,"
    " resumed_by, resume_reason, created_at, updated_at"
)


def _paused_row(account: str, *, at: str = AT) -> tuple[object, ...]:
    return (MARKET, account, GROUP, "PAUSED", "POLICY", "POLICY_BLOCKED", at, "policy/v1", 0,
            None, None, None, AT, AT)  # fmt: skip


def _insert_scope(config: AppConfig, row: tuple[object, ...]) -> None:
    with contextlib.closing(raw(config)) as connection:
        connection.execute(
            f"INSERT INTO {SCOPES} ({_SCOPE_COLUMNS}) VALUES ({', '.join('?' for _ in row)})",
            row,
        )
        connection.commit()


def _scope_refused(config: AppConfig, row: tuple[object, ...], *, match: str) -> None:
    _refused(
        config,
        f"INSERT INTO {SCOPES} ({_SCOPE_COLUMNS}) VALUES ({', '.join('?' for _ in row)})",
        *row,
        match=match,
    )


def test_an_execution_scope_opens_only_for_a_bound_canonical_account(
    container: Container, config: AppConfig, account: str
) -> None:
    # §26 keeps PR-B's account rule: the brake is scoped by a canonical account, never a free
    # string, and the scope key is exactly marketplace x account x endpoint group.
    _scope_refused(config, _paused_row("account-1"), match="not bound to its identity")
    _insert_scope(config, _paused_row(account))
    assert _one(config, f"SELECT COUNT(*) FROM {SCOPES}") == (1,)
    _refused(
        config,
        f"INSERT INTO {SCOPES} ({_SCOPE_COLUMNS})"
        f" VALUES ({', '.join('?' for _ in _paused_row(account))})",
        *_paused_row(account),
        match="UNIQUE",
    )
    # Another endpoint group of the same account is another row, not the same brake.
    other = (*_paused_row(account)[:2], "product_inquiry", *_paused_row(account)[3:])
    _insert_scope(config, other)
    assert _one(config, f"SELECT COUNT(*) FROM {SCOPES}") == (2,)


def test_an_execution_scope_row_is_never_half_recorded(config: AppConfig, account: str) -> None:
    paused = list(_paused_row(account))
    # ACTIVE holds no open pause.
    active_with_pause = (*paused[:3], "ACTIVE", *paused[4:])
    _scope_refused(config, active_with_pause, match="active_holds_no_pause")
    # PAUSED names its cause, its time and the policy that judged it.
    for index in (4, 6, 7):  # pause_reason, paused_at, pause_policy_version
        missing = list(paused)
        missing[index] = None
        _scope_refused(config, tuple(missing), match="paused_states_its_cause")
    # A resume boundary is complete, and exists exactly when the generation has moved.
    ungenerated = list(paused)
    ungenerated[9], ungenerated[10], ungenerated[11] = AT, "operator-1", "REVIEWED"
    _scope_refused(config, tuple(ungenerated), match="resume_boundary_complete")
    counted = list(paused)
    counted[8] = 1
    _scope_refused(config, tuple(counted), match="no resume boundary")
    # A negative generation never reaches its CHECK: on INSERT the scope claims a release it
    # never had, and on UPDATE the generation would fall. Both triggers refuse it first, and the
    # CHECK stands behind them (``test_the_m5_checks_match_the_orm`` proves it is installed).
    negative = list(paused)
    negative[8] = -1
    _scope_refused(config, tuple(negative), match="no resume boundary")
    _insert_scope(config, tuple(paused))
    _refused(
        config,
        f"UPDATE {SCOPES} SET resume_generation = -1 WHERE endpoint_group = '{GROUP}'",
        match="only move forward",
    )
    # A scope opens with no resume behind it.
    resumed_at_open = list(paused)
    resumed_at_open[8], resumed_at_open[9] = 1, "2026-09-18 00:00:00"
    resumed_at_open[10], resumed_at_open[11] = "operator-1", "REVIEWED"
    _scope_refused(config, tuple(resumed_at_open), match="no resume boundary")


def test_an_execution_scope_history_only_moves_forward(config: AppConfig, account: str) -> None:
    _insert_scope(config, _paused_row(account))
    where = f"WHERE marketplace_key = '{MARKET}' AND endpoint_group = '{GROUP}'"
    # The scope key and the creation time never change.
    _refused(
        config,
        f"UPDATE {SCOPES} SET endpoint_group = 'other' {where}",
        match="only move forward",
    )
    # Leaving PAUSED is a resume, never a quiet clearing of the brake.
    _refused(
        config,
        f"UPDATE {SCOPES} SET state = 'ACTIVE', pause_reason = NULL, pause_error_class = NULL,"
        f" paused_at = NULL, pause_policy_version = NULL {where}",
        match="only move forward",
    )
    # A generation never falls, never skips, and a move is an accepted release with its own time.
    _refused(
        config,
        f"UPDATE {SCOPES} SET state = 'ACTIVE', pause_reason = NULL, pause_error_class = NULL,"
        f" paused_at = NULL, pause_policy_version = NULL, resume_generation = 2 {where}",
        match="only move forward",
    )
    resume = (
        f"UPDATE {SCOPES} SET state = 'ACTIVE', pause_reason = NULL, pause_error_class = NULL,"
        " paused_at = NULL, pause_policy_version = NULL, resume_generation = 1,"
        " resumed_at = ?, resumed_by = 'operator-1', resume_reason = 'REVIEWED',"
        f" updated_at = ? {where}"
    )
    with contextlib.closing(raw(config)) as connection:
        connection.execute(resume, ("2026-09-20 00:00:00", "2026-09-20 00:00:00"))
        connection.commit()
    assert _one(config, f"SELECT state, resume_generation FROM {SCOPES} {where}") == ("ACTIVE", 1)
    # A later boundary never moves backwards, and the generation never falls back.
    _refused(
        config,
        f"UPDATE {SCOPES} SET resume_generation = 2, resumed_at = '2026-09-19 00:00:00' {where}",
        match="only move forward",
    )
    _refused(
        config, f"UPDATE {SCOPES} SET resume_generation = 0 {where}", match="only move forward"
    )
    # And the recorded release is never quietly rewritten without a new generation.
    _refused(
        config,
        f"UPDATE {SCOPES} SET resumed_by = 'someone-else' {where}",
        match="only move forward",
    )


def test_an_execution_scope_is_never_deleted(config: AppConfig, account: str) -> None:
    _insert_scope(config, _paused_row(account))
    _refused(config, f"DELETE FROM {SCOPES}", match="never deleted")


# ---------------------------------------------------------------- canonical account (blocker 2)


def test_registration_state_opens_only_for_a_bound_canonical_account(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    free = "account-1"  # a free caller string is no canonical account
    assert container.accounts.binding(MARKET, free) is AccountBinding.UNKNOWN_ACCOUNT
    with store.transaction() as unit, pytest.raises(PolicyBlockedError):
        unit.create_draft(MARKET, free, ListingShape.SEPARATE_LISTINGS, created_by=OPERATOR,
                          correlation_id=CID)  # fmt: skip
    for write in (
        lambda unit: unit.create_batch(MARKET, free, created_by=OPERATOR, correlation_id=CID),
        lambda unit: unit.record_duplicate_override(
            MARKET, free, item.group, reason="r", approved_by=OPERATOR, correlation_id=CID
        ),
        # The right account in the wrong marketplace is not that marketplace's account.
        lambda unit: unit.create_batch(
            OTHER_MARKET, ACCOUNT, created_by=OPERATOR, correlation_id=CID
        ),
    ):
        with store.transaction() as unit, pytest.raises(PolicyBlockedError):
            write(unit)
    _refused(
        config,
        "INSERT INTO registration_drafts (draft_id, marketplace_key, marketplace_account_id,"
        " listing_shape, draft_revision, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, 'SEPARATE_LISTINGS', 1, 'o', ?, ?)",
        str(uuid.uuid4()), MARKET, free, AT, AT,
        match="not bound to its identity",
    )  # fmt: skip
    # A connection without a committed binding has no canonical account (§5: NOT_BOUND).
    _bind(config, OTHER_MARKET, None)
    seller = container.accounts.create_seller_entity(created_by=OPERATOR, correlation_id=CID)
    with pytest.raises(PolicyBlockedError):
        container.accounts.establish(
            OTHER_MARKET, seller, established_by=OPERATOR, correlation_id=CID
        )
    _refused(
        config,
        "INSERT INTO marketplace_accounts (marketplace_account_id, seller_entity_id,"
        " marketplace_key, provider_account_uid, established_by, correlation_id, established_at)"
        " VALUES (?, ?, ?, 'uid-unbound', 'o', 'c', ?)",
        f"mpa-{uuid.uuid4().hex}", seller, OTHER_MARKET, AT,
        match="established only from its committed binding",
    )  # fmt: skip
    assert count(config, "registration_drafts") == 0
    assert count(config, "marketplace_accounts") == 1


def test_a_mismatched_account_opens_no_registration_state(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    draft_id = _draft(store, [item])
    batch = _batch(store)
    # An explicit rebinding (ACCOUNT_IDENTITY §4) now names another provider identity.
    _bind(config, MARKET, "uid-market-a-2")
    assert container.accounts.binding(MARKET, ACCOUNT) is AccountBinding.MISMATCHED
    for write in (
        lambda unit: unit.create_draft(
            MARKET, ACCOUNT, ListingShape.SEPARATE_LISTINGS, created_by=OPERATOR,
            correlation_id=CID,
        ),
        lambda unit: unit.freeze_snapshot(
            _spec(store, draft_id, [item]), created_by=OPERATOR, correlation_id=CID
        ),
        lambda unit: unit.create_batch(MARKET, ACCOUNT, created_by=OPERATOR, correlation_id=CID),
        lambda unit: unit.record_duplicate_override(
            MARKET, ACCOUNT, item.group, reason="r", approved_by=OPERATOR, correlation_id=CID
        ),
    ):  # fmt: skip
        with store.transaction() as unit, pytest.raises(PolicyBlockedError):
            write(unit)
    _refused(
        config,
        "INSERT INTO registration_batches (registration_batch_id, marketplace_key,"
        " marketplace_account_id, created_by, correlation_id, created_at)"
        " VALUES (?, ?, ?, 'o', 'c', ?)",
        str(uuid.uuid4()), MARKET, ACCOUNT, AT,
        match="not bound to its identity",
    )  # fmt: skip
    # The new identity gets its own canonical account; nothing is silently rebound onto the old.
    rebound = container.accounts.establish(
        MARKET,
        container.accounts.create_seller_entity(created_by=OPERATOR, correlation_id=CID),
        established_by=OPERATOR,
        correlation_id=CID,
    ).marketplace_account_id
    assert rebound != ACCOUNT
    assert container.accounts.binding(MARKET, rebound) is AccountBinding.BOUND
    assert count(config, "registration_snapshots") == 0
    assert _one(
        config, "SELECT COUNT(*) FROM registration_batches WHERE registration_batch_id = ?", batch
    ) == (1,)


def test_one_provider_identity_is_one_canonical_account(
    container: Container, config: AppConfig
) -> None:
    other_seller = container.accounts.create_seller_entity(created_by=OPERATOR, correlation_id=CID)
    again = container.accounts.establish(
        MARKET, other_seller, established_by=OPERATOR, correlation_id=CID
    )
    assert again.marketplace_account_id == ACCOUNT  # never re-minted: no alias
    seller = _one(
        config,
        "SELECT seller_entity_id FROM marketplace_accounts WHERE marketplace_account_id = ?",
        ACCOUNT,
    )[0]
    insert = (
        "INSERT INTO marketplace_accounts (marketplace_account_id, seller_entity_id,"
        " marketplace_key, provider_account_uid, established_by, correlation_id, established_at)"
        " VALUES (?, ?, ?, ?, 'o', 'c', ?)"
    )
    _refused(
        config, insert, f"mpa-{uuid.uuid4().hex}", seller, MARKET, "uid-market-a-1", AT,
        match="UNIQUE",
    )  # fmt: skip
    # A canonical id is minted by ICBM: a label or a provider value is not one.
    _refused(config, insert, "account-1", seller, MARKET, "uid-market-a-1", AT, match="CHECK")
    _refused(
        config,
        "UPDATE marketplace_accounts SET provider_account_uid = 'uid-other'",
        match="is immutable",
    )
    with contextlib.closing(raw(config)) as connection:
        events = [
            json.loads(details)
            for (details,) in connection.execute(
                "SELECT details_json FROM audit_events"
                " WHERE event_type = 'MARKETPLACE_ACCOUNT_ESTABLISHED'"
            ).fetchall()
        ]
    assert [e["marketplace_account_id"] for e in events] == [ACCOUNT]
    assert "uid-market-a-1" not in json.dumps(events)  # the provider identity is not audited


def test_an_account_label_cannot_escape_the_unknown_or_override_scope(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    first = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, first, RemoteOutcome.UNKNOWN)
    with store.transaction() as unit:
        unit.record_duplicate_override(
            MARKET, ACCOUNT, item.group, reason="intentional", approved_by=OPERATOR,
            correlation_id=CID,
        )  # fmt: skip
    # Asking for the account again gives the same canonical id, and its scope stays blocked.
    same = _establish(container, config, MARKET, "uid-market-a-1")
    assert same == ACCOUNT
    with pytest.raises(RegistrationConflictError):
        _intent(store, _freeze(store, [item], account=same).registration_snapshot_id)
    # Another spelling of the account is no account at all.
    for label in (ACCOUNT.upper(), f" {ACCOUNT}", "uid-market-a-1"):
        with store.transaction() as unit, pytest.raises(PolicyBlockedError):
            unit.create_draft(
                MARKET, label, ListingShape.SEPARATE_LISTINGS, created_by=OPERATOR,
                correlation_id=CID,
            )  # fmt: skip
        with store.transaction() as unit, pytest.raises(PolicyBlockedError):
            unit.record_duplicate_override(
                MARKET, label, item.group, reason="r", approved_by=OPERATOR, correlation_id=CID
            )
    with store.transaction() as unit, pytest.raises(Exception, match="one active override"):
        unit.record_duplicate_override(
            MARKET, same, item.group, reason="again", approved_by=OPERATOR, correlation_id=CID
        )


def test_the_same_provider_product_id_in_two_accounts_does_not_collide(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    first = _intent(store, _freeze(store, [item]).registration_snapshot_id)
    _finish(store, first, RemoteOutcome.APPLIED_PROVEN, "mp-shared")
    _confirm(store, first)
    # A second canonical account (a later binding) may hold a listing with the same provider id.
    second_account = _establish(container, config, MARKET, "uid-market-a-2")
    second = _intent(store, _freeze(store, [item], account=second_account).registration_snapshot_id)
    _finish(store, second, RemoteOutcome.APPLIED_PROVEN, "mp-shared")
    _confirm(store, second)
    assert count(config, "marketplace_registrations", "marketplace_product_id = 'mp-shared'") == 2
    with contextlib.closing(raw(config)) as connection:
        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'marketplace_registrations'"
        ).fetchone()[0]
    assert "UNIQUE (marketplace_key, marketplace_account_id, marketplace_product_id)" in ddl
    assert "UNIQUE (marketplace_key, marketplace_product_id)" not in ddl


# ---------------------------------------------------------------- drafts (§2, blocker 1)


def test_a_draft_holds_existing_items_once_by_item_key(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    draft_id = _draft(store, [item])
    with store.transaction() as unit, pytest.raises(InputValidationError):
        unit.add_draft_item(
            draft_id, item.item_id, item.pricing_snapshot_id, added_by=OPERATOR,
            correlation_id=CID,
        )  # fmt: skip
    with store.transaction() as unit, pytest.raises(NotFoundError):
        unit.add_draft_item(
            draft_id, str(uuid.uuid4()), item.pricing_snapshot_id, added_by=OPERATOR,
            correlation_id=CID,
        )  # fmt: skip
    signature = _one(
        config, "SELECT composition_signature FROM product_items WHERE item_id = ?", item.item_id
    )[0]
    insert = (
        "INSERT INTO registration_draft_items (draft_item_id, draft_id, item_id, product_group_id,"
        " composition_signature, pricing_snapshot_id, ordinal, added_by, added_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    # The same group + composition signature twice in one Draft (§2).
    _refused(
        config,
        insert,
        str(uuid.uuid4()), draft_id, item.item_id, item.group, signature,
        item.pricing_snapshot_id, 7, OPERATOR, AT,
        match="UNIQUE",
    )  # fmt: skip
    # An identity that is not the Item's own.
    other = _priced(container, config, sources, "5678")
    _refused(
        config,
        insert,
        str(uuid.uuid4()), draft_id, item.item_id, other.group, signature,
        item.pricing_snapshot_id, 8, OPERATOR, AT,
        match="the group and signature are the Item identity",
    )  # fmt: skip


def test_a_draft_item_is_pinned_to_an_exact_price_of_its_item_and_target(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    other = _priced(container, config, sources, "5678")
    wrong = {
        "another Item's price": other.pricing_snapshot_id,
        "a price for another marketplace": _repriced(
            container, config, item, marketplace_key=OTHER_MARKET
        ),
        "a price for another account": _repriced(
            container, config, item, account_id=f"mpa-{'0' * 32}"
        ),
    }
    with store.transaction() as unit:
        draft_id = unit.create_draft(
            MARKET, ACCOUNT, ListingShape.SELECTED_OFFERS, created_by=OPERATOR, correlation_id=CID
        ).draft_id
    signature = _one(
        config, "SELECT composition_signature FROM product_items WHERE item_id = ?", item.item_id
    )[0]
    for why, price in wrong.items():
        with store.transaction() as unit, pytest.raises(InputValidationError, match="exact M4"):
            unit.add_draft_item(
                draft_id, item.item_id, price, added_by=OPERATOR, correlation_id=CID
            )
        _refused(
            config,
            "INSERT INTO registration_draft_items (draft_item_id, draft_id, item_id,"
            " product_group_id, composition_signature, pricing_snapshot_id, ordinal, added_by,"
            " added_at) VALUES (?, ?, ?, ?, ?, ?, 0, 'o', ?)",
            str(uuid.uuid4()), draft_id, item.item_id, item.group, signature, price, AT,
            match="exact M4 snapshot of this Item and draft target",
        )  # fmt: skip
        assert count(config, "registration_draft_items") == 0, why
    # A price for this very canonical account, or one that does not vary by account, is pinned.
    own = _repriced(container, config, item, account_id=ACCOUNT)
    with store.transaction() as unit:
        draft = unit.add_draft_item(
            draft_id, item.item_id, own, added_by=OPERATOR, correlation_id=CID
        )
    assert [(i.item_id, i.pricing_snapshot_id) for i in draft.items] == [(item.item_id, own)]


def test_a_new_price_selection_is_a_new_draft_revision_that_stales_its_snapshot(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    draft_id = _draft(store, [item])
    stale = _spec(store, draft_id, [item])
    newer = _repriced(container, config, item, fee_table_version="fee-test-2")
    with store.transaction() as unit:
        draft = unit.change_draft_item_price(
            draft_id, item.item_id, newer, changed_by=OPERATOR, correlation_id=CID
        )
        again = unit.change_draft_item_price(
            draft_id, item.item_id, newer, changed_by=OPERATOR, correlation_id=CID
        )
    assert (draft.draft_revision, again.draft_revision) == (3, 3)
    assert [i.pricing_snapshot_id for i in draft.items] == [newer]
    # The Draft history keeps the price it held before.
    assert (
        count(
            config,
            "registration_draft_items",
            "removed_at IS NOT NULL AND pricing_snapshot_id = ?",
            item.pricing_snapshot_id,
        )
        == 1
    )
    with store.transaction() as unit, pytest.raises(RegistrationConflictError):
        unit.freeze_snapshot(stale, created_by=OPERATOR, correlation_id=CID)
    _refused(
        config,
        "UPDATE registration_draft_items SET pricing_snapshot_id = ? WHERE draft_id = ?",
        item.pricing_snapshot_id,
        draft_id,
        match="only ever removed",
    )
    # A Snapshot freezes the pinned price and the revisions that price was computed from.
    snapshot = _freeze_in(store, draft_id, [item])
    (frozen,) = snapshot.items
    newer_truth = _truth(config, item.item_id, newer, item.group)
    assert (
        frozen.pricing_snapshot_id,
        frozen.group_membership_revision_id,
        frozen.source_product_facts_revision_id,
    ) == (newer, newer_truth.membership_revision_id, newer_truth.facts_revision_id)
    # Selecting another price after the freeze makes that Snapshot stale for an Intent.
    with store.transaction() as unit:
        unit.change_draft_item_price(
            draft_id, item.item_id, item.pricing_snapshot_id, changed_by=OPERATOR,
            correlation_id=CID,
        )  # fmt: skip
    with pytest.raises(RegistrationConflictError, match="current revision"):
        _intent(store, snapshot.registration_snapshot_id)
    _refused(
        config,
        _RAW_INTENT,
        *_raw_intent_args(_batch(store), snapshot.registration_snapshot_id, ACCOUNT),
        match="opens from a snapshot of the current draft revision",
    )


def test_an_item_snapshot_is_the_exact_m4_truth_of_its_pinned_price(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    item = _priced(container, config, sources)
    other = _priced(container, config, sources, "5678")
    draft_id = _draft(store, [item])
    pinned = _freeze_in(store, draft_id, [item])
    newer = _repriced(container, config, item, fee_table_version="fee-test-2")
    with store.transaction() as unit:
        unit.change_draft_item_price(
            draft_id, item.item_id, newer, changed_by=OPERATOR, correlation_id=CID
        )
    # Copying the old unit into the new revision sends a price that is no longer the pin.
    current = _freeze_in(store, draft_id, [item])
    copy = _copy_unit(config, current.registration_snapshot_id)
    source = pinned.registration_snapshot_id
    _refused(
        config,
        _COPY_ITEM,
        *_copy_item_args(source, item.item_id, copy),
        match="the price is the pinned exact M4 snapshot",
    )
    # A membership revision of another group is not this Item's.
    _refused(
        config,
        _COPY_ITEM,
        *_copy_item_args(
            current.registration_snapshot_id, item.item_id, copy, other.membership_revision_id
        ),
        match="belongs to the group",
    )


# ---------------------------------------------------------------- snapshots (§6, §7)


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
        "UPDATE registration_drafts SET marketplace_account_id = ?,"
        " draft_revision = draft_revision + 1 WHERE draft_id = ?",
        f"mpa-{'f' * 32}",
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
    assert (
        frozen.group_id_at_registration,
        frozen.pricing_snapshot_id,
        frozen.group_membership_revision_id,
        frozen.source_product_facts_revision_id,
    ) == (item.group, item.pricing_snapshot_id, item.membership_revision_id, item.facts_revision_id)
    assert snapshot.marketplace_account_id == ACCOUNT
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
        " marketplace_key, marketplace_account_id, listing_shape, listing_identity,"
        " preflight_rule_version, preflight_fingerprint, category_mapping_revision,"
        " taxonomy_revision, policy_revisions_json, detail_composition_revision,"
        " sanitizer_profile_version, payload_hash, payload_json, created_by, correlation_id,"
        " created_at) VALUES (?, ?, 1, ?, ?, 'SELECTED_OFFERS', 'icbm-listing-0002', 'p', ?, 'c',"
        " 't', '{}', 'd', 's', ?, '{}', 'o', 'c', ?)",
        str(uuid.uuid4()), draft_id, MARKET, ACCOUNT, "a" * 64, "a" * 64, AT,
        match="freezes the current draft revision",
    )  # fmt: skip


# ---------------------------------------------------------------- provider-listing units (R3)


def test_a_single_listing_sends_every_open_item_of_its_draft(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    items = _tiered(container, config, sources, "tiered-single")
    every = [items[1], items[2], items[3]]
    draft_id = _draft(store, every)
    # Blocker 3: a SINGLE Draft of A/B/C never freezes A/B.
    with store.transaction() as unit, pytest.raises(InputValidationError, match="every open Item"):
        unit.freeze_snapshot(
            _spec(store, draft_id, every[:2]), created_by=OPERATOR, correlation_id=CID
        )
    assert count(config, "registration_snapshots") == 0
    whole = _freeze_in(store, draft_id, every)
    assert {i.item_id for i in whole.items} == {i.item_id for i in every}
    # The durable barrier: an A/B unit written around the store never opens an Intent.
    partial = _copy_unit(config, whole.registration_snapshot_id)
    with contextlib.closing(raw(config)) as connection:
        for item in every[:2]:
            connection.execute(
                _COPY_ITEM, _copy_item_args(whole.registration_snapshot_id, item.item_id, partial)
            )
        connection.commit()
    batch = _batch(store)
    with (
        store.transaction() as unit,
        pytest.raises(RegistrationConflictError, match="current revision"),
    ):
        unit.create_intent(batch, partial, created_by=OPERATOR, correlation_id=CID)
    _refused(
        config,
        _RAW_INTENT,
        *_raw_intent_args(batch, partial, ACCOUNT),
        match="a single listing sends every open Item of its draft",
    )
    # A/B/C is one provider-listing unit and opens its one Intent.
    assert _intent(store, whole.registration_snapshot_id)
    assert count(config, "registration_intents") == 1


def test_selected_offers_may_send_a_chosen_subset(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    items = _tiered(container, config, sources, "tiered-selected")
    draft_id = _draft(store, [items[1], items[2], items[3]], ListingShape.SELECTED_OFFERS)
    chosen = _freeze_in(store, draft_id, [items[1], items[2]])
    assert len(chosen.items) == 2
    assert _intent(store, chosen.registration_snapshot_id)


def test_a_separate_listing_is_one_item_per_snapshot(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    items = _tiered(container, config, sources, "tiered-1")
    both = [items[1], items[2]]
    draft_id = _draft(store, both, ListingShape.SEPARATE_LISTINGS)
    with store.transaction() as unit, pytest.raises(InputValidationError):
        unit.freeze_snapshot(_spec(store, draft_id, both), created_by=OPERATOR, correlation_id=CID)
    first = _freeze_in(store, draft_id, [items[1]])
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
        " registration_snapshot_id, marketplace_key, marketplace_account_id, operation, ?,"
        " 'PREPARED', NULL, NULL, 'NOT_VERIFIED', NULL, NULL, NULL, NULL, created_by,"
        " correlation_id, created_at, updated_at FROM registration_intents",
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
    elsewhere = _establish(container, config, OTHER_MARKET, "uid-market-b-1")
    with store.transaction() as unit:
        other = unit.create_batch(OTHER_MARKET, elsewhere, created_by=OPERATOR, correlation_id=CID)
    with store.transaction() as unit, pytest.raises(InputValidationError):
        unit.create_intent(
            other, snapshot.registration_snapshot_id, created_by=OPERATOR, correlation_id=CID
        )
    insert = (
        "INSERT INTO registration_intents (intent_id, registration_batch_id,"
        " registration_snapshot_id, marketplace_key, marketplace_account_id, operation,"
        " idempotency_key, state, remote_outcome, marketplace_product_id, verification_state,"
        " created_by, correlation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'CREATE', ?,"
        " ?, ?, ?, 'NOT_VERIFIED', 'o', 'c', ?, ?)"
    )
    _refused(
        config,
        insert,
        str(uuid.uuid4()), other, snapshot.registration_snapshot_id, OTHER_MARKET, elsewhere,
        "1" * 64, "PREPARED", None, None, AT, AT,
        match="the intent scope is its snapshot scope",
    )  # fmt: skip
    batch = _batch(store)
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
    request_digest = sanitized_digest({"request": "sanitized"})
    assert {a.request_payload_hash for a in attempts} == {request_digest}
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
    _refused(
        config,
        _RAW_INTENT,
        *_raw_intent_args(_batch(store), again.registration_snapshot_id, ACCOUNT),
        match="an unresolved CREATE blocks its conflict scope",
    )


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
        " registration_snapshot_id, marketplace_key, marketplace_account_id,"
        " marketplace_product_id, seller_product_code, published_state, lifecycle_state,"
        " comparison_contract_version, normalizer_version, readback_evidence_digest, verified_at,"
        " last_readback_at, created_by, correlation_id, created_at) VALUES (?, ?, ?, ?, ?, 'mp-4',"
        " ?, 'SALE', 'ACTIVE', 'c', 'n', ?, ?, ?, 'o', 'c', ?)",
        str(uuid.uuid4()), intent_id, snapshot.registration_snapshot_id, MARKET, ACCOUNT,
        snapshot.listing_identity, "8" * 64, AT, AT, AT,
        match="follows its CONFIRMED intent",
    )  # fmt: skip
    registration_id = _confirm(store, intent_id)
    registration = store.registration(registration_id)
    assert registration is not None
    assert registration.seller_product_code == snapshot.listing_identity
    assert registration.marketplace_account_id == ACCOUNT
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
        items=[_item_spec(item)],
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
    assert "uid-market-a-1" not in text  # never the provider account identity


# ------------------------------------------- the preparation owner (§27, migration 0018)

PREPARATIONS, REVISIONS, PREP_ITEMS, LINKS = PREPARATION_TABLES


def _authored(**overrides: object) -> PreparationInputs:
    values: dict[str, object] = {
        "category": {"category_id": "cat-1", "confirmation": "OPERATOR_CONFIRMED"},
        "listing": {"name": {"value": "authored name"}, "tags": [], "attributes": {}},
        "detail": {"composition_revision": "detail-1", "body": "authored body"},
        "fingerprint": "c" * 64,
    }
    values.update(overrides)
    return PreparationInputs(**values)  # type: ignore[arg-type]


def _prepared_draft(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> tuple[str, str, str]:
    """A Draft holding one open Item, and a preparation authored for it."""
    item = _priced(container, config, sources)
    with store.transaction() as unit:
        draft_id = unit.create_draft(
            MARKET,
            ACCOUNT,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id=CID,
        ).draft_id
        unit.add_draft_item(
            draft_id,
            item.item_id,
            item.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id=CID,
        )
        record = unit.create_preparation(
            draft_id,
            item_ids=[item.item_id],
            inputs=_authored(),
            created_by=OPERATOR,
            correlation_id=CID,
        )
    return draft_id, item.item_id, record.preparation_id


def test_a_preparation_records_its_authored_inputs_and_keeps_every_revision(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    # §27: inputs only, append-only, and each revision with its own fingerprint.
    _draft_id, item_id, preparation_id = _prepared_draft(container, config, sources, store)
    with store.transaction() as unit:
        unit.revise_preparation(
            preparation_id,
            item_ids=[item_id],
            inputs=_authored(fingerprint="d" * 64),
            authored_by=OPERATOR,
            correlation_id=CID,
        )
    record = store.preparation(preparation_id)
    assert record is not None
    assert [revision.revision_no for revision in record.revisions] == [1, 2]
    assert record.current.inputs_fingerprint == "d" * 64
    assert record.revisions[0].inputs_fingerprint == "c" * 64
    assert record.current.item_ids == (item_id,)
    assert record.current.listing["name"]["value"] == "authored name"
    # Every write is audited, with identifiers and the fingerprint only.
    kinds = {kind for kind, _details in _audit_rows(config, "REGISTRATION_PREPARATION_%")}
    assert kinds == {"REGISTRATION_PREPARATION_RECORDED", "REGISTRATION_PREPARATION_REVISED"}


def test_one_preparation_owns_a_draft_unit_and_its_membership_never_moves(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    draft_id, item_id, preparation_id = _prepared_draft(container, config, sources, store)
    with (
        store.transaction() as unit,
        pytest.raises(RegistrationConflictError, match="already has a preparation"),
    ):
        unit.create_preparation(
            draft_id,
            item_ids=[item_id],
            inputs=_authored(fingerprint="d" * 64),
            created_by=OPERATOR,
            correlation_id=CID,
        )
    other = _priced(container, config, sources, "5678")
    with store.transaction() as unit:
        unit.add_draft_item(
            draft_id,
            other.item_id,
            other.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id=CID,
        )
    with (
        store.transaction() as unit,
        pytest.raises(RegistrationConflictError, match="original exact Item membership"),
    ):
        unit.revise_preparation(
            preparation_id,
            item_ids=[item_id, other.item_id],
            inputs=_authored(fingerprint="e" * 64),
            authored_by=OPERATOR,
            correlation_id=CID,
        )
    assert len(store.preparations_of_draft(draft_id)) == 1
    stored = store.preparation(preparation_id)
    assert stored is not None and stored.current.item_ids == (item_id,)


def test_an_authored_revision_is_never_edited_or_deleted(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    _draft_id, _item_id, preparation_id = _prepared_draft(container, config, sources, store)
    record = store.preparation(preparation_id)
    assert record is not None
    revision_id = record.current.preparation_revision_id
    _refused(
        config,
        f"UPDATE {REVISIONS} SET listing_json = '{{}}' WHERE preparation_revision_id = ?",
        revision_id,
        match="never updated",
    )
    _refused(
        config,
        f"DELETE FROM {REVISIONS} WHERE preparation_revision_id = ?",
        revision_id,
        match="never deleted",
    )
    _refused(
        config,
        f"DELETE FROM {PREP_ITEMS} WHERE preparation_revision_id = ?",
        revision_id,
        match="never deleted",
    )
    _refused(
        config,
        f"DELETE FROM {PREPARATIONS} WHERE preparation_id = ?",
        preparation_id,
        match="never deleted",
    )


def test_a_revision_follows_the_one_before_it_and_names_open_items(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    _draft_id, _item_id, preparation_id = _prepared_draft(container, config, sources, store)
    # A revision number that skips or repeats is refused by the database itself.
    _refused(
        config,
        f"INSERT INTO {REVISIONS} (preparation_revision_id, preparation_id, revision_no,"
        " draft_revision, category_json, listing_json, detail_json, inputs_fingerprint,"
        " authored_by, correlation_id, authored_at)"
        " VALUES (?, ?, 5, 1, NULL, '{}', NULL, ?, 'o', 'c', ?)",
        str(uuid.uuid4()),
        preparation_id,
        "e" * 64,
        AT,
        match="follows the one before it",
    )
    # An Item of another Draft is refused: a preparation prepares its own Draft's Items.
    other = _priced(container, config, sources, "5678")
    record = store.preparation(preparation_id)
    assert record is not None
    _refused(
        config,
        f"INSERT INTO {PREP_ITEMS} VALUES (?, ?, ?, 9)",
        str(uuid.uuid4()),
        record.current.preparation_revision_id,
        other.item_id,
        match="not an open Item",
    )


def test_a_snapshot_provenance_names_the_fingerprint_its_revision_holds(
    container: Container, config: AppConfig, sources: Collections, store: RegistrationStore
) -> None:
    _draft_id, _item_id, preparation_id = _prepared_draft(container, config, sources, store)
    record = store.preparation(preparation_id)
    assert record is not None
    # A provenance row whose fingerprint is not the revision's own is refused.
    _refused(
        config,
        f"INSERT INTO {LINKS} VALUES ('not-a-snapshot', ?, ?, 0, ?)",
        record.current.preparation_revision_id,
        "f" * 64,
        AT,
        match="the one the named revision holds",
    )


def _audit_rows(config: AppConfig, like: str) -> list[tuple[str, str]]:
    with contextlib.closing(raw(config)) as connection:
        return [
            (str(kind), str(details))
            for kind, details in connection.execute(
                "SELECT event_type, details_json FROM audit_events WHERE event_type LIKE ?",
                (like,),
            ).fetchall()
        ]


# ---------------------------------------------------------------- migration


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_0017_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    # §26: the execution-scope owner adds one table and touches nothing else, round-trips, and
    # never lets an engaged brake be dropped silently.
    url = _url(tmp_path / "icbm.db")
    upgrade_to_head(url)
    before = _tables(tmp_path / "icbm.db")
    command.downgrade(alembic_config(url), "0016_m5_registration_foundation")
    # 0017 owns exactly the scope table; the tables later migrations add step down with it.
    assert before - _tables(tmp_path / "icbm.db") == (
        {SCOPES}
        | set(PREPARATION_TABLES)
        | set(TARGET_POLICY_TABLES)
        | set(REVIEW_TABLES)
        | set(ADAPTIVE_TABLES)
    )
    command.upgrade(alembic_config(url), "head")
    assert _tables(tmp_path / "icbm.db") == before
    account_id = f"mpa-{'1' * 32}"
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        connection.execute(
            "INSERT INTO marketplace_connections (marketplace_key, credential_generation_hwm,"
            " session_generation_hwm, provider_account_uid, provider_account_id,"
            " bound_credential_generation, bound_session_generation, bound_at, bound_by,"
            " created_at, updated_at) VALUES (?, 1, 1, 'uid-x', NULL, 1, 1, ?, 'o', ?, ?)",
            (MARKET, AT, AT, AT),
        )
        connection.execute("INSERT INTO seller_entities VALUES ('seller-1', 'o', 'c', ?)", (AT,))
        connection.execute(
            "INSERT INTO marketplace_accounts VALUES (?, 'seller-1', ?, 'uid-x', 'o', 'c', ?)",
            (account_id, MARKET, AT),
        )
        connection.execute(
            f"INSERT INTO {SCOPES} ({_SCOPE_COLUMNS})"
            f" VALUES ({', '.join('?' for _ in _paused_row(account_id))})",
            _paused_row(account_id),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0016_m5_registration_foundation")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0017_m5_registration_execution_scope"
    finally:
        engine.dispose()


def test_0018_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    # §27: the preparation owner adds four tables, touches nothing else, round-trips, and never
    # lets an authored preparation be dropped silently.
    url = _url(tmp_path / "icbm.db")
    upgrade_to_head(url)
    before = _tables(tmp_path / "icbm.db")
    command.downgrade(alembic_config(url), "0017_m5_registration_execution_scope")
    # 0018 owns exactly the preparation tables; the tables later migrations add step down too.
    assert before - _tables(tmp_path / "icbm.db") == (
        set(PREPARATION_TABLES)
        | set(TARGET_POLICY_TABLES)
        | set(REVIEW_TABLES)
        | set(ADAPTIVE_TABLES)
    )
    command.upgrade(alembic_config(url), "head")
    assert _tables(tmp_path / "icbm.db") == before
    account_id = f"mpa-{'1' * 32}"
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        connection.execute(
            "INSERT INTO marketplace_connections (marketplace_key, credential_generation_hwm,"
            " session_generation_hwm, provider_account_uid, provider_account_id,"
            " bound_credential_generation, bound_session_generation, bound_at, bound_by,"
            " created_at, updated_at) VALUES (?, 1, 1, 'uid-x', NULL, 1, 1, ?, 'o', ?, ?)",
            (MARKET, AT, AT, AT),
        )
        connection.execute("INSERT INTO seller_entities VALUES ('seller-1', 'o', 'c', ?)", (AT,))
        connection.execute(
            "INSERT INTO marketplace_accounts VALUES (?, 'seller-1', ?, 'uid-x', 'o', 'c', ?)",
            (account_id, MARKET, AT),
        )
        connection.execute(
            "INSERT INTO registration_drafts VALUES"
            " ('draft-1', ?, ?, 'SINGLE_LISTING_WITH_OPTIONS', 1, 'o', ?, ?)",
            (MARKET, account_id, AT, AT),
        )
        connection.execute(
            f"INSERT INTO {PREPARATIONS} (preparation_id, draft_id, marketplace_key,"
            " marketplace_account_id, unit_membership_fingerprint, created_by, created_at)"
            " VALUES ('prep-1', 'draft-1', ?, ?, ?, 'o', ?)",
            (MARKET, account_id, "1" * 64, AT),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0017_m5_registration_execution_scope")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0018_m5_registration_preparation"
    finally:
        engine.dispose()


def _tables(database: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_0016_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    url = _url(tmp_path / "icbm.db")
    upgrade_to_head(url)
    command.downgrade(alembic_config(url), "0015_m4_quantity_offers")
    command.upgrade(alembic_config(url), "head")
    account_id = f"mpa-{'1' * 32}"
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        connection.execute(
            "INSERT INTO marketplace_connections (marketplace_key, credential_generation_hwm,"
            " session_generation_hwm, provider_account_uid, provider_account_id,"
            " bound_credential_generation, bound_session_generation, bound_at, bound_by,"
            " created_at, updated_at) VALUES (?, 1, 1, 'uid-x', NULL, 1, 1, ?, 'o', ?, ?)",
            (MARKET, AT, AT, AT),
        )
        connection.execute("INSERT INTO seller_entities VALUES ('seller-1', 'o', 'c', ?)", (AT,))
        connection.execute(
            "INSERT INTO marketplace_accounts VALUES (?, 'seller-1', ?, 'uid-x', 'o', 'c', ?)",
            (account_id, MARKET, AT),
        )
        connection.execute(
            "INSERT INTO registration_drafts (draft_id, marketplace_key, marketplace_account_id,"
            " listing_shape, draft_revision, created_by, created_at, updated_at)"
            " VALUES ('d', ?, ?, 'SEPARATE_LISTINGS', 1, 'o', ?, ?)",
            (MARKET, account_id, AT, AT),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0015_m4_quantity_offers")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0016_m5_registration_foundation"
    finally:
        engine.dispose()
