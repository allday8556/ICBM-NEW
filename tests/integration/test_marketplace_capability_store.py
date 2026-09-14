"""Marketplace capability truth through persistence, restart and the read API (M2 PR-B).

``test_s17_16_*`` and ``test_s17_17_*`` are the named tests for CAPABILITY_MAPPING.md §17
targets 16 and 17 at the persistence/read-API layer (the UI portion of 16 belongs to PR-D).
No SmartStore credential and no provider call: evidence is supplied as fixtures.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.connect.marketplace.capability import (
    AuthEvidence,
    AuthStatus,
    ContractFreshness,
    EvidenceStrength,
    FailureEvidence,
    Finding,
    Generations,
    IdentityProof,
    RemoteOutcome,
    Resolution,
    WorkflowScope,
    WriteScope,
    WriteScopeStatus,
)
from app.container import Container, build_container
from app.core.egress import EGRESS
from app.core.errors import ErrorClass, PolicyBlockedError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.main import create_app
from tests.conftest import LOCAL
from tests.support import FakeClock

pytestmark = pytest.mark.integration

KEY = "smartstore"
BASE = "/api/v1/connect/marketplaces"
EXPECTED = "account-uid-A"
T0 = datetime(2026, 9, 14, tzinfo=UTC)
ATTESTED = WriteScope(WriteScopeStatus.READY, EvidenceStrength.OPERATOR_ATTESTED)
MISSING = WriteScope(WriteScopeStatus.MISSING, EvidenceStrength.OPERATOR_ATTESTED)
VIEW_FIELDS = {
    "marketplace_key",
    "auth",
    "auth_verified_at",
    "write_scope",
    "write",
    "contract_freshness",
    "workflow",
    "error_class",
    "remote_outcome",
    "updated_at",
}


def _evidence(session: int = 1) -> AuthEvidence:
    generations = Generations(credential=1, session=session)
    return AuthEvidence(
        binding_committed=True,
        expected_account_uid=EXPECTED,
        current=generations,
        proof=IdentityProof(generations, EXPECTED, T0),
    )


@contextmanager
def _process(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    """One ICBM process on the data directory, released on exit (a later one is a restart)."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config, ownership=lease, clock=clock, secret_store=MemorySecretStore()
        )
        try:
            yield built
        finally:
            built.db.dispose()


def _rows(config: AppConfig, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        return raw.execute(sql, (KEY,)).fetchall()


# ---------------------------------------------------------------- §17, PR-B-owned targets


def test_s17_16_axes_are_separate_fields_in_persistence_and_the_read_api(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock) as process:
        capability = process.marketplace_capability
        capability.observe_permission(KEY, MISSING)
        capability.observe_failure(
            KEY,
            FailureEvidence(WorkflowScope.AUTHENTICATION, ErrorClass.UNKNOWN, Finding.UNRESOLVED),
        )
        capability.observe_failure(
            KEY,
            FailureEvidence(
                WorkflowScope.PRODUCT_REGISTRATION,
                ErrorClass.TRANSIENT,
                Finding.RECOVERABLE,
                remote_outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            ),
        )
        capability.record_contract_freshness(KEY, ContractFreshness.STALE, actor="operator:test")

    # Persistence: every axis in its own column; each overlay a row keyed by its typed scope.
    assert _rows(
        config,
        "SELECT auth, write_scope_status, evidence_strength, write_status, contract_freshness,"
        " error_class, remote_outcome FROM marketplace_capabilities WHERE marketplace_key = ?",
    ) == [
        (
            "NOT_BOUND",
            "MISSING",
            "OPERATOR_ATTESTED",
            "BLOCKED",
            "STALE",
            "TRANSIENT",
            "NOT_APPLIED_PROVEN",
        )
    ]
    assert _rows(
        config,
        "SELECT workflow_scope, workflow_state, reason_code FROM marketplace_workflow_overlays"
        " WHERE marketplace_key = ? ORDER BY workflow_scope",
    ) == [
        ("AUTHENTICATION", "REVIEW_REQUIRED", None),
        ("PRODUCT_REGISTRATION", "PAUSED", "SCOPE_INSUFFICIENT"),
    ]

    # Read API: the same axes, each its own field, nothing pre-combined for display.
    with TestClient(create_app(config), base_url=LOCAL) as client:
        body = client.get(f"{BASE}/{KEY}/capability").json()
        listed = client.get(f"{BASE}/capabilities").json()
    assert set(body) == VIEW_FIELDS
    assert body["auth"] == "NOT_BOUND"
    assert body["write_scope"] == {
        "status": "MISSING",
        "evidence_strength": "OPERATOR_ATTESTED",
        "evidence_grade": None,
    }
    assert body["write"] == {"status": "BLOCKED"}
    assert body["contract_freshness"] == "STALE"
    assert body["workflow"] == [
        {
            "workflow_state": "REVIEW_REQUIRED",
            "workflow_scope": "AUTHENTICATION",
            "reason_code": None,
        },
        {
            "workflow_state": "PAUSED",
            "workflow_scope": "PRODUCT_REGISTRATION",
            "reason_code": "SCOPE_INSUFFICIENT",
        },
    ]
    assert (body["error_class"], body["remote_outcome"]) == ("TRANSIENT", "NOT_APPLIED_PROVEN")
    assert listed == [body]


def test_s17_17_persisted_ready_loses_to_current_evidence_after_restart(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock) as first:
        first.marketplace_capability.record_contract_freshness(
            KEY, ContractFreshness.CURRENT, actor="operator:review"
        )
        assert first.marketplace_capability.observe_auth(KEY, _evidence()).auth is AuthStatus.READY
        first.marketplace_capability.observe_permission(KEY, ATTESTED)
    assert _rows(config, "SELECT auth FROM marketplace_capabilities WHERE marketplace_key = ?") == [
        ("READY",)
    ]

    # Restart through the real application: startup converges before any request is served.
    with TestClient(create_app(config), base_url=LOCAL) as client:
        body = client.get(f"{BASE}/{KEY}/capability").json()
    assert body["auth"] == "NOT_READY"
    assert body["write"] == {"status": "UNVERIFIED"}
    # The attestation is evidence, not a proof of this process; its own validity is PR-C's.
    assert body["write_scope"]["evidence_grade"] == "LIMITED"
    assert _rows(config, "SELECT auth FROM marketplace_capabilities WHERE marketplace_key = ?") == [
        ("NOT_READY",)
    ]

    # Only current evidence brings READY back.
    with _process(config, clock) as second:
        assert (
            second.marketplace_capability.observe_auth(KEY, _evidence(2)).auth is AuthStatus.READY
        )


# ---------------------------------------------------------------- persistence guards


def _capability(
    *,
    auth: str = "'NOT_BOUND'",
    verified: str = "NULL",
    scope: str = "'UNKNOWN'",
    strength: str = "NULL",
    write: str = "'UNVERIFIED'",
    error: str = "NULL",
) -> str:
    """A marketplace_capabilities row that is valid except for the values overridden."""
    return (
        "INSERT INTO marketplace_capabilities VALUES ('x', "
        f"{auth}, {verified}, {scope}, {strength}, {write}, 'CURRENT', {error}, NULL, NULL, "
        "'2026-09-14', '2026-09-14')"
    )


def _overlay_row(scope: str, state: str, reason: str = "NULL") -> str:
    return (
        "INSERT INTO marketplace_workflow_overlays VALUES ('smartstore', "
        f"'{scope}', '{state}', {reason}, NULL, '2026-09-14')"
    )


ATTESTED_SQL = "'OPERATOR_ATTESTED'"
FORBIDDEN = [
    pytest.param(_capability(auth="'CONNECTED_WITH_PERMISSION'"), id="composite-is-no-axis-value"),
    pytest.param(
        _capability(
            auth="'READY'",
            verified="'2026-09-14'",
            scope="'READY'",
            strength=ATTESTED_SQL,
            write="'READY'",
        ),
        id="W1-m2-write-never-ready",
    ),
    pytest.param(_capability(auth="'READY'"), id="A1-ready-needs-its-proof-time"),
    pytest.param(_capability(scope="'READY'"), id="S1-ready-needs-a-strength"),
    pytest.param(_capability(strength=ATTESTED_SQL), id="S1-unknown-carries-no-strength"),
    pytest.param(
        _capability(scope="'MISSING'", strength=ATTESTED_SQL), id="S2-missing-blocks-write"
    ),
    pytest.param(_capability(error="'GW.AUTHN'"), id="error-class-is-canonical-only"),
    pytest.param(_overlay_row("SMARTSTORE_PRODUCT_API", "REVIEW_REQUIRED"), id="scope-is-frozen"),
    pytest.param(_overlay_row("AUTHENTICATION", "PAUSED"), id="paused-needs-a-reason"),
    pytest.param(
        _overlay_row("AUTHENTICATION", "REVIEW_REQUIRED", "'AUTH_RETRY_LIMIT'"),
        id="review-carries-no-reason",
    ),
    pytest.param(
        _overlay_row("AUTHENTICATION", "PAUSED", "'SCOPE_INSUFFICIENT'"),
        id="reason-fits-its-scope",
    ),
    pytest.param(
        _overlay_row("AUTHENTICATION", "PAUSED", "'APPLICATION_REAUTH_REQUIRED'"),
        id="app-reauth-records-its-generation",
    ),
    pytest.param(
        _overlay_row("PRODUCT_REGISTRATION", "REVIEW_REQUIRED"), id="one-overlay-per-scope"
    ),
]


@pytest.mark.parametrize("statement", FORBIDDEN)
def test_the_database_refuses_forbidden_values_and_combinations(
    config: AppConfig, clock: FakeClock, statement: str
) -> None:
    with _process(config, clock) as process:
        process.marketplace_capability.observe_permission(KEY, MISSING)  # a valid row + overlay
    with sqlite3.connect(config.data_dir / "icbm.db") as raw, pytest.raises(sqlite3.IntegrityError):
        raw.execute(statement)


# ---------------------------------------------------------------- service behaviour


def test_an_unconnected_marketplace_starts_unbound_unverified_and_unreviewed(
    client: TestClient,
) -> None:
    body = client.get(f"{BASE}/{KEY}/capability").json()
    # F4: a fresh install never asserts that the adopted contract is CURRENT.
    assert (body["auth"], body["write"], body["contract_freshness"]) == (
        "NOT_BOUND",
        {"status": "UNVERIFIED"},
        "REVIEW_REQUIRED",
    )
    assert body["write_scope"] == {
        "status": "UNKNOWN",
        "evidence_strength": None,
        "evidence_grade": None,
    }
    assert (body["workflow"], body["error_class"], body["remote_outcome"]) == ([], None, None)
    missing = client.get(f"{BASE}/coupang/capability")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "MARKETPLACE_CAPABILITY_UNKNOWN"


def test_a_fresh_install_needs_recorded_freshness_before_any_new_trust(
    config: AppConfig, clock: FakeClock
) -> None:
    # PR #26 audit 5657617043 (F4): a clean install never starts CURRENT by default.
    with _process(config, clock) as process:
        capability = process.marketplace_capability
        assert capability.capability(KEY).contract_freshness is ContractFreshness.REVIEW_REQUIRED
        with pytest.raises(PolicyBlockedError):
            capability.observe_auth(KEY, _evidence())
        with pytest.raises(PolicyBlockedError):
            capability.observe_permission(KEY, ATTESTED)
        # Fail-closed evidence still converges while freshness is unrecorded.
        assert capability.observe_permission(KEY, MISSING).write.status == "BLOCKED"
        # The explicit, audited recording is what unlocks new trust.
        capability.record_contract_freshness(
            KEY, ContractFreshness.CURRENT, actor="operator:review"
        )
        assert capability.observe_auth(KEY, _evidence()).auth is AuthStatus.READY
        recorded = [
            e
            for e in process.audit.list_events(limit=20)
            if e.action == "RECORD_CONTRACT_FRESHNESS"
        ]
    assert [(e.actor, e.outcome) for e in recorded] == [("operator:review", "ALLOWED")]
    assert recorded[0].before is not None and recorded[0].after is not None
    assert (recorded[0].before["contract_freshness"], recorded[0].after["contract_freshness"]) == (
        "REVIEW_REQUIRED",
        "CURRENT",
    )


def test_every_change_is_audited_with_axis_values_only(config: AppConfig, clock: FakeClock) -> None:
    with _process(config, clock) as process:
        capability = process.marketplace_capability
        capability.record_contract_freshness(KEY, ContractFreshness.CURRENT, actor="operator:x")
        capability.observe_auth(KEY, _evidence())
        capability.observe_failure(
            KEY,
            FailureEvidence(
                WorkflowScope.AUTHENTICATION,
                ErrorClass.AUTH,
                Finding.AUTH_RECOVERY_EXHAUSTED,
                provider_code="GW.AUTHN",
            ),
        )
        capability.resolve(KEY, WorkflowScope.AUTHENTICATION, Resolution.RESUME, actor="operator:x")
        capability.observe_failure(  # no change: nothing new is recorded
            KEY,
            FailureEvidence(
                WorkflowScope.AUTHENTICATION,
                ErrorClass.AUTH,
                Finding.RECOVERABLE,
                provider_code="GW.AUTHN",
            ),
        )
        events = [
            e
            for e in process.audit.list_events(limit=50)
            if e.event_type == "MARKETPLACE_CAPABILITY_CHANGED"
        ]
    assert [e.action for e in reversed(events)] == [
        "RECORD_CONTRACT_FRESHNESS",
        "OBSERVE_AUTH",
        "OBSERVE_FAILURE",
        "RESOLVE_WORKFLOW",
    ]
    paused = next(e for e in events if e.action == "OBSERVE_FAILURE")
    assert paused.after is not None
    assert paused.after["workflow"] == ["AUTHENTICATION:PAUSED:AUTH_RETRY_LIMIT"]
    assert paused.after["error_class"] == "AUTH"
    resumed = next(e for e in events if e.action == "RESOLVE_WORKFLOW")
    assert (resumed.actor, resumed.outcome, resumed.details["resolution"]) == (
        "operator:x",
        "ALLOWED",
        "RESUME",
    )
    # Provider codes are diagnostics, never durable state: none reaches the database.
    assert "GW.AUTHN" not in json.dumps([e.model_dump(mode="json") for e in events])
    assert _rows(
        config, "SELECT error_class FROM marketplace_capabilities WHERE marketplace_key = ?"
    ) == [("AUTH",)]


def test_new_trust_under_a_stale_contract_is_a_policy_refusal(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock) as process:
        capability = process.marketplace_capability
        capability.record_contract_freshness(KEY, ContractFreshness.STALE, actor="operator:test")
        with pytest.raises(PolicyBlockedError) as refused:
            capability.observe_permission(KEY, ATTESTED)
        assert refused.value.code == "MARKETPLACE_CONTRACT_NOT_CURRENT"
        assert capability.capability(KEY).write_scope.status is WriteScopeStatus.UNKNOWN


def test_capability_handling_makes_no_network_call(config: AppConfig, clock: FakeClock) -> None:
    EGRESS.install()
    before = EGRESS.snapshot()
    with _process(config, clock) as process:
        capability = process.marketplace_capability
        capability.record_contract_freshness(KEY, ContractFreshness.CURRENT, actor="operator:test")
        capability.observe_auth(KEY, _evidence())
        capability.observe_permission(KEY, MISSING)
        capability.observe_failure(
            KEY,
            FailureEvidence(
                WorkflowScope.AUTHENTICATION,
                ErrorClass.AUTH,
                Finding.APPLICATION_REAUTH_SUSPECTED,
                provider_code="GW.AUTHN",
            ),
        )
        capability.resolve(
            KEY, WorkflowScope.AUTHENTICATION, Resolution.REVIEW_RESOLVED, actor="operator:test"
        )
        capability.record_contract_freshness(KEY, ContractFreshness.REVIEW_REQUIRED, actor="op")
        capability.normalize_on_startup()
    with TestClient(create_app(config), base_url=LOCAL) as client:
        assert client.get(f"{BASE}/capabilities").status_code == 200
        assert client.get(f"{BASE}/{KEY}/capability").status_code == 200
    after = EGRESS.snapshot()
    assert after["external_attempts"] == before["external_attempts"]
    assert after["granted_events"] == before["granted_events"]
