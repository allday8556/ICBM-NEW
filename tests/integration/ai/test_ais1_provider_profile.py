"""ADR-0027 AIS-1: the CLIProxyAPI provider profile, its approvals and the fail-closed call.

The probe and the call are deterministic fakes here: a fake serving process over a temporary
sidecar configuration, and a fake completion. No sidecar runs and nothing leaves the test.
"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.capabilities.ai.execution import AIExecution
from app.capabilities.ai.profiles import (
    ApproveRequest,
    ConfigureRequest,
    CredentialRequest,
    DataTransferRequest,
    ProfiledProvider,
    ProfileStore,
    ProviderProfileService,
    routing_version,
)
from app.capabilities.ai.provider import TaskRequest
from app.capabilities.audit.models import AuditEventType
from app.config import AppConfig, database_path
from app.container import Container
from app.platform.core.errors import AppError
from app.platform.core.secrets import MemorySecretStore
from app.stages.connect.state import CapabilityStatus
from integrations.ai import cliproxyapi, sidecar

pytestmark = pytest.mark.integration

KEY = "icbm-dedicated-client-key-0001"
CONFIG = f"""host: "127.0.0.1"
port: 18317
api-keys:
  - "{KEY}"
remote-management:
  disable-auto-update-panel: true
"""


@dataclass
class FakeProbe:
    process: sidecar.ServingProcess | None

    def serving(self, port: int) -> sidecar.ServingProcess | None:
        return self.process


@dataclass
class FakeComplete:
    value: dict[str, Any] | None = field(default_factory=lambda: {"ok": True})
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def __call__(self, endpoint: str, key: str, model: str, text: str) -> cliproxyapi.SidecarAnswer:
        self.calls.append((endpoint, key, model))
        return cliproxyapi.SidecarAnswer(
            value=self.value,
            error_kind=None if self.value is not None else "VALIDATION",
            error_code=None if self.value is not None else "AI_OUTPUT_NOT_JSON",
            actual_model="gpt-5.6-sol-2026",
            sidecar_version="7.2.155",
            tokens_in=300,
            tokens_out=12,
            latency_ms=900,
        )


def _serving(
    tmp_path: Path, args: str = " -local-model", body: bytes = b"cli"
) -> sidecar.ServingProcess:
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    exe = tmp_path / "cli-proxy-api.exe"
    exe.write_bytes(body)
    return sidecar.ServingProcess(
        pid=7,
        path=str(exe),
        sha256=sidecar.file_sha256(str(exe)) or "",
        command_line=f'"{exe}" -config "{exe.parent / "config.yaml"}"{args}',
    )


@pytest.fixture
def world(container: Container, tmp_path: Path) -> dict[str, Any]:
    probe = FakeProbe(_serving(tmp_path))
    complete = FakeComplete()
    store = ProfileStore(container.db, container.clock, container.audit)
    secrets = MemorySecretStore()
    provider = ProfiledProvider(store, probe, container.clock, secrets, complete=complete)
    service = ProviderProfileService(store, provider, container.clock)
    return {
        "secrets": secrets,
        "probe": probe,
        "complete": complete,
        "provider": provider,
        "service": service,
        "tmp": tmp_path,
    }


def _configure(service: ProviderProfileService, expected: str | None = None, cap: int = 3) -> Any:
    return service.configure(
        ConfigureRequest(
            actor="owner",
            expected_current_revision=expected,
            endpoint="http://127.0.0.1:18317",
            requested_model="gpt-5.6-sol",
            billing_mode="SUBSCRIPTION",
            daily_call_cap=cap,
        ),
        cid="c-1",
    )


def _approve_all(service: ProviderProfileService, view: Any = None) -> Any:
    view = view or _configure(service)
    view = service.set_credential(
        CredentialRequest(actor="owner", expected_current_revision=view.current_revision, key=KEY),
        cid="c-k",
    )
    view = service.approve_executable(
        ApproveRequest(
            actor="owner",
            expected_current_revision=view.current_revision,
            observed=view.observed.sha256,
            observed_path=view.observed.path,
        ),
        cid="c-2",
    )
    view = service.approve_routing(
        ApproveRequest(
            actor="owner",
            expected_current_revision=view.current_revision,
            observed=view.observed.routing_fingerprint,
        ),
        cid="c-3",
    )
    return service.set_data_transfer(
        DataTransferRequest(
            actor="owner", expected_current_revision=view.current_revision, approved=True
        ),
        cid="c-4",
    )


def test_a_profile_is_ready_only_after_every_approval(world: dict[str, Any]) -> None:
    service: ProviderProfileService = world["service"]
    assert service.view().capability.detail == "no AI provider is configured"
    view = _configure(service)
    assert view.capability.status is CapabilityStatus.NOT_CONFIGURED
    assert view.capability.detail == "not approved: executable, routing, data_transfer, credential"
    assert world["provider"].requested_identity() is None
    assert view.runtime_state == "NOT_CONFIGURED"
    ready = _approve_all(service, view)
    assert (ready.capability.status, ready.runtime_state) == (CapabilityStatus.READY, "AVAILABLE")
    identity = world["provider"].requested_identity()
    assert identity is not None
    assert (identity.requested_provider, identity.requested_model) == ("cliproxyapi", "gpt-5.6-sol")
    assert identity.proxy_version == ready.observed.sha256
    assert identity.routing_config_version == routing_version(
        "http://127.0.0.1:18317", ready.observed.routing_fingerprint
    )
    assert [h["action"] for h in ready.history] == [
        "DATA_TRANSFER",
        "APPROVE_ROUTING",
        "APPROVE_EXECUTABLE",
        "SET_CREDENTIAL",
        "CONFIGURE",
    ]


def test_only_a_loopback_endpoint_and_what_is_served_now_can_be_approved(
    world: dict[str, Any],
) -> None:
    service: ProviderProfileService = world["service"]
    with pytest.raises(AppError) as remote:
        service.configure(
            ConfigureRequest(
                actor="owner",
                endpoint="http://example.com:8317",
                requested_model="gpt-5.6-sol",
                billing_mode="SUBSCRIPTION",
                daily_call_cap=3,
            ),
            cid="c",
        )
    assert remote.value.code == "AI_PROFILE_INVALID"
    view = _configure(service)
    with pytest.raises(AppError) as wrong:
        service.approve_executable(
            ApproveRequest(
                actor="owner", expected_current_revision=view.current_revision, observed="0" * 64
            ),
            cid="c",
        )
    assert wrong.value.code == "AI_EXECUTABLE_MISMATCH"
    # The same bytes at another path than the one shown are not what was approved.
    with pytest.raises(AppError) as elsewhere:
        service.approve_executable(
            ApproveRequest(
                actor="owner",
                expected_current_revision=view.current_revision,
                observed=view.observed.sha256,
                observed_path="C:\\other\\cli-proxy-api.exe",
            ),
            cid="c",
        )
    assert elsewhere.value.code == "AI_EXECUTABLE_MISMATCH"
    # A sidecar that still fetches its model catalog is never approved (ADR-0027 §5).
    world["probe"].process = _serving(world["tmp"], args="")
    plain = service.view()
    with pytest.raises(AppError) as updates:
        service.approve_routing(
            ApproveRequest(
                actor="owner",
                expected_current_revision=plain.current_revision,
                observed=plain.observed.routing_fingerprint,
            ),
            cid="c",
        )
    assert updates.value.code == "AI_ROUTING_UPDATES_ON"
    # A save against a moved revision is refused.
    with pytest.raises(AppError) as moved:
        _configure(service, expected=None)
    assert moved.value.code == "AI_PROFILE_CURRENT_MOVED"


def test_a_call_proves_the_served_sidecar_first_and_records_what_answered(
    world: dict[str, Any], container: Container, config: AppConfig
) -> None:
    service: ProviderProfileService = world["service"]
    provider: ProfiledProvider = world["provider"]
    _approve_all(service)
    outcome = provider.execute(TaskRequest("TASK_X", "text", "c-9"))
    assert outcome.ok and outcome.value == {"ok": True}
    provenance = outcome.provenance
    assert (provenance.requested_model, provenance.actual_model, provenance.billing_mode.value) == (
        "gpt-5.6-sol",
        "gpt-5.6-sol-2026",
        "SUBSCRIPTION",
    )
    assert (provenance.vendor_cost, provenance.tokens_in, provenance.proxy_name) == (
        None,
        300,
        "CLIProxyAPI",
    )
    # The key came from the OS secret store and was handed to the call only.
    assert world["complete"].calls == [("http://127.0.0.1:18317", KEY, "gpt-5.6-sol")]
    # Through the execution owner the outcome is trusted: its identity is the requested one.
    execution = AIExecution(provider)
    from app.capabilities.ai.composer import ComposedRequest

    composed = ComposedRequest("TASK_X", "R", "P", "M", "text", {}, {})
    assert execution.run(composed, correlation_id="c-10").outcome.ok
    # The key is never stored or audited.
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        dump = "\n".join(
            str(row)
            for table in ("ai_provider_profile_revisions", "ai_provider_calls", "audit_events")
            for row in raw.execute(f"SELECT * FROM {table}")
        )
        approvals = [
            json.loads(r[0])
            for r in raw.execute(
                "SELECT details_json FROM audit_events WHERE event_type = ?",
                (AuditEventType.AI_PROVIDER_PROFILE_REVISED,),
            )
        ]
        calls = raw.execute("SELECT COUNT(*) FROM ai_provider_calls").fetchone()[0]
    assert KEY not in dump
    assert len(approvals) == 5 and calls == 2


def test_a_mismatch_unserved_sidecar_or_reached_cap_refuses_before_anything_is_sent(
    world: dict[str, Any],
) -> None:
    service: ProviderProfileService = world["service"]
    provider: ProfiledProvider = world["provider"]
    _approve_all(service)
    complete: FakeComplete = world["complete"]
    approved = world["probe"].process
    for process, code, state in (
        (
            _serving(world["tmp"], body=b"another binary"),
            "AI_EXECUTABLE_MISMATCH",
            "VERSION_MISMATCH",
        ),
        (None, "AI_EXECUTABLE_NOT_SERVING", "UNAVAILABLE"),
        (
            _serving(world["tmp"], args=" -local-model -debug"),
            "AI_ROUTING_MISMATCH",
            "ROUTING_CONFIG_MISMATCH",
        ),
        (
            _serving(world["tmp"], args=' -local-model -config "missing.yaml"'),
            "AI_ROUTING_UNREADABLE",
            "UNAVAILABLE",
        ),
    ):
        world["probe"].process = process
        with pytest.raises(AppError) as refused:
            provider.execute(TaskRequest("TASK_X", "t", "c"))
        assert (refused.value.code, refused.value.error_class.value) == (code, "POLICY_BLOCKED")
        assert provider.capability_report().detail == code
        assert provider.runtime_state() == state
    assert complete.calls == []
    world["probe"].process = approved
    for _ in range(3):
        provider.execute(TaskRequest("TASK_X", "t", "c"))
    assert provider.capability_report().detail == "AI_DAILY_CAP_REACHED"
    assert provider.runtime_state() == "AVAILABLE"
    with pytest.raises(AppError) as capped:
        provider.execute(TaskRequest("TASK_X", "t", "c"))
    assert capped.value.code == "AI_DAILY_CAP_REACHED"
    assert len(complete.calls) == 3


def test_without_the_data_transfer_approval_nothing_is_ready(world: dict[str, Any]) -> None:
    service: ProviderProfileService = world["service"]
    ready = _approve_all(service)
    revoked = service.set_data_transfer(
        DataTransferRequest(
            actor="owner", expected_current_revision=ready.current_revision, approved=False
        ),
        cid="c",
    )
    assert revoked.capability.detail == "not approved: data_transfer"
    assert world["provider"].requested_identity() is None
    with pytest.raises(AppError) as refused:
        world["provider"].execute(TaskRequest("TASK_X", "t", "c"))
    assert refused.value.code == "AI_PROVIDER_NOT_CONFIGURED"


def test_the_routes_configure_and_read_the_profile(config: AppConfig) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import LOCAL

    headers = {"X-ICBM-Client": "pytest"}
    with TestClient(create_app(config), base_url=LOCAL) as client:
        empty = client.get("/api/v1/ai/provider", headers=headers).json()
        assert (empty["content"], empty["capability"]["status"]) == (None, "NOT_CONFIGURED")
        saved = client.post(
            "/api/v1/ai/provider/profile",
            headers=headers,
            json={
                "actor": "owner",
                "endpoint": "http://127.0.0.1:45999",
                "requested_model": "gpt-5.6-sol",
                "billing_mode": "SUBSCRIPTION",
                "daily_call_cap": 50,
            },
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["content"]["requested_model"] == "gpt-5.6-sol"
        assert body["content"]["credential_ref"] == "ai.cliproxyapi.client-key"
        assert body["content"]["credential_set_at"] is None
        # The key is written to the secret store and never returned.
        keyed = client.post(
            "/api/v1/ai/provider/credential",
            headers=headers,
            json={
                "actor": "owner",
                "expected_current_revision": body["current_revision"],
                "key": KEY,
            },
        )
        assert keyed.status_code == 200, keyed.text
        assert KEY not in keyed.text
        body = keyed.json()
        bad = client.post(
            "/api/v1/ai/provider/credential",
            headers=headers,
            json={
                "actor": "owner",
                "expected_current_revision": body["current_revision"],
                "key": "short key",
            },
        )
        assert bad.json()["error"]["code"] == "AI_CREDENTIAL_INVALID"
        assert "short key" not in bad.text
        # Nothing serves that port: nothing can be approved, and nothing is ready.
        assert body["observed"]["serving"] is False
        assert body["capability"]["status"] == "NOT_CONFIGURED"


def test_an_unreadable_profile_store_degrades_the_capability_and_never_raises(
    world: dict[str, Any], config: AppConfig
) -> None:
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        raw.execute("ALTER TABLE ai_provider_profile_current RENAME TO moved_away")
    report = world["provider"].capability_report()
    assert (report.status, report.detail) == (CapabilityStatus.DEGRADED, "AI_PROFILE_UNREADABLE")


def test_concurrent_calls_never_pass_the_cap_together(
    world: dict[str, Any], config: AppConfig
) -> None:
    """GPT audit of #273: the place under the cap is taken before the call, in one serialized write
    unit, so concurrent calls cannot both take the last place."""
    _approve_all(world["service"])
    provider: ProfiledProvider = world["provider"]
    gate = threading.Barrier(6)
    outcomes: list[str] = []

    def slow(endpoint: str, key: str, model: str, text: str) -> cliproxyapi.SidecarAnswer:
        time.sleep(0.2)
        return world["complete"](endpoint, key, model, text)

    provider._complete = slow

    def one() -> None:
        gate.wait()
        try:
            provider.execute(TaskRequest("TASK_X", "t", "c"))
            outcomes.append("sent")
        except AppError as refused:
            outcomes.append(refused.code)

    threads = [threading.Thread(target=one) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["AI_DAILY_CAP_REACHED"] * 3 + ["sent"] * 3
    assert len(world["complete"].calls) == 3
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        assert raw.execute("SELECT outcome FROM ai_provider_calls").fetchall() == [("OK",)] * 3
        # A settled call never changes again.
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("UPDATE ai_provider_calls SET outcome = 'FAILED'")


def test_a_call_that_raises_is_settled_failed_and_still_counts(
    world: dict[str, Any], config: AppConfig
) -> None:
    _approve_all(world["service"])
    provider: ProfiledProvider = world["provider"]

    def broken(endpoint: str, key: str, model: str, text: str) -> cliproxyapi.SidecarAnswer:
        raise RuntimeError("adapter fault")

    provider._complete = broken
    with pytest.raises(RuntimeError):
        provider.execute(TaskRequest("TASK_X", "t", "c"))
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        assert raw.execute("SELECT outcome, error_code FROM ai_provider_calls").fetchall() == [
            ("FAILED", "AI_PROVIDER_CALL_FAILED")
        ]
    assert world["service"].view().calls_today == 1


def test_only_the_owners_model_is_asked_and_a_move_withdraws_the_transfer_approval(
    world: dict[str, Any],
) -> None:
    """GPT audit of #273: the model is the owner's (AIS-09), and the data-transfer approval names
    the endpoint and model it was given for."""
    service: ProviderProfileService = world["service"]
    with pytest.raises(AppError) as other:
        service.configure(
            ConfigureRequest(
                actor="owner",
                endpoint="http://127.0.0.1:18317",
                requested_model="another-model",
                billing_mode="SUBSCRIPTION",
                daily_call_cap=3,
            ),
            cid="c",
        )
    assert other.value.code == "AI_MODEL_NOT_APPROVED"
    ready = _approve_all(service)
    # A new cap keeps the approval: the endpoint and the model are unchanged.
    capped = service.configure(
        ConfigureRequest(
            actor="owner",
            expected_current_revision=ready.current_revision,
            endpoint="http://127.0.0.1:18317",
            requested_model="gpt-5.6-sol",
            billing_mode="SUBSCRIPTION",
            daily_call_cap=5,
        ),
        cid="c",
    )
    assert capped.capability.status is CapabilityStatus.READY
    moved = service.configure(
        ConfigureRequest(
            actor="owner",
            expected_current_revision=capped.current_revision,
            endpoint="http://127.0.0.1:18318",
            requested_model="gpt-5.6-sol",
            billing_mode="SUBSCRIPTION",
            daily_call_cap=5,
        ),
        cid="c",
    )
    assert moved.content["data_transfer_approved"] is False
    assert moved.capability.detail == "not approved: data_transfer"
    assert world["provider"].requested_identity() is None
    # The endpoint is part of the requested identity: a re-approved move is a new identity.
    again = service.set_data_transfer(
        DataTransferRequest(
            actor="owner", expected_current_revision=moved.current_revision, approved=True
        ),
        cid="c",
    )
    assert again.capability.detail.startswith("provider=")
    identity = world["provider"].requested_identity()
    assert identity is not None
    assert identity.routing_config_version == routing_version(
        "http://127.0.0.1:18318", again.content["approved_routing"]["fingerprint"]
    )
    assert identity.routing_config_version != routing_version(
        "http://127.0.0.1:18317", again.content["approved_routing"]["fingerprint"]
    )


def test_without_the_credential_nothing_is_ready_and_it_is_kept_only_in_the_secret_store(
    world: dict[str, Any], config: AppConfig
) -> None:
    service: ProviderProfileService = world["service"]
    ready = _approve_all(service)
    assert ready.capability.status is CapabilityStatus.READY
    assert world["secrets"].get("ai.cliproxyapi.client-key") == KEY
    world["secrets"].delete("ai.cliproxyapi.client-key")
    assert world["provider"].capability_report().detail == "not approved: credential"
    assert world["provider"].requested_identity() is None
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        dump = "\n".join(
            str(row)
            for table in ("ai_provider_profile_revisions", "audit_events")
            for row in raw.execute(f"SELECT * FROM {table}")
        )
    assert KEY not in dump


def test_a_revision_committed_after_the_call_read_the_profile_refuses_it(
    world: dict[str, Any], config: AppConfig
) -> None:
    """GPT audit of #273: the call's reservation proves the profile revision it read is still the
    current one, so a revocation in between is never overtaken by a send."""
    service: ProviderProfileService = world["service"]
    ready = _approve_all(service)
    provider: ProfiledProvider = world["provider"]
    store = provider._store
    real = store.reserve_call

    def revoked_meanwhile(*args: Any, **kwargs: Any) -> Any:
        service.set_data_transfer(
            DataTransferRequest(
                actor="owner", expected_current_revision=ready.current_revision, approved=False
            ),
            cid="c-revoke",
        )
        return real(*args, **kwargs)

    store.reserve_call = revoked_meanwhile  # type: ignore[method-assign]
    with pytest.raises(AppError) as moved:
        provider.execute(TaskRequest("TASK_X", "t", "c"))
    assert moved.value.code == "AI_PROFILE_CURRENT_MOVED"
    assert world["complete"].calls == []
    with sqlite3.connect(database_path(config.data_dir)) as raw:
        assert raw.execute("SELECT COUNT(*) FROM ai_provider_calls").fetchone()[0] == 0
