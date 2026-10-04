"""The durable registration target policy over the real application (Gate 1 G1-A, ADR-0015 §2).

What this proves, from the outside in:
- a Settings save appends one server-created revision and moves one current pointer, and nothing
  is ever rewritten or deleted — not by the owner, and not by SQL either;
- the server creates the revision identity and the fingerprint, and refuses a client that tries;
- an invalid, unsafe or scope-crossing save is refused whole: no policy, revision, pointer or audit
  row appears;
- one policy per marketplace × canonical account, with strict isolation between accounts;
- the policy survives a restart exactly;
- the production preflight reads this owner: it leaves `REGISTER_TARGET_POLICY_MISSING` only for a
  valid current policy, a new revision stales an earlier candidate, and a request whose authoring
  revisions are not exactly the ones the policy holds is never READY and freezes no Snapshot
  (decisions 5800619183, 5801915996).

No provider is reached: a target policy is local configuration.
"""

import contextlib
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.capabilities.audit.models import AuditEventType
from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.platform.core.errors import InputValidationError, PolicyBlockedError
from app.platform.db.database import create_sqlite_engine
from app.platform.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.stages.products.model import ReadinessStatus
from app.stages.register.authoring_revisions import AuthoringRevisionKind
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.model import RegistrationConflictError
from app.stages.register.policy import StaticRegistrationMetadata
from app.stages.register.preflight import RegistrationPreflightService
from app.stages.register.preparation import AUTHORING_REVISIONS_UNOWNED
from app.stages.register.target_policy import DurableRegistrationPolicy
from tests.conftest import LOCAL
from tests.support.product_support import Collections, raw
from tests.support.register_support import (
    CID,
    MARKET,
    OPERATOR,
    TAXONOMY,
    FakeCapability,
    draft,
    establish,
    metadata,
    no_match,
    prepared,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
BASE = "/api/v1/settings/target-policies"
POLICIES = "registration_target_policies"
REVISIONS = "registration_target_policy_revisions"
CURRENT = "registration_target_policy_current"
TABLES = (POLICIES, REVISIONS, CURRENT)


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


REFERENCES = ("category_mapping_revision", "detail_composition_revision")


def authored(view_inputs: dict[str, Any]) -> dict[str, Any]:
    """The inputs a stored revision answers with, as they were authored: the two authoring
    revisions are the server's stamp (ADR-0014 §27.1), never an authored value."""
    assert all(isinstance(view_inputs[name], str) and view_inputs[name] for name in REFERENCES)
    return {**view_inputs, **dict.fromkeys(REFERENCES)}


def inputs(**overrides: Any) -> dict[str, Any]:
    """The supported surface, fully authored: the same policy `register_support.target()` holds."""
    values: dict[str, Any] = {
        "taxonomy_revision": TAXONOMY,
        "pricing_context": {
            "marketplace_key": MARKET,
            "account_id": None,
            "fee_table_version": "fee-test-1",
            "pricing_policy_version": "policy-test-1",
            "fee_rate": "0.1",
            "fee_fixed_krw": 0,
            "other_cost_rate": "0",
            "other_cost_fixed_krw": 0,
            "cost_rounding": "CEIL_KRW_1",
            "price_rounding": "CEIL_KRW_1",
        },
        "sanitizer_profile_version": "sanitizer-test-1",
        "asset_policy": {
            "profile": "asset-profile-test-1",
            "min_images": 1,
            "max_images": 10,
            "requires_representative": True,
            "provider_asset_identity_required": True,
        },
        "templates": {"shipping": "shipping-template-test", "returns": "returns-template-test"},
        "duplicate_proof_required": True,
        "duplicate_lookup_keys": ["SELLER_CODE"],
        # Server-owned references: only an explicit null is valid; the server stamps them.
        "category_mapping_revision": None,
        "detail_composition_revision": None,
    }
    values.update(overrides)
    return values


def save(
    api: TestClient,
    account: str,
    body_inputs: dict[str, Any],
    expected: str | None = None,
    **extra: Any,
) -> Any:
    return api.post(
        f"{BASE}/{MARKET}/{account}/revisions",
        json={
            "actor": OPERATOR,
            "expected_current_revision": expected,
            "inputs": body_inputs,
            **extra,
        },
        headers=CLIENT,
    )


def counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }


def policy_events(container: Container) -> list[Any]:
    return [
        event
        for event in container.audit.list_events(limit=500)
        if event.event_type == AuditEventType.REGISTRATION_TARGET_POLICY_REVISED
    ]


# ------------------------------------------------------------------ the owner and its history


def test_a_first_save_creates_the_policy_its_revision_and_its_current_pointer(
    api: TestClient, container: Container, config: AppConfig, account: str
) -> None:
    before = api.get(f"{BASE}/{MARKET}/{account}", headers=CLIENT).json()
    assert before["editable"] is True
    assert before["current"] is None and before["history"] == [] and before["inputs"] is None

    saved = save(api, account, inputs())
    assert saved.status_code == 200, saved.text
    body = saved.json()
    current = body["current"]
    assert current["revision_no"] == 1 and current["current"] is True
    assert len(current["content_fingerprint"]) == 64
    assert current["authored_by"] == OPERATOR
    assert authored(body["inputs"]) == inputs()
    assert [entry["policy_revision"] for entry in body["history"]] == [current["policy_revision"]]
    assert counts(config) == {POLICIES: 1, REVISIONS: 1, CURRENT: 1}

    events = policy_events(container)
    assert len(events) == 1
    assert events[0].after == {
        "policy_revision": current["policy_revision"],
        "revision_no": 1,
        "content_fingerprint": current["content_fingerprint"],
    }
    # The audit record carries identifiers only, never a policy value.
    assert "shipping-template-test" not in str(events[0].model_dump())


def test_a_save_appends_a_revision_and_never_rewrites_the_one_before(
    api: TestClient, config: AppConfig, account: str
) -> None:
    first = save(api, account, inputs()).json()["current"]
    changed = inputs(
        templates={"shipping": "shipping-template-2", "returns": "returns-template-test"}
    )
    second = save(api, account, changed, expected=first["policy_revision"]).json()

    assert second["current"]["revision_no"] == 2
    assert authored(second["inputs"]) == changed
    history = second["history"]
    assert [entry["revision_no"] for entry in history] == [2, 1]
    assert [entry["current"] for entry in history] == [True, False]
    older = history[1]
    assert older["policy_revision"] == first["policy_revision"]
    assert older["content_fingerprint"] == first["content_fingerprint"]
    assert counts(config) == {POLICIES: 1, REVISIONS: 2, CURRENT: 1}


def test_the_policy_survives_a_restart_exactly(config: AppConfig) -> None:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        served: Container = client.app.state.container
        account = establish(served, config, MARKET, "uid-market-a-1")
        saved = save(client, account, inputs()).json()
    with TestClient(create_app(config), base_url=LOCAL) as client:
        again = client.get(f"{BASE}/{MARKET}/{account}", headers=CLIENT).json()
        restarted: Container = client.app.state.container
        target = restarted.registration_preflight.target_policy(MARKET, account)
    assert again == saved
    assert target is not None
    assert target.policy_revision == saved["current"]["policy_revision"]
    assert dict(target.templates) == inputs()["templates"]


def test_policies_are_one_per_account_and_never_cross_accounts(
    api: TestClient, container: Container, config: AppConfig, account: str
) -> None:
    # A later explicit rebinding establishes a second canonical account in the same marketplace.
    other = establish(container, config, MARKET, "uid-market-a-2")
    assert other != account
    save(api, account, inputs())

    assert api.get(f"{BASE}/{MARKET}/{other}", headers=CLIENT).json()["current"] is None
    listed = api.get(f"{BASE}/{MARKET}", headers=CLIENT).json()["accounts"]
    assert {entry["marketplace_account_id"]: entry["current"] is not None for entry in listed} == {
        account: True,
        other: False,
    }
    # A pricing context naming another account is refused for this one.
    crossing = inputs(pricing_context={**inputs()["pricing_context"], "account_id": account})
    refused = save(api, other, crossing)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "TARGET_POLICY_PRICING_CONTEXT_INVALID"
    # The account belongs to its own marketplace only.
    assert api.get(f"{BASE}/market_b/{account}", headers=CLIENT).status_code == 404
    assert counts(config) == {POLICIES: 1, REVISIONS: 1, CURRENT: 1}


def test_an_unknown_account_has_no_policy_and_gets_none(api: TestClient, config: AppConfig) -> None:
    missing = f"mpa-{'0' * 32}"
    assert api.get(f"{BASE}/{MARKET}/{missing}", headers=CLIENT).status_code == 404
    refused = save(api, missing, inputs())
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "TARGET_POLICY_ACCOUNT_UNKNOWN"
    assert counts(config) == {POLICIES: 0, REVISIONS: 0, CURRENT: 0}


# ------------------------------------------------------------------ the server owns the identity


@pytest.mark.parametrize(
    "extra",
    [
        {"policy_revision": "client-made-revision"},
        {"content_fingerprint": "f" * 64},
    ],
)
def test_a_client_supplied_revision_identity_or_fingerprint_is_refused(
    api: TestClient, config: AppConfig, account: str, extra: dict[str, str]
) -> None:
    at_request = save(api, account, inputs(), **extra)
    in_inputs = save(api, account, inputs(**extra))
    assert at_request.status_code == 422
    assert in_inputs.status_code == 422
    assert counts(config) == {POLICIES: 0, REVISIONS: 0, CURRENT: 0}


def _without(key: str) -> dict[str, Any]:
    values = inputs()
    del values[key]
    return values


INVALID = {
    "a missing field is never defaulted": (_without("templates"), 422, None),
    "an optional revision must be an explicit null": (
        _without("detail_composition_revision"),
        422,
        None,
    ),
    "a rate is decimal text, never a float": (
        inputs(pricing_context={**inputs()["pricing_context"], "fee_rate": 0.1}),
        422,
        None,
    ),
    "a pricing context of another marketplace": (
        inputs(pricing_context={**inputs()["pricing_context"], "marketplace_key": "market_b"}),
        422,
        "TARGET_POLICY_PRICING_CONTEXT_INVALID",
    ),
    "a pricing context M4 would refuse": (
        inputs(pricing_context={**inputs()["pricing_context"], "fee_rate": "1.5"}),
        422,
        "TARGET_POLICY_PRICING_CONTEXT_INVALID",
    ),
    "a URL as a template identity": (
        inputs(templates={"shipping": "https://example.invalid/t", "returns": "r-1"}),
        422,
        "TARGET_POLICY_INVALID",
    ),
    "a credential-named template": (
        inputs(templates={"token": "value-1", "returns": "r-1"}),
        422,
        "TARGET_POLICY_UNSAFE_CONTENT",
    ),
    "image bounds that contradict": (
        inputs(asset_policy={**inputs()["asset_policy"], "min_images": 5, "max_images": 2}),
        422,
        "TARGET_POLICY_INVALID",
    ),
    "a lookup key listed twice": (
        inputs(duplicate_lookup_keys=["SELLER_CODE", "SELLER_CODE"]),
        422,
        "TARGET_POLICY_INVALID",
    ),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_an_invalid_save_is_refused_whole_and_writes_nothing(
    api: TestClient, container: Container, config: AppConfig, account: str, case: str
) -> None:
    body, status, code = INVALID[case]
    refused = save(api, account, body)
    assert refused.status_code == status, refused.text
    if code is not None:
        assert refused.json()["error"]["code"] == code
    assert counts(config) == {POLICIES: 0, REVISIONS: 0, CURRENT: 0}
    assert policy_events(container) == []
    # The same holds on top of an existing policy: nothing partial is appended.
    first = save(api, account, inputs()).json()["current"]
    again = save(api, account, body, expected=first["policy_revision"])
    assert again.status_code == status
    assert counts(config) == {POLICIES: 1, REVISIONS: 1, CURRENT: 1}
    assert len(policy_events(container)) == 1


@pytest.mark.parametrize("reference", ["category_mapping_revision", "detail_composition_revision"])
def test_an_invented_authoring_revision_reference_is_refused_and_writes_nothing(
    api: TestClient, container: Container, config: AppConfig, account: str, reference: str
) -> None:
    # ADR-0015 §2: server-owned references. A client cannot name one — however plausible the
    # label — and only an explicit null is accepted; the server stamps the owner's revisions.
    invented = save(api, account, inputs(**{reference: "mapping-test-1"}))
    assert invented.status_code == 422
    error = invented.json()["error"]
    assert error["code"] == "TARGET_POLICY_AUTHORING_REVISION_UNOWNED"
    assert error["details"] == {"field": reference}
    assert counts(config) == {POLICIES: 0, REVISIONS: 0, CURRENT: 0}
    assert policy_events(container) == []

    first = save(api, account, inputs())
    assert first.status_code == 200
    assert first.json()["inputs"][reference] not in (None, "mapping-test-1")
    current = first.json()["current"]["policy_revision"]
    again = save(api, account, inputs(**{reference: "detail-test-1"}), expected=current)
    assert again.status_code == 422
    assert again.json()["error"]["code"] == "TARGET_POLICY_AUTHORING_REVISION_UNOWNED"
    assert counts(config) == {POLICIES: 1, REVISIONS: 1, CURRENT: 1}
    assert len(policy_events(container)) == 1
    # The production preflight's policy carries no invented reference either: only the two
    # revisions the authoring-revision owner holds.
    target = container.registration_preflight.target_policy(MARKET, account)
    assert target is not None
    mapping = container.authoring_revisions.current(
        AuthoringRevisionKind.CATEGORY_MAPPING, MARKET, target.taxonomy_revision
    )
    composition = container.authoring_revisions.current(
        AuthoringRevisionKind.DETAIL_COMPOSITION, MARKET
    )
    assert mapping is not None and composition is not None
    assert (target.category_mapping_revision, target.detail_composition_revision) == (
        mapping.revision_id,
        composition.revision_id,
    )


def test_a_save_against_a_moved_or_identical_policy_is_refused(
    api: TestClient, config: AppConfig, account: str
) -> None:
    first = save(api, account, inputs()).json()["current"]
    stale = save(api, account, inputs(taxonomy_revision="taxonomy-test-2"), expected=None)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "TARGET_POLICY_CURRENT_MOVED"
    same = save(api, account, inputs(), expected=first["policy_revision"])
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "TARGET_POLICY_UNCHANGED"
    assert counts(config) == {POLICIES: 1, REVISIONS: 1, CURRENT: 1}


# ------------------------------------------------------------------ the database keeps the rules


def test_the_database_refuses_to_rewrite_history_or_move_the_pointer_back(
    api: TestClient, config: AppConfig, account: str
) -> None:
    first = save(api, account, inputs()).json()["current"]["policy_revision"]
    second = save(api, account, inputs(taxonomy_revision="taxonomy-test-2"), expected=first).json()[
        "current"
    ]["policy_revision"]
    other = establish(api.app.state.container, config, MARKET, "uid-market-a-2")
    foreign = save(api, other, inputs()).json()["current"]["policy_revision"]
    backwards = (
        f"UPDATE {CURRENT} SET policy_revision_id = '{first}' WHERE policy_revision_id = '{second}'"
    )
    across = (
        f"UPDATE {CURRENT} SET policy_revision_id = '{foreign}'"
        f" WHERE policy_revision_id = '{second}'"
    )
    with contextlib.closing(raw(config)) as connection:
        # The pointer never moves back to an older revision, and never to another policy's one.
        for statement in (backwards, across):
            with pytest.raises(sqlite3.IntegrityError, match="newest of its own policy"):
                connection.execute(statement)
    statements = {
        "never updated": f"UPDATE {REVISIONS} SET authored_by = 'x'",
        "never deleted": f"DELETE FROM {REVISIONS}",
        f"{CURRENT} row is never deleted": f"DELETE FROM {CURRENT}",
        f"{POLICIES} is never updated": f"UPDATE {POLICIES} SET created_by = 'x'",
        f"{POLICIES} is never deleted": f"DELETE FROM {POLICIES}",
        "a revision follows the one before it": (
            f"INSERT INTO {REVISIONS} SELECT 'r-gap', policy_id, revision_no + 5, content_json,"
            f" content_fingerprint, authored_by, correlation_id, authored_at FROM {REVISIONS}"
            f" WHERE policy_revision_id = '{second}'"
        ),
        "names another marketplace account": (
            f"INSERT INTO {REVISIONS} SELECT 'r-cross', r.policy_id, 3,"
            f" (SELECT content_json FROM {REVISIONS} WHERE policy_revision_id = '{foreign}'),"
            f" r.content_fingerprint, r.authored_by, r.correlation_id, r.authored_at"
            f" FROM {REVISIONS} r WHERE r.policy_revision_id = '{second}'"
        ),
    }
    with contextlib.closing(raw(config)) as connection:
        for message, statement in statements.items():
            with pytest.raises(sqlite3.IntegrityError, match=message):
                connection.execute(statement)
    assert counts(config) == {POLICIES: 2, REVISIONS: 3, CURRENT: 2}


def test_0019_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}"
    upgrade_to_head(url)
    before = _tables(tmp_path / "icbm.db")
    command.downgrade(alembic_config(url), "0018_m5_registration_preparation")
    # 0019 owns exactly these three; the tables later revisions add step down with it.
    assert before - _tables(tmp_path / "icbm.db") == set(TABLES) | {
        "registration_category_metadata",
        "registration_category_metadata_revisions",
        "registration_category_metadata_current",
        "review_items",
        "review_item_events",
        "review_coverage",
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
        "live_grants",
        "protected_write_brakes",
        "asset_upload_attempts",
        "adaptive_capture_requests",
        "adaptive_capture_candidates",
        "adaptive_phase_c_commands",
        "adaptive_phase_c_command_results",
        "adaptive_phase_c_read_budgets",
        "adaptive_phase_c_reads",
        "adaptive_phase_c_read_refusals",
        "restore_drills",
        "retention_proofs",
        "visual_acceptances",
        "registration_reconcile_checks",
        "registration_authoring_revisions",
        "canary_eligibility_records",
        "extension_queues",
        "extension_queue_items",
        "residual_risk_acceptances",
        "registration_deletions",
        "synthetic_test_products",
        "supplier_common_image_decisions",
        "marketplace_category_catalog_snapshots",
        "marketplace_category_catalog_entries",
        "common_sales_option_revisions",
        "common_sales_option_axes",
        "common_sales_option_values",
        "current_common_sales_option_revision_moves",
        "common_option_fact_mapping_revisions",
        "common_option_fact_axis_mappings",
        "common_option_fact_value_mappings",
        "current_common_option_fact_mapping_moves",
        "atomic_sku_set_revisions",
        "atomic_skus",
        "atomic_sku_selections",
        "atomic_sku_revision_members",
        "atomic_sku_revision_selection_evidence",
        "current_atomic_sku_set_moves",
        "atomic_sku_product_items",
        "registration_bulk_runs",
        "registration_bulk_items",
    }
    command.upgrade(alembic_config(url), "head")
    assert _tables(tmp_path / "icbm.db") == before
    account_id = f"mpa-{'1' * 32}"
    at = "2026-09-23 00:00:00"
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        connection.execute(
            "INSERT INTO marketplace_connections (marketplace_key, credential_generation_hwm,"
            " session_generation_hwm, provider_account_uid, provider_account_id,"
            " bound_credential_generation, bound_session_generation, bound_at, bound_by,"
            " created_at, updated_at) VALUES (?, 1, 1, 'uid-x', NULL, 1, 1, ?, 'o', ?, ?)",
            (MARKET, at, at, at),
        )
        connection.execute("INSERT INTO seller_entities VALUES ('seller-1', 'o', 'c', ?)", (at,))
        connection.execute(
            "INSERT INTO marketplace_accounts VALUES (?, 'seller-1', ?, 'uid-x', 'o', 'c', ?)",
            (account_id, MARKET, at),
        )
        connection.execute(
            f"INSERT INTO {POLICIES} VALUES ('policy-1', ?, ?, 'o', 'c', ?)",
            (MARKET, account_id, at),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0018_m5_registration_preparation")
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0019_g1_registration_target_policy"
    finally:
        engine.dispose()


def _tables(database: Path) -> set[str]:
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


# ------------------------------------------------------------------ the preflight reads this owner


def _served_preflight(container: Container) -> RegistrationPreflightService:
    """The application's own preflight, with the durable policy source it was built with.

    Only the category metadata and the capability are test sources: the reviewed metadata owner is
    G1-B, and no provider capability is read here. The policy source is never replaced.
    """
    served = container.registration_preflight
    assert isinstance(served._policies, DurableRegistrationPolicy)
    served._metadata = StaticRegistrationMetadata((metadata(),), marketplace_key=MARKET)
    served._capability = FakeCapability()
    return served


def test_the_preflight_leaves_policy_missing_only_for_a_valid_current_policy(
    api: TestClient, container: Container, config: AppConfig, account: str
) -> None:
    preflight = _served_preflight(container)
    item = ready_item(container, Collections.of(container, config))
    req = request(
        container.registrations, draft(container.registrations, account, [item]), account, [item]
    )

    with pytest.raises(PolicyBlockedError, match="registration policy"):
        preflight.candidate(req)
    assert (
        save(api, account, inputs(templates={"shipping": "https://x.invalid"})).status_code == 422
    )
    with pytest.raises(PolicyBlockedError, match="registration policy"):
        preflight.candidate(req)

    current = save(api, account, inputs()).json()["current"]
    result = preflight.candidate(req)
    assert result.resolved.target.policy_revision == current["policy_revision"]
    assert "REGISTER_TARGET_POLICY_MISSING" not in {reason.code for reason in result.reasons}


def test_a_new_policy_revision_stales_an_earlier_candidate(
    api: TestClient, container: Container, config: AppConfig, account: str
) -> None:
    preflight = _served_preflight(container)
    first = save(api, account, inputs()).json()["current"]
    item = ready_item(container, Collections.of(container, config))
    req = request(
        container.registrations, draft(container.registrations, account, [item]), account, [item]
    )
    req = replace(req, duplicate_evidence=no_match(preflight.candidate(req)))
    candidate = preflight.candidate(req)
    # Every rule passes except one: the request carries authoring revisions no owner issued,
    # not the ones the policy holds (decision 5801915996), so it is never READY.
    assert candidate.status is ReadinessStatus.REVIEW_REQUIRED
    assert candidate.codes == (AUTHORING_REVISIONS_UNOWNED,)
    assets = prepared(candidate)
    assert preflight.final(req, assets).codes == (AUTHORING_REVISIONS_UNOWNED,)

    changed = inputs(
        templates={"shipping": "shipping-template-2", "returns": "returns-template-test"}
    )
    assert save(api, account, changed, expected=first["policy_revision"]).status_code == 200

    again = preflight.candidate(req)
    assert again.candidate_fingerprint != candidate.candidate_fingerprint
    stale = preflight.final(req, assets)
    assert stale.status is ReadinessStatus.STALE
    assert "PREPARED_ASSET_CANDIDATE_MISMATCH" in {reason.code for reason in stale.reasons}


def test_a_durable_policy_without_owned_authoring_revisions_freezes_nothing(
    api: TestClient, container: Container, config: AppConfig, account: str
) -> None:
    """The durable policy holds the owner's two authoring revisions (ADR-0014 §27.1). A request
    carrying revisions of its own — which no owner issued — is still unowned: the evaluation
    records the policy revision it read, and no Snapshot is frozen, by any path."""
    preflight = _served_preflight(container)
    first = save(api, account, inputs()).json()["current"]
    item = ready_item(container, Collections.of(container, config))
    req = request(
        container.registrations, draft(container.registrations, account, [item]), account, [item]
    )
    assert req.category is not None and req.category.mapping_revision == "mapping-test-1"
    req = replace(req, duplicate_evidence=no_match(preflight.candidate(req)))
    final = preflight.final(req, prepared(preflight.candidate(req)))
    assert final.status is ReadinessStatus.REVIEW_REQUIRED
    assert final.codes == (AUTHORING_REVISIONS_UNOWNED,)
    assert final.resolved.target.policy_revision == first["policy_revision"]
    builder = RegistrationSnapshotBuilder(
        preflight=preflight, registrations=container.registrations
    )
    with pytest.raises(InputValidationError, match="final READY"):
        builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    forged = replace(final, status=ReadinessStatus.READY)
    with pytest.raises(RegistrationConflictError) as stale:
        builder.freeze(forged, created_by=OPERATOR, correlation_id=CID)
    assert stale.value.code == "REGISTER_PREFLIGHT_STALE"
    with (
        pytest.raises(RegistrationConflictError) as unowned,
        container.registrations.transaction() as unit,
    ):
        builder._freeze_fresh(
            unit, forged, created_by=OPERATOR, correlation_id=CID, preparation_revision_id=None
        )
    assert unowned.value.code == "REGISTER_AUTHORING_REVISIONS_UNOWNED"
    with contextlib.closing(raw(config)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM registration_snapshots").fetchone() == (0,)
