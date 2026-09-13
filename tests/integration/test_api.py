"""The real application over HTTP: real worker, system clock, migrated database."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.logging import LOG_FILE_NAME
from app.main import create_app
from tests.conftest import LOCAL, make_config
from tests.support import TEST_JOBS

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
SCREENS = {
    "dashboard": "NO_CONNECTIONS",
    "collect": "NO_COLLECTION_JOBS",
    "db": "NO_PRODUCTS",
    "register": "NO_REGISTRATION_CANDIDATES",
    "orders": "NO_ORDERS",
    "inquiry": "NO_INQUIRIES",
    "soldout": "NO_STOCK_REVIEW_ITEMS",
    "ai-insight": "NO_INTERNAL_HISTORY",
    "analytics": "NO_OPERATING_DATA",
    "settings": "NO_SETTINGS_SAVED",
}


def _wait_for(predicate, timeout_s: float = 10.0):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_health_and_readiness_pass(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    first = client.get("/api/ready")
    second = client.get("/api/ready")
    assert first.status_code == second.status_code == 200
    checks = {c["name"]: c["status"] for c in first.json()["checks"]}
    assert checks == {
        "data_dir_owner": "PASS",
        "database": "PASS",
        "sqlite_wal": "PASS",
        "schema": "PASS",
        "job_worker": "PASS",
        "secret_store": "PASS",
        "execution_mode": "PASS",
        "egress_guard": "PASS",
    }
    assert [c["name"] for c in second.json()["checks"]] == list(checks)


@pytest.mark.parametrize(("screen", "reason"), SCREENS.items())
def test_every_screen_contract_reports_empty(client: TestClient, screen: str, reason: str) -> None:
    response = client.get(f"/api/v1/screens/{screen}")
    assert response.status_code == 200
    meta = response.json()["meta"]
    assert meta == meta | {"screen": screen, "state": "EMPTY", "empty_reason": reason}
    assert meta["milestone"] == "M0"


def test_settings_contract_has_no_connections_and_is_read_only(client: TestClient) -> None:
    body = client.get("/api/v1/screens/settings").json()
    assert body["execution_mode"] == "DRY_RUN"
    assert body["editable"] is False
    assert body["supplier_connections"] == []
    assert body["policy_values"] == {}
    assert {c["connection_state"] for c in body["marketplace_connections"]} == {"NOT_CONNECTED"}


def test_shell_contract_serves_marketplace_identity_assets(client: TestClient) -> None:
    shell = client.get("/api/v1/shell").json()
    assert shell["milestone"] == "M0"
    assert shell["execution_mode"] == "DRY_RUN"
    keys = [m["key"] for m in shell["marketplaces"]]
    assert keys == ["smartstore", "coupang", "st11", "gmarket", "auction"]
    for marketplace in shell["marketplaces"]:
        logo = client.get(marketplace["logo_url"])
        assert logo.status_code == 200
        assert logo.content.startswith(b"\x89PNG")


def test_ui_is_served_with_a_restrictive_csp(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-frame-options"] == "DENY"


def test_correlation_id_is_echoed_or_issued(client: TestClient) -> None:
    echoed = client.get("/api/health", headers={"X-Correlation-ID": "trace-api-000001"})
    assert echoed.headers["x-correlation-id"] == "trace-api-000001"
    issued = client.get("/api/health", headers={"X-Correlation-ID": "not valid!"})
    assert issued.headers["x-correlation-id"] != "not valid!"


def test_state_changing_requests_require_the_client_header(client: TestClient) -> None:
    response = client.post("/api/v1/system/execution-mode", json={"target_mode": "LIVE"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CLIENT_HEADER_REQUIRED"


def test_non_loopback_host_header_is_rejected(client: TestClient) -> None:
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 400


def test_errors_use_the_canonical_envelope(client: TestClient) -> None:
    missing = client.get("/api/v1/system/jobs/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["class"] == "NOT_FOUND"
    invalid = client.post(
        "/api/v1/system/execution-mode", json={"target_mode": "X"}, headers=CLIENT
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["class"] == "VALIDATION"


def test_live_request_is_denied_and_audited(client: TestClient) -> None:
    response = client.post(
        "/api/v1/system/execution-mode",
        json={"target_mode": "LIVE", "reason": "acceptance probe"},
        headers=CLIENT | {"X-Correlation-ID": "trace-protected-01"},
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert (error["class"], error["code"]) == ("POLICY_BLOCKED", "M0_LIVE_FORBIDDEN")
    assert error["correlation_id"] == "trace-protected-01"

    events = client.get(
        "/api/v1/system/audit-events", params={"correlation_id": "trace-protected-01"}
    ).json()
    assert len(events) == 1
    event = events[0]
    assert (event["event_type"], event["action"], event["outcome"]) == (
        "PROTECTED_ACTION",
        "EXECUTION_MODE_CHANGE",
        "DENIED",
    )
    assert event["details"]["requested_mode"] == "LIVE"
    assert event["event_id"] == error["details"]["audit_event_id"]
    assert client.get("/api/v1/system/execution-mode").json()["mode"] == "DRY_RUN"


def test_same_mode_request_is_a_no_op(client: TestClient) -> None:
    response = client.post(
        "/api/v1/system/execution-mode", json={"target_mode": "DRY_RUN"}, headers=CLIENT
    )
    assert response.status_code == 200
    assert client.get("/api/v1/system/audit-events").json() == []


def test_diagnostics_are_disabled_by_default(client: TestClient) -> None:
    response = client.post("/api/v1/diagnostics/failing-job", headers=CLIENT)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DIAGNOSTICS_DISABLED"


def test_failing_job_dead_letters_and_is_traceable(data_dir: Path) -> None:
    config = make_config(data_dir, diagnostics_enabled=True)
    app = create_app(config, extra_jobs=TEST_JOBS)
    trace = "trace-deadletter-01"
    with TestClient(app, base_url=LOCAL) as client:
        created = client.post(
            "/api/v1/diagnostics/failing-job", headers=CLIENT | {"X-Correlation-ID": trace}
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert created.json()["correlation_id"] == trace

        def dead() -> dict[str, object] | None:
            assert client.get("/api/health").status_code == 200  # API stays responsive
            job = client.get(f"/api/v1/system/jobs/{job_id}").json()
            return job if job["state"] == "DEAD" else None

        job = _wait_for(dead)
        assert job["attempt_count"] == config.job_max_attempts
        events = client.get("/api/v1/system/audit-events", params={"correlation_id": trace}).json()
        assert sorted(e["event_type"] for e in events) == [
            "DIAGNOSTIC_REQUEST",
            "JOB_DEAD_LETTERED",
        ]

    lines = [
        json.loads(line)
        for line in (data_dir / "logs" / LOG_FILE_NAME).read_text("utf-8").splitlines()
    ]
    traced = {line["msg"] for line in lines if line.get("correlation_id") == trace}
    assert {"job.enqueued", "job.attempt.started", "job.dead_lettered", "audit.appended"} <= traced


def test_unmigrated_database_is_not_ready(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path))
    with TestClient(app, base_url=LOCAL) as client:
        response = client.get("/api/ready")
        assert response.status_code == 503
        checks = {c["name"]: c["status"] for c in response.json()["checks"]}
        assert checks["schema"] == "FAIL"
        assert checks["job_worker"] == "FAIL"


def test_restart_preserves_queued_and_dead_letter_state(data_dir: Path) -> None:
    config = make_config(
        data_dir, diagnostics_enabled=True, job_backoff_base_s=0.5, job_backoff_max_s=1.0
    )
    with TestClient(create_app(config), base_url=LOCAL) as client:
        job_id = client.post("/api/v1/diagnostics/failing-job", headers=CLIENT).json()["job_id"]
        _wait_for(
            lambda: client.get(f"/api/v1/system/jobs/{job_id}").json()["state"] == "RETRY_SCHEDULED"
        )
    # Process stopped while the job waits for its retry.
    with TestClient(create_app(config), base_url=LOCAL) as client:
        pending = client.get(f"/api/v1/system/jobs/{job_id}").json()
        assert pending["state"] in {"RETRY_SCHEDULED", "RUNNING", "DEAD"}
        assert pending["attempt_count"] >= 1
        done = _wait_for(
            lambda: (
                (j := client.get(f"/api/v1/system/jobs/{job_id}").json())["state"] == "DEAD" and j
            )
        )
        assert done["attempt_count"] == config.job_max_attempts
        assert [a["attempt_no"] for a in done["attempts"]] == [1, 2, 3]
