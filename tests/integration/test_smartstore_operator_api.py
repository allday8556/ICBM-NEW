"""SmartStore operator actions over the real application (M2 PR-E; instructions §8A, Issue #44).

Every provider response comes from a fake transport behind the real registry-gated caller: no
test here makes a real SmartStore call or reaches a network (§8A, Issue #44 §6). PR-E owns no
CAPABILITY_MAPPING §17 target, so no test here is named for one (ownership registry §3.2).
"""

import logging
import sqlite3
import urllib.parse
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.audit.models import AuditEventType
from app.audit.service import AuditEntry, AuditLog
from app.config import AppConfig
from app.connect.marketplace.capability import (
    FailureEvidence,
    Finding,
    PauseReason,
    Resolution,
    WorkflowOverlay,
    WorkflowScope,
    WorkflowState,
    resolution_for,
)
from app.container import Container
from app.core.egress import EGRESS
from app.core.errors import ErrorClass
from app.main import create_app
from integrations.marketplaces.smartstore.caller import SmartStoreEndpointCaller
from tests.conftest import LOCAL

pytestmark = pytest.mark.integration

# Fixture values; none is a real credential or account.
SECRET = "$2a$04$abcdefghijklmnopqrstuu"
OTHER_SECRET = "$2a$04$zyxwvutsrqponmlkjihgfe"
CLIENT_ID = "fixture-client-id-pe01"
OTHER_CLIENT_ID = "fixture-client-id-pe02"
UID_A = "uid-fixture-A"
UID_B = "uid-fixture-B"
CLIENT = {"X-ICBM-Client": "pytest"}
KEY = "smartstore"
MARKETPLACE = f"/api/v1/connect/marketplaces/{KEY}"
TOKEN_PATH = "/external/v1/oauth2/token"


class Provider:
    """A fake SmartStore behind the real caller: numbered tokens, a switchable account."""

    def __init__(self) -> None:
        self.account_uid = UID_A
        self.calls: list[str] = []
        self.client_ids: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            self.calls.append("TOKEN")
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            self.client_ids.append(form["client_id"])
            token = f"fixture-token-{self.calls.count('TOKEN')}"
            return httpx.Response(
                200, json={"access_token": token, "expires_in": 10800, "token_type": "Bearer"}
            )
        self.calls.append("ACCOUNT")
        body = {"accountUid": self.account_uid, "accountId": f"id-{self.account_uid}"}
        return httpx.Response(200, json=body)


def _app(config: AppConfig, provider: Provider):  # type: ignore[no-untyped-def]
    return create_app(
        config.with_overrides(smartstore_renewal_margin_s=600),
        smartstore_caller=SmartStoreEndpointCaller(transport=httpx.MockTransport(provider)),
    )


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def api(config: AppConfig, provider: Provider) -> Iterator[TestClient]:
    with TestClient(_app(config, provider), base_url=LOCAL) as client:
        yield client


def _container(client: TestClient) -> Container:
    container: Container = client.app.state.container  # type: ignore[attr-defined]
    return container


def _save(api: TestClient, client_id: str = CLIENT_ID, secret: str = SECRET) -> dict[str, object]:
    response = api.put(
        f"{MARKETPLACE}/credentials",
        json={"client_id": client_id, "client_secret": secret},
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    assert secret not in response.text
    return dict(response.json())


def _freshness(api: TestClient, value: str = "CURRENT") -> httpx.Response:
    return api.post(
        f"{MARKETPLACE}/contract-freshness", json={"contract_freshness": value}, headers=CLIENT
    )


def _observe(api: TestClient) -> httpx.Response:
    return api.post(f"{MARKETPLACE}/connect", headers=CLIENT)


def _bind(api: TestClient, uid: str) -> httpx.Response:
    return api.post(f"{MARKETPLACE}/bind", json={"confirmed_account_uid": uid}, headers=CLIENT)


def _resolve(api: TestClient, scope: str, resolution: str) -> httpx.Response:
    return api.post(
        f"{MARKETPLACE}/workflow-resolution",
        json={"workflow_scope": scope, "resolution": resolution},
        headers=CLIENT,
    )


def _capability(api: TestClient) -> dict[str, object]:
    return dict(api.get(f"{MARKETPLACE}/capability").json())


def _binding(config: AppConfig) -> str | None:
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        row = raw.execute(
            "SELECT provider_account_uid FROM marketplace_connections WHERE marketplace_key = ?",
            (KEY,),
        ).fetchone()
    return row[0] if row else None


def _audit_text(config: AppConfig) -> str:
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        return str(raw.execute("SELECT * FROM audit_events").fetchall())


def _overlays(view: dict[str, object]) -> list[tuple[object, object, object, object]]:
    return [
        (o["workflow_state"], o["workflow_scope"], o["reason_code"], o["resolution"])  # type: ignore[index]
        for o in view["workflow"]  # type: ignore[attr-defined]
    ]


# ---------------------------------------------------------------- credential safety


def test_credentials_are_replaced_as_a_new_generation_and_the_secret_never_returns(
    api: TestClient,
) -> None:
    status = api.get(f"{MARKETPLACE}/credentials").json()
    assert status == {"configured": False, "credential_generation": None}
    assert _save(api) == {"configured": True, "credential_generation": 1}
    assert _save(api, OTHER_CLIENT_ID, OTHER_SECRET) == {
        "configured": True,
        "credential_generation": 2,
    }
    status = api.get(f"{MARKETPLACE}/credentials")
    assert status.json() == {"configured": True, "credential_generation": 2}
    for secret in (SECRET, OTHER_SECRET):
        assert secret not in status.text


@pytest.mark.parametrize(
    "body",
    [
        {"client_secret": "$2a$04$typed-secret-never-echoed"},
        {"client_id": "", "client_secret": "$2a$04$typed-secret-never-echoed"},
        {
            "client_id": CLIENT_ID,
            "client_secret": "$2a$04$typed-secret-never-echoed",
            "store_id": "store-1",
        },
        {"client_id": CLIENT_ID, "client_secret": "typed-secret-never-echoed-not-a-salt"},
        {"client_id": CLIENT_ID, "client_secret": 12345678},
    ],
    ids=["missing-client-id", "empty-client-id", "extra-field", "unusable-secret", "wrong-type"],
)
def test_a_rejected_credential_save_never_echoes_the_secret(
    api: TestClient, config: AppConfig, body: dict[str, object]
) -> None:
    response = api.put(f"{MARKETPLACE}/credentials", json=body, headers=CLIENT)
    assert response.status_code == 422, response.text
    assert "typed-secret-never-echoed" not in response.text
    assert "12345678" not in response.text
    assert api.get(f"{MARKETPLACE}/credentials").json()["configured"] is False
    assert "typed-secret-never-echoed" not in _audit_text(config)


def test_the_secret_never_reaches_logs_audit_or_capability(
    api: TestClient, config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _save(api)
    _freshness(api)
    observed = _observe(api)
    bound = _bind(api, UID_A)
    capability = api.get(f"{MARKETPLACE}/capability")
    logs = caplog.text + "\n".join(
        str(value) for record in caplog.records for value in vars(record).values()
    )
    for text in (observed.text, bound.text, capability.text, _audit_text(config), logs):
        assert SECRET not in text
        assert CLIENT_ID not in text


def test_credential_replacement_ends_the_prior_session_and_proof(
    api: TestClient, provider: Provider
) -> None:
    _save(api)
    _freshness(api)
    _observe(api)
    assert _bind(api, UID_A).json()["capability"]["auth"] == "READY"
    _save(api, OTHER_CLIENT_ID, OTHER_SECRET)
    assert _capability(api)["auth"] == "NOT_READY"  # the old proof ended with its generation
    observed = _observe(api).json()
    assert provider.client_ids[-1] == OTHER_CLIENT_ID  # a new token under the new bundle
    assert (observed["bound"], observed["capability"]["auth"]) == (True, "READY")


# ---------------------------------------------------------------- observation vs binding


def test_account_observation_shows_the_account_and_never_binds(
    api: TestClient, config: AppConfig, provider: Provider
) -> None:
    _save(api)
    _freshness(api)
    response = _observe(api)
    assert response.status_code == 200, response.text
    view = response.json()
    assert (view["bound"], view["observed_account_uid"], view["observed_account_id"]) == (
        False,
        UID_A,
        f"id-{UID_A}",
    )
    assert view["capability"]["auth"] == "NOT_BOUND"
    assert _binding(config) is None
    assert provider.calls == ["TOKEN", "ACCOUNT"]
    # The observed identity is shown to the operator's screen, never audited.
    assert UID_A not in _audit_text(config)


def test_first_binding_happens_only_through_the_explicit_bind_action(
    api: TestClient, config: AppConfig, provider: Provider
) -> None:
    _save(api)
    _freshness(api)
    _observe(api)
    _observe(api)
    assert _binding(config) is None  # observing again still binds nothing
    reads = provider.calls.count("ACCOUNT")
    response = _bind(api, UID_A)
    assert response.status_code == 200, response.text
    assert provider.calls.count("ACCOUNT") == reads + 1  # the bind reads the account afresh
    view = response.json()
    assert (view["bound"], view["capability"]["auth"]) == (True, "READY")
    assert _binding(config) == UID_A


def test_a_changed_account_between_observation_and_bind_binds_nothing(
    api: TestClient, config: AppConfig, provider: Provider
) -> None:
    _save(api)
    _freshness(api)
    assert _observe(api).json()["observed_account_uid"] == UID_A
    provider.account_uid = UID_B
    response = _bind(api, UID_A)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SMARTSTORE_BINDING_NOT_CONFIRMED"
    assert _binding(config) is None
    assert _capability(api)["auth"] == "NOT_BOUND"


@pytest.mark.parametrize(
    "body",
    [
        {"confirmed_account_uid": UID_A, "force": True},
        {"confirmed_account_uid": UID_A, "rebind": True},
        {"confirmed_account_uid": UID_A, "skip_check": True},
        {"account_uid": UID_A},
        {},
    ],
    ids=["force", "rebind", "skip-check", "unconfirmed-field", "empty"],
)
def test_bind_accepts_no_override_or_unconfirmed_identity(
    api: TestClient, config: AppConfig, provider: Provider, body: dict[str, object]
) -> None:
    _save(api)
    _freshness(api)
    response = api.post(f"{MARKETPLACE}/bind", json=body, headers=CLIENT)
    assert response.status_code == 422
    assert provider.calls == []
    assert _binding(config) is None


def test_a_non_current_contract_blocks_the_first_binding(
    api: TestClient, config: AppConfig, provider: Provider
) -> None:
    _save(api)
    response = _bind(api, UID_A)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "MARKETPLACE_CONTRACT_NOT_CURRENT"
    assert provider.calls == []  # refused before any provider call
    assert _binding(config) is None


def test_a_failure_before_the_commit_leaves_no_binding(
    config: AppConfig, provider: Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    append = AuditLog.append

    def failing(self: AuditLog, entry: AuditEntry, **kwargs: object) -> object:
        if entry.event_type is AuditEventType.MARKETPLACE_ACCOUNT_BOUND:
            raise RuntimeError("failed while committing the binding")
        return append(self, entry, **kwargs)  # type: ignore[arg-type]

    with TestClient(_app(config, provider), base_url=LOCAL, raise_server_exceptions=False) as api:
        _save(api)
        _freshness(api)
        _observe(api)
        monkeypatch.setattr(AuditLog, "append", failing)
        response = _bind(api, UID_A)
        monkeypatch.undo()
        assert response.status_code == 500
        assert _binding(config) is None
        view = _capability(api)
        assert (view["auth"], view["auth_verified_at"]) == ("NOT_BOUND", None)
        assert _bind(api, UID_A).status_code == 200  # a retry binds normally
    assert _binding(config) == UID_A


def test_a_bound_account_is_never_rebound(
    api: TestClient, config: AppConfig, provider: Provider
) -> None:
    _save(api)
    _freshness(api)
    _bind(api, UID_A)
    provider.account_uid = UID_B
    observed = _observe(api).json()
    assert (observed["bound"], observed["observed_account_uid"]) == (True, UID_B)
    assert observed["capability"]["auth"] == "AUTH_MISMATCH"
    response = _bind(api, UID_B)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SMARTSTORE_ACCOUNT_ALREADY_BOUND"
    assert _binding(config) == UID_A


# ---------------------------------------------------------------- capability truth


def test_auth_mismatch_review_lifts_only_to_non_ready(
    api: TestClient, config: AppConfig, provider: Provider
) -> None:
    _save(api)
    _freshness(api)
    _bind(api, UID_A)
    provider.account_uid = UID_B
    view = _observe(api).json()["capability"]
    assert _overlays(view) == [("REVIEW_REQUIRED", "AUTHENTICATION", None, "REVIEW_RESOLVED")]
    resolved = _resolve(api, "AUTHENTICATION", "REVIEW_RESOLVED")
    assert resolved.status_code == 200, resolved.text
    assert (resolved.json()["auth"], resolved.json()["workflow"]) == ("NOT_READY", [])
    assert _binding(config) == UID_A  # never rebound
    provider.account_uid = UID_A
    assert _observe(api).json()["capability"]["auth"] == "READY"  # only fresh matching proof


def test_scope_insufficient_has_no_generic_resolution(api: TestClient) -> None:
    _save(api)
    _freshness(api)
    api.post(f"{MARKETPLACE}/permission-attestation", json={"observed_groups": []}, headers=CLIENT)
    view = _capability(api)
    assert _overlays(view) == [("PAUSED", "PRODUCT_REGISTRATION", "SCOPE_INSUFFICIENT", None)]
    for resolution in ("RESUME", "REVIEW_RESOLVED", "PROVIDER_REAUTH_COMPLETED"):
        response = _resolve(api, "PRODUCT_REGISTRATION", resolution)
        assert response.status_code == 422, resolution
    assert _overlays(_capability(api)) == _overlays(view)


@pytest.mark.parametrize(
    ("finding", "error_class", "overlay", "resolution"),
    [
        (Finding.UNRESOLVED, ErrorClass.UNKNOWN, ("REVIEW_REQUIRED", None), "REVIEW_RESOLVED"),
        (
            Finding.AUTH_RECOVERY_EXHAUSTED,
            ErrorClass.AUTH,
            ("PAUSED", "AUTH_RETRY_LIMIT"),
            "RESUME",
        ),
        (
            Finding.ACCOUNT_RESTRICTION_PROVEN,
            ErrorClass.POLICY_BLOCKED,
            ("PAUSED", "ACCOUNT_RESTRICTED"),
            "RESUME",
        ),
    ],
    ids=["review", "auth-retry-limit", "account-restricted"],
)
def test_each_overlay_names_only_its_typed_resolution_and_none_makes_ready(
    api: TestClient,
    finding: Finding,
    error_class: ErrorClass,
    overlay: tuple[str, str | None],
    resolution: str,
) -> None:
    failure = FailureEvidence(WorkflowScope.AUTHENTICATION, error_class, finding)
    _container(api).marketplace_capability.observe_failure(KEY, failure)
    view = _capability(api)
    assert _overlays(view) == [(overlay[0], "AUTHENTICATION", overlay[1], resolution)]
    for wrong in {"RESUME", "REVIEW_RESOLVED", "PROVIDER_REAUTH_COMPLETED"} - {resolution}:
        assert _resolve(api, "AUTHENTICATION", wrong).status_code == 422, wrong
    lifted = _resolve(api, "AUTHENTICATION", resolution).json()
    assert lifted["workflow"] == []
    assert lifted["auth"] != "READY"  # a resolution never fabricates READY


def test_an_application_reauth_pause_is_lifted_only_by_provider_reauth() -> None:
    # The pause itself cannot arise in M2 (detection disabled); its resolution is still fixed.
    pause = WorkflowOverlay(
        WorkflowState.PAUSED,
        WorkflowScope.AUTHENTICATION,
        PauseReason.APPLICATION_REAUTH_REQUIRED,
        session_generation=3,
    )
    assert resolution_for(pause) is Resolution.PROVIDER_REAUTH_COMPLETED


# ---------------------------------------------------------------- freshness recording


def test_freshness_recording_is_typed_audited_and_closed(
    api: TestClient, config: AppConfig
) -> None:
    first = _freshness(api).json()
    second = _freshness(api).json()  # a same-value recording is a real event
    assert first["contract_freshness"] == second["contract_freshness"] == "CURRENT"
    assert second["contract_freshness_recorded_at"] >= first["contract_freshness_recorded_at"]
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        events = raw.execute(
            "SELECT actor FROM audit_events WHERE action = 'RECORD_CONTRACT_FRESHNESS'"
        ).fetchall()
    assert events == [("operator:local",), ("operator:local",)]
    for value in ("UNRECORDED", "VERIFIED_BY_NAVER"):
        assert _freshness(api, value).status_code == 422, value
    assert _capability(api)["contract_freshness"] == "CURRENT"


# ---------------------------------------------------------------- web and network boundary


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/credentials", {"client_id": CLIENT_ID, "client_secret": SECRET}),
        ("POST", "/connect", None),
        ("POST", "/bind", {"confirmed_account_uid": UID_A}),
        ("POST", "/contract-freshness", {"contract_freshness": "CURRENT"}),
        (
            "POST",
            "/workflow-resolution",
            {"workflow_scope": "AUTHENTICATION", "resolution": "REVIEW_RESOLVED"},
        ),
    ],
)
def test_every_operator_action_requires_the_client_header(
    api: TestClient,
    config: AppConfig,
    provider: Provider,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    response = api.request(method, f"{MARKETPLACE}{path}", json=body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CLIENT_HEADER_REQUIRED"
    assert provider.calls == []
    assert api.get(f"{MARKETPLACE}/credentials").json()["configured"] is False
    assert _capability(api)["contract_freshness"] == "UNRECORDED"
    assert _binding(config) is None


def test_the_operator_flow_makes_no_external_attempt(api: TestClient, provider: Provider) -> None:
    before = EGRESS.snapshot()
    _save(api)
    _freshness(api)
    _observe(api)
    _bind(api, UID_A)
    _observe(api)
    after = EGRESS.snapshot()
    assert (after["external_attempts"], after["granted_events"]) == (
        before["external_attempts"],
        before["granted_events"],
    )
    assert set(provider.calls) <= {"TOKEN", "ACCOUNT"}  # only the fake provider answered
