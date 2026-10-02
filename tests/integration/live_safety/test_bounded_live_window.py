"""ROADMAP §14 item 5: the bounded LIVE runtime transition of ADR-0018 §2.

`DRY_RUN` is the boot default and the state every process starts in. `LIVE` is one bounded,
audited window held in the process's memory: it names the user's GitHub approval and a duration,
lapses by itself, is never widened while open, and a restart never restores it. The mode alone is
never authority for a mutation: in `LIVE` the brake, the grant and the rest of the send-time stack
still decide. No provider is reached here.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.live_safety import model as live_model
from app.capabilities.live_safety.model import GrantState, MutationRefused
from app.config import AppConfig, ConfigError
from app.container import Container, build_container
from app.platform.core.errors import PolicyBlockedError
from app.platform.core.execution import ExecutionMode
from app.platform.core.ownership import acquire_data_dir
from app.platform.core.secrets import MemorySecretStore
from app.platform.system import execution_mode as em
from tests.conftest import make_config
from tests.integration.live_safety.test_g3a_live_authority import (  # noqa: F401 - fixture
    DERIVED_A,
    count,
    grant,
    release,
    request,
    smartstore,
)
from tests.support.jobs_support import TEST_JOBS, FakeClock

pytestmark = pytest.mark.integration

APPROVAL = "github_issue_comment:5953614791@" + "a" * 64
CLIENT = {"X-ICBM-Client": "pytest"}


def _events(container: Container) -> list[Any]:
    # Oldest first: the audit log lists newest first.
    events = [e for e in container.audit.list_events(limit=500) if e.action == em.ACTION]
    return events[::-1]


def _open(container: Container, window_s: int = 600) -> None:
    container.execution_mode.request_change(
        ExecutionMode.LIVE,
        actor="operator",
        reason="bounded canary",
        approval_reference=APPROVAL,
        window_s=window_s,
    )


def _denied(container: Container, code: str, **kwargs: Any) -> None:
    with pytest.raises(PolicyBlockedError) as caught:
        container.execution_mode.request_change(
            ExecutionMode.LIVE, actor="operator", reason="probe", **kwargs
        )
    assert caught.value.code == code


def test_a_process_boots_dry_run_and_an_unbounded_live_request_is_refused_and_audited(
    container: Container,
) -> None:
    state = container.execution_mode.state()
    assert (state.mode, state.live_writes_permitted, state.policy) == (
        ExecutionMode.DRY_RUN,
        False,
        em.M0_POLICY,
    )
    _denied(container, em.UNBOUNDED_LIVE_FORBIDDEN)
    (event,) = _events(container)
    assert (event.outcome, event.reason_code) == ("DENIED", em.UNBOUNDED_LIVE_FORBIDDEN)
    assert container.execution_mode.state().mode is ExecutionMode.DRY_RUN


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"approval_reference": "approved by the user", "window_s": 600}, em.APPROVAL_UNSUPPORTED),
        (
            {"approval_reference": "github_issue_comment:1@abc", "window_s": 600},
            em.APPROVAL_UNSUPPORTED,
        ),
        ({"approval_reference": None, "window_s": 600}, em.APPROVAL_UNSUPPORTED),
        ({"approval_reference": APPROVAL, "window_s": None}, em.WINDOW_OUT_OF_BOUNDS),
        ({"approval_reference": APPROVAL, "window_s": 0}, em.WINDOW_OUT_OF_BOUNDS),
        ({"approval_reference": APPROVAL, "window_s": -5}, em.WINDOW_OUT_OF_BOUNDS),
        ({"approval_reference": APPROVAL, "window_s": True}, em.WINDOW_OUT_OF_BOUNDS),
        (
            {"approval_reference": APPROVAL, "window_s": em.LIVE_WINDOW_MAX_S + 1},
            em.WINDOW_OUT_OF_BOUNDS,
        ),
    ],
)
def test_a_live_request_outside_the_bounds_opens_nothing(
    container: Container, kwargs: dict[str, Any], code: str
) -> None:
    _denied(container, code, **kwargs)
    (event,) = _events(container)
    assert (event.outcome, event.reason_code) == ("DENIED", code)
    assert container.execution_mode.state().mode is ExecutionMode.DRY_RUN


def test_a_bounded_request_opens_one_audited_live_window(
    container: Container, clock: FakeClock
) -> None:
    opened_at = clock.now()
    _open(container, window_s=em.LIVE_WINDOW_MAX_S)
    state = container.execution_mode.state()
    assert state.mode is ExecutionMode.LIVE and state.live_writes_permitted is True
    assert state.policy == em.LIVE_POLICY
    assert state.live_until == opened_at + timedelta(seconds=em.LIVE_WINDOW_MAX_S)
    assert state.approval_reference == APPROVAL
    (event,) = _events(container)
    assert (event.outcome, event.reason_code) == ("ALLOWED", em.WINDOW_OPENED)
    assert (event.before, event.after) == ({"mode": "DRY_RUN"}, {"mode": "LIVE"})
    assert event.details["approval_reference"] == APPROVAL
    assert event.actor == "operator"


def test_an_open_window_is_never_widened(container: Container, clock: FakeClock) -> None:
    _open(container, window_s=600)
    until = container.execution_mode.state().live_until
    clock.advance(300)
    _denied(container, em.WINDOW_ALREADY_OPEN, approval_reference=APPROVAL, window_s=600)
    assert container.execution_mode.state().live_until == until
    assert [e.reason_code for e in _events(container)] == [
        em.WINDOW_OPENED,
        em.WINDOW_ALREADY_OPEN,
    ]


def test_a_window_lapses_to_dry_run_by_itself(container: Container, clock: FakeClock) -> None:
    _open(container, window_s=600)
    clock.advance(599)
    assert container.execution_mode.state().mode is ExecutionMode.LIVE
    clock.advance(1)
    state = container.execution_mode.state()
    assert (state.mode, state.live_writes_permitted, state.live_until) == (
        ExecutionMode.DRY_RUN,
        False,
        None,
    )
    # A lapsed window is gone: a new bounded request may open the next one.
    _open(container, window_s=60)
    assert container.execution_mode.state().mode is ExecutionMode.LIVE


def test_closing_is_audited_and_closing_dry_run_records_nothing(container: Container) -> None:
    _open(container)
    container.execution_mode.request_change(
        ExecutionMode.DRY_RUN, actor="operator", reason="canary done"
    )
    assert container.execution_mode.state().mode is ExecutionMode.DRY_RUN
    container.execution_mode.request_change(ExecutionMode.DRY_RUN, actor="operator", reason=None)
    opened, closed = _events(container)
    assert (closed.outcome, closed.reason_code) == ("ALLOWED", em.WINDOW_CLOSED)
    assert (closed.before, closed.after) == ({"mode": "LIVE"}, {"mode": "DRY_RUN"})
    assert closed.details["opened_by_audit_event_id"] == opened.event_id


@contextmanager
def _process(config: AppConfig, clock: FakeClock) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=TEST_JOBS,
        )
        try:
            yield built
        finally:
            built.db.dispose()


def test_a_restart_returns_to_dry_run(config: AppConfig, clock: FakeClock) -> None:
    with _process(config, clock) as first:
        _open(first, window_s=em.LIVE_WINDOW_MAX_S)
        assert first.execution_mode.state().mode is ExecutionMode.LIVE
    with _process(config, clock) as second:
        assert second.execution_mode.state().mode is ExecutionMode.DRY_RUN
        # The audit trail of the earlier window survives; the window does not.
        assert [e.reason_code for e in _events(second)] == [em.WINDOW_OPENED]


def test_the_boot_default_can_never_be_live(container: Container, data_dir: Any) -> None:
    with pytest.raises(ConfigError):
        make_config(data_dir, execution_mode=ExecutionMode.LIVE)
    with pytest.raises(ValueError):
        em.ExecutionModeService(ExecutionMode.LIVE, container.audit)


def test_live_mode_by_itself_permits_no_mutation(
    container: Container,
    smartstore: str,  # noqa: F811
) -> None:
    grant_id = grant(container, smartstore, [DERIVED_A], market="smartstore")
    release(container)
    _open(container)
    with pytest.raises(MutationRefused) as caught:
        container.asset_uploads.upload(request(grant_id, DERIVED_A))
    reasons = {layer["reason"] for layer in caught.value.details["layers"]}
    # The execution-mode layer now allows; every other layer still refuses on its own.
    assert live_model.MODE_NOT_LIVE not in reasons
    assert {
        live_model.SENDER_NOT_WIRED,
        live_model.ELIGIBILITY_UNPROVEN,
        live_model.RESTORE_PROOF_ABSENT,
        live_model.RETENTION_UNPROVEN,
        live_model.VISUAL_UNRECORDED,
        live_model.RESIDUAL_RISK_UNACCEPTED,
    } <= reasons
    assert count(container.config, "asset_upload_attempts") == 0
    stored = container.live_authority.grant_record(grant_id)
    assert stored is not None and stored.budget_used == 0 and stored.state is GrantState.ACTIVE


def _mode_check(container: Container) -> Any:
    (check,) = [c for c in container.readiness.check().checks if c.name == "execution_mode"]
    return check


def test_readiness_and_the_canary_view_read_the_current_mode(container: Container) -> None:
    assert _mode_check(container).status.value == "PASS"
    _open(container)
    # A bounded LIVE window is a healthy state, never a readiness failure.
    check = _mode_check(container)
    assert check.status.value == "PASS" and check.detail.startswith("LIVE until ")
    # The REGISTER canary readiness reads the owner at every evaluation, never a boot snapshot.
    assert container.register._execution_mode() == "LIVE"
    container.execution_mode.request_change(ExecutionMode.DRY_RUN, actor="operator", reason=None)
    assert container.register._execution_mode() == "DRY_RUN"


def test_the_route_opens_and_closes_a_bounded_window(client: TestClient) -> None:
    opened = client.post(
        "/api/v1/system/execution-mode",
        json={
            "target_mode": "LIVE",
            "reason": "bounded canary",
            "approval_reference": APPROVAL,
            "window_s": 600,
        },
        headers=CLIENT,
    )
    assert opened.status_code == 200
    assert (opened.json()["mode"], opened.json()["policy"]) == ("LIVE", em.LIVE_POLICY)
    closed = client.post(
        "/api/v1/system/execution-mode", json={"target_mode": "DRY_RUN"}, headers=CLIENT
    )
    assert closed.status_code == 200 and closed.json()["mode"] == "DRY_RUN"
    codes = [
        event["reason_code"]
        for event in client.get("/api/v1/system/audit-events").json()
        if event["action"] == em.ACTION
    ]
    assert sorted(codes) == sorted([em.WINDOW_OPENED, em.WINDOW_CLOSED])
