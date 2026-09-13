"""Supplier CONNECT over the real application: worker, audit, capability readiness (no network)."""

import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.main import create_app
from app.system.secret_scan import scan
from integrations.suppliers.base import RequestKind
from tests.conftest import LOCAL
from tests.suppliers import KMRETAIL_PAGES, PASSWORD, SESSION_TOKEN, USERNAME, FakeGateway

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
BASE = "/api/v1/connect/suppliers/kmretail"


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway(KMRETAIL_PAGES)


@pytest.fixture
def api(config: AppConfig, gateway: FakeGateway) -> Iterator[TestClient]:
    with TestClient(create_app(config, supplier_gateway=gateway), base_url=LOCAL) as client:
        yield client


def _wait_job(client: TestClient, job_id: str, *states: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/api/v1/system/jobs/{job_id}").json()
        if job["state"] in states:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {states}")


def _supplier(client: TestClient) -> dict[str, Any]:
    [supplier] = client.get("/api/v1/connect/suppliers").json()
    return dict(supplier)


def _save(client: TestClient, password: str = PASSWORD) -> dict[str, Any]:
    response = client.put(
        f"{BASE}/credentials", json={"username": USERNAME, "password": password}, headers=CLIENT
    )
    assert response.status_code == 200
    assert USERNAME not in response.text and password not in response.text
    return dict(response.json())


def _test(client: TestClient) -> dict[str, Any]:
    response = client.post(f"{BASE}/test", headers=CLIENT)
    assert response.status_code == 202, response.text
    return dict(response.json())


def test_an_unconfigured_supplier_degrades_a_capability_never_core_readiness(
    api: TestClient,
) -> None:
    response = api.get("/api/ready")
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["overall"]) == ("PASS", "READY")
    assert body["capabilities"] == [
        {"key": "supplier:kmretail", "status": "NOT_CONFIGURED", "detail": "state=DISCONNECTED"}
    ]
    assert body["degraded_capabilities"] == ["supplier:kmretail"]


def test_a_connection_test_proves_the_protected_read_and_makes_the_capability_ready(
    api: TestClient, gateway: FakeGateway, config: AppConfig
) -> None:
    saved = _save(api)
    assert (saved["credentials_stored"], saved["capability_status"]) == (True, "DISCONNECTED")
    job = _wait_job(api, _test(api)["job_id"], "SUCCEEDED", "DEAD")
    assert job["state"] == "SUCCEEDED", job
    supplier = _supplier(api)
    assert (supplier["state"], supplier["auth_state"], supplier["session_state"]) == (
        "READY",
        "AUTHENTICATED",
        "VERIFIED",
    )
    ready = api.get("/api/ready").json()
    assert ready["capabilities"][0]["status"] == "READY"
    assert ready["degraded_capabilities"] == []
    assert gateway.logins == 1
    assert gateway.requests == [RequestKind.CONTROL_READ, RequestKind.PROTECTED_READ]

    # The same target, both sides: the proof recorded in the audit trail.
    events = api.get(
        "/api/v1/system/audit-events", params={"correlation_id": job["correlation_id"]}
    )
    verified = [e for e in events.json() if e["event_type"] == "SUPPLIER_CONNECTION_VERIFIED"]
    assert verified[0]["details"] | {"signals": None} == verified[0]["details"] | {
        "target": "/myshop/index.html",
        "control_result": "LOGIN_REQUIRED",
        "authenticated_result": "AUTHENTICATED",
        "signals": None,
    }
    assert {"state_logoff", "login_check_redirect", "state_logon", "logout_action"} <= set(
        verified[0]["details"]["signals"]
    )

    # A second test reuses the session: no login.
    _wait_job(api, _test(api)["job_id"], "SUCCEEDED")
    assert gateway.logins == 1
    assert _supplier(api)["session_reuse_count"] == 1

    report = scan(
        [config.data_dir],
        {"password": PASSWORD, "username": USERNAME, "session_token": SESSION_TOKEN},
    )
    assert report["total_hits"] == 0, report["hits"]


def test_validation_errors_never_echo_what_was_typed(api: TestClient) -> None:
    response = api.put(f"{BASE}/credentials", json={"username": USERNAME}, headers=CLIENT)
    assert response.status_code == 422
    assert USERNAME not in response.text
    wrong_type = api.put(
        f"{BASE}/credentials",
        json={"username": USERNAME, "password": ["x", PASSWORD]},
        headers=CLIENT,
    )
    assert wrong_type.status_code == 422
    assert PASSWORD not in wrong_type.text and USERNAME not in wrong_type.text


def test_repeated_rejections_pause_until_the_operator_resumes(
    api: TestClient, gateway: FakeGateway
) -> None:
    _save(api, password="wrong-password")
    for attempt in range(1, 4):
        job = _wait_job(api, _test(api)["job_id"], "DEAD", "SUCCEEDED")
        assert (job["state"], job["last_error_class"], job["attempt_count"]) == ("DEAD", "AUTH", 1)
        assert _supplier(api)["consecutive_auth_failures"] == attempt
    supplier = _supplier(api)
    assert (supplier["state"], supplier["capability_status"]) == ("PAUSED", "PAUSED")
    assert api.get("/api/ready").json()["degraded_capabilities"] == ["supplier:kmretail"]

    refused = api.post(f"{BASE}/test", headers=CLIENT)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "SUPPLIER_AUTH_PAUSED"
    assert gateway.logins == 3

    _save(api)
    resumed = api.post(f"{BASE}/resume", headers=CLIENT)
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "DISCONNECTED"
    _wait_job(api, _test(api)["job_id"], "SUCCEEDED")
    assert _supplier(api)["state"] == "READY"
    assert gateway.logins == 4


def test_a_test_needs_stored_credentials(api: TestClient, gateway: FakeGateway) -> None:
    response = api.post(f"{BASE}/test", headers=CLIENT)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SUPPLIER_CREDENTIALS_MISSING"
    assert gateway.requests == []


def test_unknown_suppliers_are_not_found(api: TestClient) -> None:
    response = api.post("/api/v1/connect/suppliers/nobody/test", headers=CLIENT)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SUPPLIER_UNKNOWN"


def test_reopening_the_login_form_shows_the_saved_id_and_never_the_password(
    api: TestClient, config: AppConfig
) -> None:
    # Issue #7 addendum 5654634584: save → close → reopen shows the same ID and a stored state.
    _save(api)
    reopened = api.get(f"{BASE}/credentials")
    assert reopened.status_code == 200
    assert reopened.json() == {"username": USERNAME, "password_stored": True}
    assert PASSWORD not in reopened.text
    assert reopened.headers["cache-control"] == "no-store"
    report = scan([config.data_dir], {"username": USERNAME, "password": PASSWORD})
    assert report["total_hits"] == 0, "the ID is served on demand, never persisted"


def test_saving_without_a_new_password_keeps_the_stored_one(
    api: TestClient, gateway: FakeGateway
) -> None:
    _save(api)
    kept = api.put(f"{BASE}/credentials", json={"username": USERNAME}, headers=CLIENT)
    assert kept.status_code == 200
    assert _wait_job(api, _test(api)["job_id"], "SUCCEEDED", "DEAD")["state"] == "SUCCEEDED"
    assert gateway.logins == 1, "the real stored password still authenticates"


def test_the_masked_state_is_rejected_as_a_password(api: TestClient, gateway: FakeGateway) -> None:
    _save(api)
    masked = api.put(
        f"{BASE}/credentials",
        json={"username": USERNAME, "password": "•••••••• · 저장됨"},
        headers=CLIENT,
    )
    assert masked.status_code == 422
    assert masked.json()["error"]["code"] == "SUPPLIER_PASSWORD_MASK_REJECTED"
    assert USERNAME not in masked.text
    assert _wait_job(api, _test(api)["job_id"], "SUCCEEDED", "DEAD")["state"] == "SUCCEEDED"


def test_an_unconfigured_supplier_reports_no_stored_login(api: TestClient) -> None:
    assert api.get(f"{BASE}/credentials").json() == {"username": None, "password_stored": False}
    first = api.put(f"{BASE}/credentials", json={"username": USERNAME}, headers=CLIENT)
    assert first.status_code == 422
    assert first.json()["error"]["code"] == "SUPPLIER_PASSWORD_REQUIRED"


def test_auto_connect_can_be_switched_off_by_the_operator(api: TestClient) -> None:
    _save(api)
    response = api.put(f"{BASE}/auto-connect", json={"enabled": False}, headers=CLIENT)
    assert response.status_code == 200
    assert response.json()["auto_connect"] is False
    events = api.get("/api/v1/system/audit-events").json()
    assert events[0]["event_type"] == "SUPPLIER_AUTO_CONNECT_CHANGED"
    assert events[0]["details"]["auto_connect"] is False
