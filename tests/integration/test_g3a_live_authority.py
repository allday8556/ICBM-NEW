"""Gate 3 area 1 (ADR-0018 §3, §3.4, §4, §10, §12): the pre-LIVE owners over a real database.

Provider-zero throughout: every ASSET sender here is :class:`tests.live_support.ScriptedSender`
and every CREATE sender a fake. The production wiring is exercised as it is — the M0 execution
policy, the unproven prerequisites and the unwired sender — and must refuse everything.
"""

import hashlib
import sqlite3
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.config import AppConfig
from app.container import Container
from app.core.errors import InputValidationError, PolicyBlockedError
from app.core.execution import ExecutionMode
from app.live import model as live_model
from app.live.assets import (
    AssetUploadRequest,
    AssetUploadService,
    TransmissionPrecluded,
    UploadSendResult,
)
from app.live.model import (
    BrakeState,
    GrantState,
    Layer,
    MutationRefused,
    MutationStage,
    UploadAttemptState,
    WireHostPolicy,
)
from app.live.stack import (
    REPLAY_DUPLICATE_IN_UNIT,
    REPLAY_UNRESOLVED_IN_UNIT,
    CandidateState,
    SafetyStack,
    Verdict,
)
from app.live.store import ArtifactRef, LiveAuthorityStore, LiveUnit
from app.products.image_model import ImageAssetKind
from app.system.execution_mode import ExecutionModeState
from tests.live_support import (
    FixedCandidates,
    PermittedMode,
    ProvenProofs,
    ScriptedSender,
)
from tests.product_support import count
from tests.register_support import MARKET, establish

pytestmark = pytest.mark.integration

APPROVAL = "5826077470"
CID = "corr-g3a"
REV = "11111111-1111-4111-8111-111111111111"
REV2 = "22222222-2222-4222-8222-222222222222"
FP = "a" * 64
FP2 = "b" * 64
BYTES_A = b"\x89PNG artifact A"
BYTES_B = b"\x89PNG artifact B"
SHA_A = hashlib.sha256(BYTES_A).hexdigest()
SHA_B = hashlib.sha256(BYTES_B).hexdigest()
DERIVED_A = ArtifactRef(
    ImageAssetKind.DERIVED_ARTIFACT, SHA_A, "d1d1d1d1-0000-4000-8000-000000000001"
)
# The same bytes under another derivation and under the source kind: provenance differs only.
DERIVED_A2 = ArtifactRef(
    ImageAssetKind.DERIVED_ARTIFACT, SHA_A, "d2d2d2d2-0000-4000-8000-000000000002"
)
SOURCE_A = ArtifactRef(ImageAssetKind.SOURCE_ASSET, SHA_A, None)
DERIVED_B = ArtifactRef(
    ImageAssetKind.DERIVED_ARTIFACT, SHA_B, "d3d3d3d3-0000-4000-8000-000000000003"
)
REF = "https://shop-phinf.pstatic.net/20260925_1/a.png"
REF_B = "https://shop-phinf.pstatic.net/20260925_1/b.png"
HOSTS = WireHostPolicy({MARKET: "api.commerce.naver.com"})


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-g3a-1")


@pytest.fixture
def smartstore(container: Container, config: AppConfig) -> str:
    """A canonical SmartStore account: the production host rule knows this marketplace."""
    return establish(container, config, "smartstore", "uid-smartstore-g3a-1")


class NotLive:
    """The M0 execution mode, as a stand-in the tests can pass explicitly."""

    def state(self) -> ExecutionModeState:
        return ExecutionModeState(
            mode=ExecutionMode.DRY_RUN, live_writes_permitted=False, policy="M0_DRY_RUN_ONLY"
        )


def store_of(container: Container) -> LiveAuthorityStore:
    return LiveAuthorityStore(container.db, container.clock, container.audit)


def candidates(*revisions: tuple[str, str]) -> FixedCandidates:
    return FixedCandidates(
        {
            rev: CandidateState(rev, current=True, ready=True, fingerprint=fp)
            for rev, fp in revisions
        }
    )


def uploads(
    container: Container,
    *,
    sender: ScriptedSender | None = None,
    known: FixedCandidates | None = None,
    proofs: Any = None,
    mode: Any = None,
    hosts: WireHostPolicy = HOSTS,
    process_run_id: str | None = None,
) -> tuple[AssetUploadService, ScriptedSender]:
    store = store_of(container)
    stack = SafetyStack(
        store=store,
        mode=mode or PermittedMode(),
        proofs=proofs or ProvenProofs(),
        clock=container.clock,
    )
    the_sender = sender or ScriptedSender()
    service = AssetUploadService(
        store=store,
        stack=stack,
        sender=the_sender,
        hosts=hosts,
        candidates=known or candidates((REV, FP), (REV2, FP2)),
        clock=container.clock,
        process_run_id=process_run_id,
    )
    return service, the_sender


def grant(
    container: Container,
    account: str,
    artifacts: list[ArtifactRef],
    *,
    rev: str = REV,
    fp: str = FP,
    budget: int = 3,
    profile: str = "smartstore-default",
    market: str = MARKET,
) -> str:
    now = container.clock.now()
    with store_of(container).transaction() as unit:
        return unit.issue_asset_grant(
            marketplace_key=market,
            marketplace_account_id=account,
            preparation_revision_id=rev,
            candidate_fingerprint=fp,
            artifacts=artifacts,
            asset_profile=profile,
            budget=budget,
            not_before=now,
            expires_at=now + timedelta(hours=1),
            approved_by="operator",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        ).grant_id


def release(container: Container) -> None:
    with store_of(container).transaction() as unit:
        unit.release(
            actor="operator",
            reason_code="CANARY_WINDOW",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )


def request(
    grant_id: str,
    artifact: ArtifactRef,
    content: bytes = BYTES_A,
    *,
    file_name: str = "a.png",
    media_type: str = "image/png",
) -> AssetUploadRequest:
    return AssetUploadRequest(
        grant_id=grant_id,
        artifact=artifact,
        content=content,
        file_name=file_name,
        media_type=media_type,
        actor="operator",
        correlation_id=CID,
    )


def applied(ref: str = REF) -> UploadSendResult:
    return UploadSendResult(UploadAttemptState.APPLIED_PROVEN, provider_asset_ref=ref)


def attempts(container: Container) -> list[tuple[str, ...]]:
    with sqlite3.connect(container.config.data_dir / "runtime" / "icbm.db") as raw:
        return [
            tuple(row)
            for row in raw.execute(
                "SELECT state, artifact_sha256, derivation_id, file_name, media_type"
                " FROM asset_upload_attempts ORDER BY started_at, attempt_no"
            )
        ]


def audit_types(container: Container) -> list[str]:
    return [event.event_type for event in container.audit.list_events(limit=500)]


def refused(code: str, call: Any) -> MutationRefused:
    with pytest.raises(MutationRefused) as caught:
        call()
    assert caught.value.code == code, caught.value.details
    return caught.value


# ---------------------------------------------------------------- the production wiring (M0)


def test_the_production_upload_path_refuses_every_upload_at_this_main(
    container: Container, smartstore: str
) -> None:
    grant_id = grant(container, smartstore, [DERIVED_A], market="smartstore")
    release(container)
    refusal = refused(
        live_model.MODE_NOT_LIVE,
        lambda: container.asset_uploads.upload(request(grant_id, DERIVED_A)),
    )
    # Every failing layer is named, not only the first.
    reasons = {layer["reason"] for layer in refusal.details["layers"]}
    assert {
        live_model.MODE_NOT_LIVE,
        live_model.SENDER_NOT_WIRED,
        live_model.ELIGIBILITY_UNPROVEN,
        live_model.RESTORE_PROOF_ABSENT,
        live_model.RETENTION_UNPROVEN,
        live_model.VISUAL_UNRECORDED,
    } <= reasons
    # Nothing started, nothing spent, and the refusal is audited.
    assert count(container.config, "asset_upload_attempts") == 0
    stored = container.live_authority.grant_record(grant_id)
    assert stored is not None and stored.budget_used == 0 and stored.state is GrantState.ACTIVE
    assert "LIVE_MUTATION_REFUSED" in audit_types(container)
    readiness = container.asset_uploads.readiness(grant_id)
    assert readiness.verdict is Verdict.BLOCKED
    assert live_model.MODE_NOT_LIVE in readiness.missing
    assert container.execution_mode.state().live_writes_permitted is False


def test_the_production_brake_is_engaged_when_nothing_was_ever_recorded(
    container: Container,
) -> None:
    brake = container.live_authority.brake()
    assert (brake.state, brake.recorded) == (BrakeState.ENGAGED, False)


# ---------------------------------------------------------------- grants (§3.2, §3.3)


def test_a_grant_is_bounded_exact_and_prose_free(container: Container, account: str) -> None:
    now = container.clock.now()
    base: dict[str, Any] = {
        "marketplace_key": MARKET,
        "marketplace_account_id": account,
        "preparation_revision_id": REV,
        "candidate_fingerprint": FP,
        "artifacts": [DERIVED_A],
        "asset_profile": "p",
        "budget": 1,
        "not_before": now,
        "expires_at": now + timedelta(hours=1),
        "approved_by": "operator",
        "authorization_ref": APPROVAL,
        "correlation_id": CID,
    }
    bad = [
        {"expires_at": now + timedelta(hours=5)},  # longer than the server-owned maximum
        {"expires_at": now - timedelta(seconds=1)},  # already over / not after its start
        {"budget": 0},
        {"budget": live_model.MAX_ASSET_BUDGET + 1},
        {"authorization_ref": "please go ahead"},  # a reference, never prose
        {"artifacts": []},
        {"candidate_fingerprint": "not-a-digest"},
    ]
    for override in bad:
        with pytest.raises(InputValidationError), store_of(container).transaction() as unit:
            unit.issue_asset_grant(**(base | override))
    with pytest.raises(PolicyBlockedError), store_of(container).transaction() as unit:
        unit.issue_asset_grant(**(base | {"marketplace_account_id": "unbound-account"}))
    import inspect

    for method in (LiveUnit.issue_asset_grant, LiveUnit.issue_create_grant):
        names = set(inspect.signature(method).parameters)
        assert not names & {"confirmation", "confirmation_text", "prose", "note"}
    assert count(container.config, "live_grants") == 0


def test_terminal_grants_never_return_and_their_binding_never_changes(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A], budget=1)
    with store_of(container).transaction() as unit:
        unit.revoke(grant_id, actor="operator", reason_code="OPERATOR_REVOKED", correlation_id=CID)
    with pytest.raises(InputValidationError), store_of(container).transaction() as unit:
        unit.revoke(grant_id, actor="operator", reason_code="AGAIN", correlation_id=CID)
    db = container.config.data_dir / "runtime" / "icbm.db"
    with sqlite3.connect(db) as raw:
        for statement in (
            f"UPDATE live_grants SET state = 'ACTIVE', ended_at = NULL, ended_by = NULL,"
            f" end_reason = NULL WHERE grant_id = '{grant_id}'",
            f"DELETE FROM live_grants WHERE grant_id = '{grant_id}'",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)
    other = grant(container, account, [DERIVED_A])
    with sqlite3.connect(db) as raw:
        for statement in (
            f"UPDATE live_grants SET asset_profile = 'x' WHERE grant_id = '{other}'",
            f"UPDATE live_grants SET budget_max = 9 WHERE grant_id = '{other}'",
            f"UPDATE live_grants SET budget_used = 2 WHERE grant_id = '{other}'",
            f"UPDATE live_grants SET expires_at = '2099-01-01 00:00:00' WHERE grant_id = '{other}'",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)
    # A window that is over is EXPIRED, terminal, and no longer matches anything.
    container.clock.advance(2 * 3600)  # type: ignore[attr-defined]
    with store_of(container).transaction() as unit:
        assert unit.expire_due(correlation_id=CID) == (other,)
    stored = container.live_authority.grant_record(other)
    assert stored is not None and stored.state is GrantState.EXPIRED


# ---------------------------------------------------------------- the brake (§4.1)


def test_the_brake_is_fail_closed_audited_and_survives_a_restart(
    container: Container, account: str
) -> None:
    authority = container.live_authority
    with pytest.raises(InputValidationError):
        authority.release_brake(
            actor="operator", reason_code="CANARY", authorization_ref="yes", correlation_id=CID
        )
    released = authority.release_brake(
        actor="operator", reason_code="CANARY", authorization_ref=APPROVAL, correlation_id=CID
    )
    assert (released.state, released.generation) == (BrakeState.RELEASED, 1)
    engaged = authority.engage_brake(
        actor="system", reason_code="CRASH_RECOVERY", correlation_id=CID
    )
    assert (engaged.state, engaged.generation) == (BrakeState.ENGAGED, 2)
    assert audit_types(container).count("PROTECTED_WRITE_BRAKE_CHANGED") == 2
    # A fresh owner over the same database — a restart — reads the same engaged brake.
    assert store_of(container).brake().state is BrakeState.ENGAGED
    with sqlite3.connect(container.config.data_dir / "runtime" / "icbm.db") as raw:
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute("UPDATE protected_write_brakes SET state = 'RELEASED', generation = 9")
        with pytest.raises(sqlite3.DatabaseError):
            raw.execute("DELETE FROM protected_write_brakes")
        # Every change moves the generation by exactly one: never skipped, never kept.
        for statement in (
            "UPDATE protected_write_brakes SET generation = generation + 5",
            "UPDATE protected_write_brakes SET reason_code = 'OTHER'",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)
    # Releasing never resurrects a revoked grant.
    grant_id = grant(container, account, [DERIVED_A])
    authority.revoke(grant_id, actor="operator", reason_code="OPERATOR_REVOKED", correlation_id=CID)
    authority.release_brake(
        actor="operator", reason_code="CANARY", authorization_ref=APPROVAL, correlation_id=CID
    )
    stored = authority.grant_record(grant_id)
    assert stored is not None and stored.state is GrantState.REVOKED


def test_an_unreadable_brake_refuses_like_an_engaged_one(
    container: Container, account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied()]))

    def unreadable(self: LiveUnit) -> Any:
        raise OperationalError("SELECT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(LiveUnit, "brake", unreadable)
    refused(live_model.BRAKE_UNREADABLE, lambda: service.upload(request(grant_id, DERIVED_A)))
    assert sender.calls == []


def test_an_engaged_brake_and_every_unproven_prerequisite_refuse_before_transmission(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    service, sender = uploads(container, sender=ScriptedSender(script=[applied()]))
    refused(live_model.BRAKE_ENGAGED, lambda: service.upload(request(grant_id, DERIVED_A)))
    release(container)
    for missing, code in (
        ("eligibility", live_model.ELIGIBILITY_UNPROVEN),
        ("restore", live_model.RESTORE_PROOF_ABSENT),
        ("retention", live_model.RETENTION_UNPROVEN),
        ("visual", live_model.VISUAL_UNRECORDED),
    ):
        partial, _ = uploads(
            container, sender=sender, proofs=ProvenProofs(missing=frozenset({missing}))
        )
        refused(code, lambda partial=partial: partial.upload(request(grant_id, DERIVED_A)))
    unwired, _ = uploads(container, sender=ScriptedSender(wired=False))
    refused(live_model.SENDER_NOT_WIRED, lambda: unwired.upload(request(grant_id, DERIVED_A)))
    unadopted, _ = uploads(container, sender=ScriptedSender(adopted=False))
    refused(live_model.ENDPOINT_NOT_ADOPTED, lambda: unadopted.upload(request(grant_id, DERIVED_A)))
    assert sender.calls == [] and count(container.config, "asset_upload_attempts") == 0
    stored = store_of(container).grant_record(grant_id)
    assert stored is not None and stored.budget_used == 0


# ---------------------------------------------------------------- the upload path (§3.4)


def test_an_applied_upload_is_started_with_its_budget_and_yields_the_only_asset(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A], budget=1)
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied()]))
    result = service.upload(request(grant_id, DERIVED_A))
    assert result.attempt.state is UploadAttemptState.APPLIED_PROVEN
    assert result.prepared is not None
    assert (result.prepared.provider_asset_ref, result.prepared.sha256) == (REF, SHA_A)
    assert result.prepared.candidate_fingerprint == FP
    assert len(sender.calls) == 1
    stored = store_of(container).grant_record(grant_id)
    assert stored is not None and (stored.budget_used, stored.state) == (1, GrantState.EXHAUSTED)
    events = audit_types(container)
    for event in (
        "LIVE_GRANT_ISSUED",
        "LIVE_GRANT_CONSUMED",
        "ASSET_UPLOAD_ATTEMPT_STARTED",
        "ASSET_UPLOAD_ATTEMPT_SETTLED",
    ):
        assert event in events, event
    # The replay key holds the wire boundary only; the rest is provenance.
    (attempt,) = store_of(container).attempts(result.attempt.replay_key)
    assert (attempt.wire_method, attempt.wire_host, attempt.wire_path) == (
        "POST",
        "api.commerce.naver.com",
        "/external/v1/product-images/upload",
    )


def test_the_started_record_and_the_budget_commit_together_or_not_at_all(
    container: Container, account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied()]))
    real_consume = LiveUnit.consume

    def failing_after_consume(self: LiveUnit, *args: Any, **kwargs: Any) -> Any:
        real_consume(self, *args, **kwargs)
        raise RuntimeError("the unit dies after the budget was spent")

    monkeypatch.setattr(LiveUnit, "consume", failing_after_consume)
    with pytest.raises(RuntimeError):
        service.upload(request(grant_id, DERIVED_A))
    # Neither the spent budget nor a STARTED attempt survived, and nothing was sent.
    assert sender.calls == [] and count(container.config, "asset_upload_attempts") == 0
    stored = store_of(container).grant_record(grant_id)
    assert stored is not None and stored.budget_used == 0


@pytest.mark.parametrize(
    "later",
    [
        pytest.param({"artifact": DERIVED_A2}, id="another-derivation"),
        pytest.param({"artifact": SOURCE_A}, id="source-vs-derived-kind"),
        pytest.param({"file_name": "renamed.png"}, id="another-file-name"),
        pytest.param({"media_type": "image/jpeg"}, id="another-mime"),
        pytest.param({"rev": REV2, "fp": FP2}, id="new-preparation-candidate-and-grant"),
        pytest.param({"profile": "other-profile"}, id="another-local-profile"),
        pytest.param({"label": "m5-image-upload-r2"}, id="another-contract-label"),
    ],
)
def test_an_unknown_upload_fences_its_key_whatever_the_provenance(
    container: Container, account: str, later: dict[str, Any]
) -> None:
    first = grant(container, account, [DERIVED_A, DERIVED_A2, SOURCE_A])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[RuntimeError("timeout")]))
    result = service.upload(request(first, DERIVED_A))
    assert result.attempt.state is UploadAttemptState.UPLOAD_UNKNOWN and result.prepared is None
    artifact = later.get("artifact", DERIVED_A)
    second = grant(
        container,
        account,
        [artifact],
        rev=later.get("rev", REV),
        fp=later.get("fp", FP),
        profile=later.get("profile", "smartstore-default"),
    )
    retry, retry_sender = uploads(
        container,
        sender=ScriptedSender(script=[applied()], label=later.get("label", "m5-image-upload-r1")),
    )
    refused(
        live_model.REPLAY_UNRESOLVED,
        lambda: retry.upload(
            request(
                second,
                artifact,
                file_name=later.get("file_name", "a.png"),
                media_type=later.get("media_type", "image/png"),
            )
        ),
    )
    assert retry_sender.calls == [] and len(sender.calls) == 1
    # Readiness of the new grant reads the whole scope, not only its own candidate's attempts.
    readiness = retry.readiness(second)
    assert readiness.verdict is Verdict.BLOCKED
    assert [a.reason_code for a in readiness.artifacts] == [live_model.REPLAY_UNRESOLVED]
    stored = store_of(container).grant_record(second)
    assert stored is not None and stored.budget_used == 0


def test_a_proven_non_application_clears_the_fence_for_a_retry(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A], budget=2)
    release(container)
    service, sender = uploads(
        container,
        sender=ScriptedSender(script=[TransmissionPrecluded("refused locally"), applied()]),
    )
    first = service.upload(request(grant_id, DERIVED_A))
    assert first.attempt.state is UploadAttemptState.NOT_APPLIED_PROVEN
    second = service.upload(request(grant_id, DERIVED_A))
    assert second.attempt.state is UploadAttemptState.APPLIED_PROVEN
    assert (first.attempt.attempt_no, second.attempt.attempt_no) == (1, 2)
    assert len(sender.calls) == 2


@pytest.mark.parametrize("artifact", [DERIVED_A2, SOURCE_A], ids=["derivation", "kind"])
def test_an_applied_upload_is_never_re_sent_through_equal_bytes(
    container: Container, account: str, artifact: ArtifactRef
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied(), applied()]))
    assert service.upload(request(grant_id, DERIVED_A)).prepared is not None
    other = grant(container, account, [artifact], rev=REV2, fp=FP2)
    refused(
        live_model.REPLAY_APPLIED_REUSE_NOT_ADOPTED,
        lambda: service.upload(request(other, artifact, file_name="again.png")),
    )
    assert len(sender.calls) == 1


def test_one_units_images_each_have_their_own_fence_and_never_block_each_other(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A, DERIVED_B])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied(), applied(REF_B)]))
    before = service.readiness(grant_id)
    assert [a.uploadable for a in before.artifacts] == [True, True]
    assert service.upload(request(grant_id, DERIVED_A)).prepared is not None
    middle = service.readiness(grant_id)
    by_sha = {a.artifact.sha256: a for a in middle.artifacts}
    assert by_sha[SHA_A].reason_code == live_model.REPLAY_APPLIED_REUSE_NOT_ADOPTED
    assert by_sha[SHA_B].uploadable
    # The applied first image does not deadlock the second one.
    assert service.upload(request(grant_id, DERIVED_B, BYTES_B, file_name="b.png")).prepared
    assert len(sender.calls) == 2


def test_the_same_binary_selected_twice_in_one_unit_names_the_liveness_limit(
    container: Container, account: str
) -> None:
    # A representative and a detail image that are the same bytes: one replay key, two slots.
    grant_id = grant(container, account, [DERIVED_A, SOURCE_A])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied(), applied()]))
    readiness = service.readiness(grant_id)
    reasons = sorted(str(a.reason_code) for a in readiness.artifacts)
    assert reasons == sorted(["None", REPLAY_DUPLICATE_IN_UNIT])
    assert service.upload(request(grant_id, DERIVED_A)).prepared is not None
    # The second slot cannot be filled by a fresh upload, and reuse/rebind is not adopted.
    refused(
        live_model.REPLAY_APPLIED_REUSE_NOT_ADOPTED,
        lambda: service.upload(request(grant_id, SOURCE_A)),
    )
    assert len(sender.calls) == 1


@pytest.mark.parametrize(
    "host",
    [
        "API.Commerce.Naver.COM",
        "api.commerce.naver.com.",
        "api.commerce.naver.com:443",
        "https://user@api.commerce.naver.com/",
        "alias.commerce.example",
    ],
)
def test_host_spellings_and_aliases_cannot_split_the_replay_scope(
    container: Container, account: str, host: str
) -> None:
    hosts = WireHostPolicy(
        {MARKET: "api.commerce.naver.com"},
        {MARKET: {"alias.commerce.example": "api.commerce.naver.com"}},
    )
    first = grant(container, account, [DERIVED_A])
    release(container)
    service, _ = uploads(
        container, sender=ScriptedSender(script=[RuntimeError("reset")]), hosts=hosts
    )
    service.upload(request(first, DERIVED_A))
    spelled = ScriptedSender(
        script=[applied()],
        wire_value=("post", host, "/external//v1/product-images/upload/"),
    )
    other, _ = uploads(container, sender=spelled, hosts=hosts)
    refused(
        live_model.REPLAY_UNRESOLVED,
        lambda: other.upload(request(grant(container, account, [DERIVED_A]), DERIVED_A)),
    )
    assert spelled.calls == []


@pytest.mark.parametrize(
    "host", ["proxy.internal.example", "10.0.0.8", "api.commerce.naver.com:8443", "[::1]"]
)
def test_an_unproven_host_is_undeterminable_and_blocks(
    container: Container, account: str, host: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    sender = ScriptedSender(script=[applied()], wire_value=("POST", host, "/external/v1/x"))
    service, _ = uploads(container, sender=sender)
    refused(
        live_model.REPLAY_KEY_UNDETERMINABLE, lambda: service.upload(request(grant_id, DERIVED_A))
    )
    assert sender.calls == [] and count(container.config, "asset_upload_attempts") == 0
    readiness = service.readiness(grant_id)
    assert readiness.verdict is Verdict.BLOCKED
    assert readiness.artifacts[0].reason_code == live_model.REPLAY_KEY_UNDETERMINABLE


def test_a_crash_leaves_started_and_the_next_process_settles_it_unknown(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    service, _ = uploads(
        container, sender=ScriptedSender(script=[KeyboardInterrupt()]), process_run_id="run-1"
    )
    with pytest.raises(KeyboardInterrupt):
        service.upload(request(grant_id, DERIVED_A))
    assert [row[0] for row in attempts(container)] == ["STARTED"]
    # The same process never settles its own in-flight attempt ...
    assert service.settle_interrupted(correlation_id=CID) == ()
    # ... and even open, it fences its key.
    again, again_sender = uploads(container, sender=ScriptedSender(script=[applied()]))
    refused(live_model.REPLAY_UNRESOLVED, lambda: again.upload(request(grant_id, DERIVED_A)))
    # A restart: the next process settles it UPLOAD_UNKNOWN, and the fence stays closed.
    restarted, _ = uploads(container, process_run_id="run-2")
    assert len(restarted.settle_interrupted(correlation_id=CID)) == 1
    assert [row[0] for row in attempts(container)] == ["UPLOAD_UNKNOWN"]
    refused(live_model.REPLAY_UNRESOLVED, lambda: again.upload(request(grant_id, DERIVED_A)))
    assert again_sender.calls == []


def test_an_unrecordable_or_raising_sender_result_is_unknown_never_applied(
    container: Container, account: str
) -> None:
    # Two grants of one artifact each: an unresolved A must not be what refuses B here.
    grant_a = grant(container, account, [DERIVED_A])
    grant_b = grant(container, account, [DERIVED_B])
    release(container)
    unsafe = UploadSendResult(
        UploadAttemptState.APPLIED_PROVEN, provider_asset_ref="https://x.example/a?token=secret"
    )
    service, _ = uploads(container, sender=ScriptedSender(script=[unsafe, ValueError("boom")]))
    first = service.upload(request(grant_a, DERIVED_A))
    second = service.upload(request(grant_b, DERIVED_B, BYTES_B))
    assert first.attempt.state is UploadAttemptState.UPLOAD_UNKNOWN and first.prepared is None
    assert second.attempt.state is UploadAttemptState.UPLOAD_UNKNOWN and second.prepared is None
    assert (first.attempt.outcome_reason, second.attempt.outcome_reason) == (
        "UPLOAD_RESULT_UNRECORDABLE",
        "SENDER_RAISED",
    )


def test_an_unresolved_selected_artifact_holds_the_whole_asset_stage(
    container: Container, account: str
) -> None:
    # G3-26 (review 5827063895 B2): A+B selected, B UPLOAD_UNKNOWN, A otherwise open.
    grant_id = grant(container, account, [DERIVED_A, DERIVED_B])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[RuntimeError("reset")]))
    assert (
        service.upload(request(grant_id, DERIVED_B, BYTES_B, file_name="b.png")).attempt.state
        is UploadAttemptState.UPLOAD_UNKNOWN
    )
    readiness = service.readiness(grant_id)
    assert readiness.verdict is Verdict.BLOCKED
    assert REPLAY_UNRESOLVED_IN_UNIT in readiness.missing
    spent = store_of(container).grant_record(grant_id)
    assert spent is not None
    refused(REPLAY_UNRESOLVED_IN_UNIT, lambda: service.upload(request(grant_id, DERIVED_A)))
    # A started nothing and spent nothing; only B's one attempt was ever sent.
    assert [row[1] for row in attempts(container)] == [SHA_B]
    after = store_of(container).grant_record(grant_id)
    assert after is not None and after.budget_used == spent.budget_used == 1
    assert len(sender.calls) == 1


def test_an_applied_selected_artifact_stays_artifact_local(
    container: Container, account: str
) -> None:
    # Cross-audit 7 item 1 is kept: an APPLIED_PROVEN A never holds a different artifact B.
    grant_id = grant(container, account, [DERIVED_A, DERIVED_B])
    release(container)
    service, _ = uploads(container, sender=ScriptedSender(script=[applied(), applied(REF_B)]))
    assert service.upload(request(grant_id, DERIVED_A)).prepared is not None
    assert service.readiness(grant_id).verdict is Verdict.READY
    assert service.upload(request(grant_id, DERIVED_B, BYTES_B, file_name="b.png")).prepared


def test_an_unreadable_attempt_owner_refuses_and_blocks_the_stage(
    container: Container, account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant_id = grant(container, account, [DERIVED_A, DERIVED_B])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied()]))

    def unreadable(self: LiveUnit, key: Any) -> Any:
        raise OperationalError("SELECT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(LiveUnit, "fence", unreadable)
    refused(
        live_model.ATTEMPT_OWNER_UNREADABLE, lambda: service.upload(request(grant_id, DERIVED_A))
    )
    readiness = service.readiness(grant_id)
    assert readiness.verdict is Verdict.BLOCKED
    assert live_model.ATTEMPT_OWNER_UNREADABLE in readiness.missing
    assert sender.calls == [] and count(container.config, "asset_upload_attempts") == 0


def test_a_missing_or_wrong_stage_grant_is_an_audited_refusal(
    container: Container, account: str
) -> None:
    # Review 5827063895 B3: deny by default, before anything starts, and audited.
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied()]))
    missing = "00000000-0000-4000-8000-000000000000"
    refused(live_model.GRANT_MISSING, lambda: service.upload(request(missing, DERIVED_A)))
    with store_of(container).transaction() as unit:
        now = container.clock.now()
        create = unit.issue_create_grant(
            marketplace_key=MARKET,
            marketplace_account_id=account,
            registration_snapshot_id="snap-not-an-asset-unit",
            intent_id="intent-not-an-asset-unit",
            idempotency_key="idem-not-an-asset-unit",
            create_attempt_no=1,
            not_before=now,
            expires_at=now + timedelta(hours=1),
            approved_by="operator",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        ).grant_id
    refused(live_model.GRANT_MISSING, lambda: service.upload(request(create, DERIVED_A)))
    denials = [
        e
        for e in container.audit.list_events(limit=200)
        if e.event_type == "LIVE_MUTATION_REFUSED" and e.reason_code == live_model.GRANT_MISSING
    ]
    assert {e.target_ref for e in denials} == {f"live_grant:{missing}", f"live_grant:{create}"}
    assert all(e.outcome == "DENIED" for e in denials)
    assert sender.calls == [] and count(container.config, "asset_upload_attempts") == 0
    kept = store_of(container).grant_record(create)
    assert kept is not None and kept.budget_used == 0


def test_the_upload_must_be_the_granted_artifact_under_the_current_candidate(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    service, sender = uploads(container, sender=ScriptedSender(script=[applied()]))
    refused(
        live_model.CONTENT_DIGEST_MISMATCH,
        lambda: service.upload(request(grant_id, DERIVED_A, b"other bytes")),
    )
    refused(
        live_model.ARTIFACT_NOT_GRANTED,
        lambda: service.upload(request(grant_id, DERIVED_B, BYTES_B)),
    )
    drifted, _ = uploads(container, sender=sender, known=candidates((REV, FP2)))
    refused(live_model.CANDIDATE_DRIFT, lambda: drifted.upload(request(grant_id, DERIVED_A)))
    stale = FixedCandidates({REV: CandidateState(REV, current=False, ready=True, fingerprint=FP)})
    superseded, _ = uploads(container, sender=sender, known=stale)
    refused(live_model.CANDIDATE_NOT_READY, lambda: superseded.upload(request(grant_id, DERIVED_A)))
    assert sender.calls == []


def test_the_database_holds_the_replay_fence_and_terminal_attempts(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    service, _ = uploads(container, sender=ScriptedSender(script=[applied()]))
    service.upload(request(grant_id, DERIVED_A))
    db = container.config.data_dir / "runtime" / "icbm.db"
    with sqlite3.connect(db) as raw:
        columns = [row[1] for row in raw.execute("PRAGMA table_info(asset_upload_attempts)")]
        row = dict(
            zip(columns, raw.execute("SELECT * FROM asset_upload_attempts").fetchone(), strict=True)
        )
        copy = row | {
            "attempt_id": "copy-1",
            "attempt_no": 2,
            "state": "STARTED",
            "finished_at": None,
            "provider_asset_ref": None,
            "derivation_id": "another-derivation",
            "file_name": "other.png",
        }
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                f"INSERT INTO asset_upload_attempts ({', '.join(copy)})"
                f" VALUES ({', '.join('?' for _ in copy)})",
                tuple(copy.values()),
            )
        for statement in (
            "UPDATE asset_upload_attempts SET state = 'NOT_APPLIED_PROVEN',"
            " provider_asset_ref = NULL, outcome_reason = 'X'",
            "UPDATE asset_upload_attempts SET file_name = 'x.png'",
            "DELETE FROM asset_upload_attempts",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)


def test_the_asset_stage_never_touches_the_create_only_scope_brake(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    service, _ = uploads(container, sender=ScriptedSender(script=[applied()]))
    service.upload(request(grant_id, DERIVED_A))
    assert count(container.config, "registration_execution_scopes") == 0
    assert count(container.config, "registration_attempts") == 0
    readiness = service.readiness(grant_id)
    assert all(layer.layer is not Layer.STAGE_GATE or layer.satisfied for layer in readiness.layers)
    assert MutationStage.ASSET is readiness.stage


def test_only_an_applied_attempt_may_carry_a_provider_asset(
    container: Container, account: str
) -> None:
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    service, _ = uploads(container, sender=ScriptedSender(script=[KeyboardInterrupt()]))
    with pytest.raises(KeyboardInterrupt):
        service.upload(request(grant_id, DERIVED_A))
    (open_attempt,) = store_of(container).attempts(_only_key(container))
    for state in (UploadAttemptState.NOT_APPLIED_PROVEN, UploadAttemptState.UPLOAD_UNKNOWN):
        with pytest.raises(InputValidationError), store_of(container).transaction() as unit:
            unit.finish_upload(
                open_attempt.attempt_id,
                state=state,
                actor="operator",
                correlation_id=CID,
                provider_asset_ref=REF,
                outcome_reason="ANY_REASON",
            )


def _only_key(container: Container) -> str:
    with sqlite3.connect(container.config.data_dir / "runtime" / "icbm.db") as raw:
        (key,) = raw.execute("SELECT replay_key FROM asset_upload_attempts").fetchone()
    return str(key)


# ---------------------------------------------------------------- restore targets (§7)


def test_a_proven_non_application_changes_the_asset_restore_target(
    container: Container, account: str
) -> None:
    # Review 5827905179 control 1: the proof taken for state S0 never admits the retry.
    grant_id = grant(container, account, [DERIVED_A], budget=2)
    release(container)
    recording = ProvenProofs()
    first, _ = uploads(
        container,
        sender=ScriptedSender(script=[TransmissionPrecluded("refused locally")]),
        proofs=recording,
    )
    assert first.upload(request(grant_id, DERIVED_A)).attempt.state is (
        UploadAttemptState.NOT_APPLIED_PROVEN
    )
    before = recording.restore_targets[-1]
    stale, sender = uploads(
        container, sender=ScriptedSender(script=[applied()]), proofs=ProvenProofs(accept={before})
    )
    refused(live_model.RESTORE_PROOF_ABSENT, lambda: stale.upload(request(grant_id, DERIVED_A)))
    assert sender.calls == []
    now = ProvenProofs()
    reader, _ = uploads(container, proofs=now)
    reader.readiness(grant_id)
    after = now.restore_targets[-1]
    assert after != before
    fresh, fresh_sender = uploads(
        container, sender=ScriptedSender(script=[applied()]), proofs=ProvenProofs(accept={after})
    )
    assert fresh.upload(request(grant_id, DERIVED_A)).prepared is not None
    assert len(fresh_sender.calls) == 1


def test_another_selected_artifacts_replay_state_stales_the_asset_restore_target(
    container: Container, account: str
) -> None:
    # Review 5827905179 control 2: B's new replay state stales the target taken before A.
    grant_id = grant(container, account, [DERIVED_A, DERIVED_B])
    release(container)
    # The target of an admission of A itself, taken while the execution mode still refuses it.
    recording = ProvenProofs()
    held, _ = uploads(container, proofs=recording, mode=NotLive())
    refused(live_model.MODE_NOT_LIVE, lambda: held.upload(request(grant_id, DERIVED_A)))
    before = recording.restore_targets[-1]
    service, _ = uploads(container, sender=ScriptedSender(script=[applied(REF_B)]))
    assert service.upload(request(grant_id, DERIVED_B, BYTES_B, file_name="b.png")).prepared
    stale, sender = uploads(
        container, sender=ScriptedSender(script=[applied()]), proofs=ProvenProofs(accept={before})
    )
    refused(live_model.RESTORE_PROOF_ABSENT, lambda: stale.upload(request(grant_id, DERIVED_A)))
    assert sender.calls == []


def test_an_unreadable_attempt_owner_yields_no_restore_target(
    container: Container, account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 5827905179 control 4: no readable attempt truth, no current restore target.
    grant_id = grant(container, account, [DERIVED_A])
    release(container)
    proofs = ProvenProofs()
    service, sender = uploads(container, sender=ScriptedSender(script=[applied()]), proofs=proofs)

    def unreadable(self: LiveUnit, key: Any) -> Any:
        raise OperationalError("SELECT", {}, Exception("disk I/O error"))

    monkeypatch.setattr(LiveUnit, "attempts", unreadable)
    refusal = refused(
        live_model.RESTORE_PROOF_ABSENT, lambda: service.upload(request(grant_id, DERIVED_A))
    )
    reasons = {layer["reason"] for layer in refusal.details["layers"]}
    assert live_model.ATTEMPT_OWNER_UNREADABLE in reasons
    assert proofs.restore_targets == [] and sender.calls == []
