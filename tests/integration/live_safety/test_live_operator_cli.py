"""The protected pre-LIVE operator commands (Issue #89 architect resolution 5915900049 D3).

``icbm live inspect / eligibility-packet / record-eligibility / issue-asset-grant /
issue-create-grant / release-brake / engage-brake`` extend the existing ``icbm live`` family. Each
protected action calls exactly one existing owner method, ``inspect`` reads the two existing
read-only projections, and each prints the answer; no command owns truth:

- an input that is not even well-formed (a naive time, an unreadable file) is refused before any
  owner is asked, and nothing is written;
- every other refusal is the owner's own code, and nothing is written;
- what the owner records is exactly what it would record through its own method — the identities
  are the owner's, never the command's;
- every command holds the data directory, and none reaches a provider or changes the execution
  mode: after a grant and a released brake, the send-time stack still refuses under M0.
"""

import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.live_safety import model as live_model
from app.capabilities.live_safety.assets import PreparationCandidateGate
from app.capabilities.live_safety.model import MutationRefused, MutationStage
from app.config import AppConfig, database_path
from app.container import Container
from app.interface import cli
from app.stages.register.execution import CREATE_ENDPOINT_GROUP
from tests.integration.live_safety.test_canary_eligibility import proven_checks
from tests.integration.live_safety.test_g3b_restore_retention import (  # noqa: F401 - fixtures
    api,
    container,
    durable_unit,
    frozen_unit,
    proofs,
)
from tests.support.gate1_support import OPERATOR

pytestmark = pytest.mark.integration

# A synthetic approval reference of the owners' comment-id form. It stands for the separate,
# per-action approval rule §7.2 requires; no real approval exists or is implied by these tests,
# and the resolution that decided the command surface (5915900049 D3) grants none.
APPROVAL = "1000000001"


def _window() -> list[str]:
    now = datetime.now(UTC)
    return [
        "--not-before",
        (now - timedelta(minutes=1)).isoformat(),
        "--expires-at",
        (now + timedelta(hours=1)).isoformat(),
        "--approved-by",
        "owner",
        "--authorization-ref",
        APPROVAL,
    ]


@pytest.fixture
def fresh(
    tmp_path_factory: pytest.TempPathFactory,
    migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """A migrated data directory the commands own, with no unit and no LIVE record."""
    data = tmp_path_factory.mktemp("live-cli")
    database = database_path(data)
    database.parent.mkdir(parents=True)
    shutil.copyfile(migrated_template, database)
    monkeypatch.setenv("ICBM_DATA_DIR", str(data))
    monkeypatch.setenv("ICBM_SECRET_BACKEND", "memory")
    return database


def _rows(database: Path, table: str) -> int:
    with sqlite3.connect(database) as raw:
        return int(raw.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, Any, str]:
    code = cli.main(["live", *args])
    out, err = capsys.readouterr()
    # The owner's answer is the last stdout line; the application log may precede it.
    return code, (json.loads(out.strip().splitlines()[-1]) if code == 0 else None), err


def test_every_operator_command_is_an_owning_live_command() -> None:
    commands = {("live", name) for name in cli.LIVE_OPERATOR_COMMANDS}
    assert commands == {
        ("live", "inspect"),
        ("live", "eligibility-packet"),
        ("live", "record-eligibility"),
        ("live", "issue-asset-grant"),
        ("live", "issue-create-grant"),
        ("live", "issue-delete-grant"),
        ("live", "issue-dispatch-grant"),
        ("live", "release-brake"),
        ("live", "engage-brake"),
        ("live", "restore-drill-asset"),
        ("live", "restore-drill-create"),
        ("live", "prove-retention"),
        ("live", "record-residual-risk-acceptance"),
        # The one command that reaches a provider: a bounded upload run (owner decision
        # 5975217061), still decided upload by upload by the send-time stack.
        ("live", "upload-assets"),
    }
    # They hold the data directory like every other writer; none is classified read-only.
    assert commands <= set(cli.OWNING_COMMANDS)
    assert not commands & set(cli.READ_ONLY_COMMANDS)
    assert set(cli._OPERATIONS) == set(cli.LIVE_OPERATOR_COMMANDS)


def test_the_brake_commands_call_the_brake_owner_and_inspect_reads_it(
    fresh: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, shown, _ = _run(capsys, "inspect")
    assert code == 0
    # No row is the fail-closed default: ENGAGED.
    assert shown["live"]["brake"]["state"] == "ENGAGED"
    assert shown["live"]["brake"]["recorded"] is False
    assert shown["canary"]["verdict"] == "BLOCKED"
    # A release without its authorization reference is not even a command.
    with pytest.raises(SystemExit):
        cli.main(["live", "release-brake", "--actor", OPERATOR, "--reason-code", "CANARY"])
    capsys.readouterr()
    code, released, _ = _run(
        capsys,
        "release-brake",
        "--actor",
        OPERATOR,
        "--reason-code",
        "CANARY_WINDOW",
        "--authorization-ref",
        APPROVAL,
    )
    assert code == 0
    assert released["state"] == "RELEASED" and released["authorization_ref"] == APPROVAL
    code, shown, _ = _run(capsys, "inspect")
    assert shown["live"]["brake"]["state"] == "RELEASED"
    code, engaged, _ = _run(
        capsys, "engage-brake", "--actor", OPERATOR, "--reason-code", "OPERATOR_STOP"
    )
    assert code == 0 and engaged["state"] == "ENGAGED"
    assert engaged["generation"] == released["generation"] + 1


def test_malformed_inputs_are_refused_before_any_owner_and_write_nothing(
    fresh: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    naive = ["--not-before", "2026-10-01T00:00:00", "--expires-at", "2026-10-01T01:00:00"]
    code = cli.main(
        [
            "live",
            "issue-create-grant",
            "--intent-id",
            "i-1",
            *naive,
            "--approved-by",
            "owner",
            "--authorization-ref",
            APPROVAL,
        ]
    )
    assert code == 1 and "LIVE_CLI_TIME_INVALID" in capsys.readouterr().err
    missing = tmp_path / "missing.json"
    code = cli.main(
        [
            "live",
            "record-eligibility",
            "--preparation-id",
            "p-1",
            "--packet-digest",
            "0" * 64,
            "--checks",
            str(missing),
            "--actor",
            OPERATOR,
        ]
    )
    assert code == 1 and "LIVE_CLI_FILE_UNREADABLE" in capsys.readouterr().err
    listed = tmp_path / "checks.json"
    listed.write_text("[]")
    code = cli.main(
        [
            "live",
            "record-eligibility",
            "--preparation-id",
            "p-1",
            "--packet-digest",
            "0" * 64,
            "--checks",
            str(listed),
            "--actor",
            OPERATOR,
        ]
    )
    assert code == 1 and "LIVE_CLI_CHECKS_INVALID" in capsys.readouterr().err
    artifacts = tmp_path / "artifacts.json"
    artifacts.write_text(json.dumps([{"asset_kind": "NOT_A_KIND", "sha256": "a" * 64}]))
    code = cli.main(
        [
            "live",
            "issue-asset-grant",
            "--marketplace",
            "smartstore",
            "--account",
            "a-1",
            "--preparation-revision-id",
            "r-1",
            "--candidate-fingerprint",
            "c" * 64,
            "--artifacts",
            str(artifacts),
            "--asset-profile",
            "p",
            "--budget",
            "1",
            *_window(),
        ]
    )
    assert code == 1 and "LIVE_CLI_ARTIFACTS_INVALID" in capsys.readouterr().err
    assert _rows(fresh, "live_grants") == 0
    assert _rows(fresh, "canary_eligibility_records") == 0


def test_an_owner_refusal_is_printed_with_its_own_code_and_writes_nothing(
    fresh: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = cli.main(["live", "issue-create-grant", "--intent-id", "no-such-intent", *_window()])
    assert code == 1 and "LIVE_GRANT_INTENT_NOT_FOUND" in capsys.readouterr().err
    code = cli.main(["live", "eligibility-packet", "--preparation-id", "no-such-preparation"])
    assert code == 1
    assert "CANARY_ELIGIBILITY_PREPARATION_NOT_FOUND" in capsys.readouterr().err
    artifacts = tmp_path / "artifacts.json"
    artifacts.write_text("[]")
    code = cli.main(
        [
            "live",
            "issue-asset-grant",
            "--marketplace",
            "smartstore",
            "--account",
            "a-1",
            "--preparation-revision-id",
            "no-such-revision",
            "--candidate-fingerprint",
            "c" * 64,
            "--artifacts",
            str(artifacts),
            "--asset-profile",
            "p",
            "--budget",
            "1",
            *_window(),
        ]
    )
    assert code == 1 and "LIVE_GRANT_PREPARATION_NOT_FOUND" in capsys.readouterr().err
    assert _rows(fresh, "live_grants") == 0
    # The upload run: an unknown grant opens no window, reaches no provider and records nothing.
    code = cli.main(
        [
            "live",
            "upload-assets",
            "--grant-id",
            "no-such-grant",
            "--window-s",
            "900",
            "--actor",
            "op",
        ]
    )
    assert code == 1 and "LIVE_UPLOAD_GRANT_NOT_FOUND" in capsys.readouterr().err
    assert _rows(fresh, "asset_upload_attempts") == 0


# ---------------------------------------------------------------- through the owners of a real unit


@pytest.fixture
def served(
    container: Container,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Container:
    """The commands against the application's own owners.

    The served application already holds its data directory, so the commands lock a directory of
    their own and are handed the served container instead of composing a second one: what they
    call is then exactly the owners the application serves.
    """
    lock = tmp_path_factory.mktemp("live-cli-lock")
    monkeypatch.setenv("ICBM_DATA_DIR", str(lock))
    monkeypatch.setenv("ICBM_SECRET_BACKEND", "memory")
    monkeypatch.setattr("app.container.build_container", lambda *args, **kwargs: container)
    return container


def test_the_eligibility_review_is_recorded_over_the_exact_server_packet(
    api: TestClient,  # noqa: F811
    served: Container,
    config: AppConfig,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    unit = durable_unit(api, served, config)
    code, shown, _ = _run(capsys, "eligibility-packet", "--preparation-id", unit["preparation_id"])
    assert code == 0
    packet = served.canary_eligibility.review_packet(unit["preparation_id"])
    assert shown["digest"] == packet.digest
    assert shown["binding"]["candidate_fingerprint"] == packet.binding.candidate_fingerprint
    checks = tmp_path / "checks.json"
    checks.write_text(json.dumps(proven_checks(packet)))
    # A packet digest that is not the current one refuses with the owner's code.
    code, _, err = _run(
        capsys,
        "record-eligibility",
        "--preparation-id",
        unit["preparation_id"],
        "--packet-digest",
        "f" * 64,
        "--checks",
        str(checks),
        "--actor",
        OPERATOR,
    )
    assert code == 1 and "CANARY_ELIGIBILITY_PACKET_MOVED" in err
    code, recorded, _ = _run(
        capsys,
        "record-eligibility",
        "--preparation-id",
        unit["preparation_id"],
        "--packet-digest",
        packet.digest,
        "--checks",
        str(checks),
        "--actor",
        OPERATOR,
    )
    assert code == 0
    assert recorded["verdict"] == "PROVEN_OUTSIDE"
    assert recorded["review_packet_digest"] == packet.digest
    # The identities stored are the owner's own lineage.
    assert recorded["candidate_fingerprint"] == packet.binding.candidate_fingerprint
    assert recorded["recorded_by"] == OPERATOR


def test_the_grants_are_issued_by_the_authority_and_the_stack_still_refuses(
    api: TestClient,  # noqa: F811
    served: Container,
    config: AppConfig,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    def asset_stage(preparation_id: str) -> None:
        # Before the freeze, as a canary runs: the ASSET grant of the READY candidate.
        preparation = served.registrations.preparation(preparation_id)
        assert preparation is not None
        revision = preparation.current.preparation_revision_id
        gate = PreparationCandidateGate(served.registration_preparations, served.registrations)
        state = gate.current(revision)
        assert state.ready is True
        candidate = served.registration_preparations.stage_candidate(preparation_id)
        artifacts = tmp_path / "artifacts.json"
        artifacts.write_text(
            json.dumps(
                [
                    {
                        "asset_kind": image.asset_kind.value,
                        "sha256": image.sha256,
                        "derivation_id": image.derivation_id,
                    }
                    for item in candidate.resolved.items
                    for image in item.images
                ]
            )
        )
        args = [
            "issue-asset-grant",
            "--marketplace",
            preparation.marketplace_key,
            "--account",
            preparation.marketplace_account_id,
            "--preparation-revision-id",
            revision,
            "--candidate-fingerprint",
            state.fingerprint or "0" * 64,
            "--artifacts",
            str(artifacts),
            "--asset-profile",
            candidate.resolved.target.asset_policy.profile,
            "--budget",
            "1",
            *_window(),
        ]
        # A fingerprint that is not the current candidate's is the owner's refusal.
        wrong = [*args]
        wrong[wrong.index("--candidate-fingerprint") + 1] = "c" * 64
        code, _, err = _run(capsys, *wrong)
        assert code == 1 and "LIVE_GRANT_CANDIDATE_MISMATCH" in err
        code, granted, err = _run(capsys, *args)
        assert code == 0, err
        seen["state"], seen["granted"] = state, granted

    frozen, _ = frozen_unit(api, served, config, before_freeze=asset_stage)
    granted = seen["granted"]
    assert granted["stage"] == "ASSET"
    assert granted["candidate_fingerprint"] == seen["state"].fingerprint
    assert granted["authorization_ref"] == APPROVAL
    # A grant is one layer: the ASSET stage stays blocked, and so does the CREATE stage.
    readiness = served.asset_uploads.readiness(granted["grant_id"])
    assert readiness.verdict.value == "BLOCKED" and readiness.missing
    # A CREATE grant names the sendable Intent the owner resolves itself.
    code, create, err = _run(
        capsys, "issue-create-grant", "--intent-id", frozen.intent.intent_id, *_window()
    )
    assert code == 0, err
    assert create["stage"] == "CREATE"
    assert create["intent_id"] == frozen.intent.intent_id
    assert create["idempotency_key"] == frozen.intent.idempotency_key
    assert create["create_attempt_no"] == 1
    code, shown, _ = _run(capsys, "inspect")
    listed = {grant["grant_id"] for grant in shown["live"]["grants"]}
    assert {granted["grant_id"], create["grant_id"]} <= listed
    assert shown["canary"]["verdict"] == "BLOCKED"
    # With the CREATE grant issued and the brake released through the commands, the production
    # send-time stack still refuses the CREATE under M0 — and spends nothing.
    code, released, err = _run(
        capsys,
        "release-brake",
        "--actor",
        OPERATOR,
        "--reason-code",
        "CANARY_WINDOW",
        "--authorization-ref",
        APPROVAL,
    )
    assert code == 0 and released["state"] == "RELEASED", err
    intent = frozen.intent
    copy = served.registration_preparations.execution_copy(intent.registration_snapshot_id)
    fence = served.safety_stack.truth_fence()
    with served.registrations.transaction() as unit, pytest.raises(MutationRefused) as refused:
        served.safety_stack.admit_create(
            unit.session,
            intent=intent,
            attempt_no=1,
            endpoint_adopted=True,
            reconcile_path_adopted=True,
            scope=unit.execution_scope(
                intent.marketplace_key, intent.marketplace_account_id, CREATE_ENDPOINT_GROUP
            ),
            truth_fence=fence,
            actor=OPERATOR,
            correlation_id="corr-live-cli",
            send_gate=copy.final,
            preparation_revision_id=frozen.preparation_revision_id,
        )
    assert refused.value.code == live_model.MODE_NOT_LIVE
    spent = served.live_authority.grant_record(create["grant_id"])
    assert spent is not None and spent.budget_used == 0
    assert served.registrations.attempts(intent.intent_id) == ()


# ---------------------------------------------------------------- the two local proofs (§7, §8)


def test_the_retention_proof_is_recorded_by_its_owner_and_inspect_reads_it(
    fresh: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, shown, _ = _run(capsys, "inspect")
    assert code == 0 and shown["live"]["proofs"]["evidence_retention_ready"] is False
    code, proved, err = _run(capsys, "prove-retention", "--actor", OPERATOR)
    assert code == 0, err
    # The owner ran the checks on this data directory and recorded its own verdict.
    assert proved["verdict"] == "PASSED" and proved["proof_id"]
    assert _rows(fresh, "retention_proofs") == 1
    code, shown, _ = _run(capsys, "inspect")
    assert shown["live"]["proofs"]["evidence_retention_ready"] is True
    # A proof is one layer: the canary is still blocked.
    assert shown["canary"]["verdict"] == "BLOCKED"


def test_a_drill_refusal_is_the_owners_own_and_records_nothing(
    fresh: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    code = cli.main(
        [
            "live",
            "restore-drill-asset",
            "--grant-id",
            "g-1",
            "--restore-root",
            "relative/root",
            "--actor",
            OPERATOR,
        ]
    )
    assert code == 1 and "DRILL_RESTORE_ROOT_INVALID" in capsys.readouterr().err
    code = cli.main(
        [
            "live",
            "restore-drill-asset",
            "--grant-id",
            "no-such-grant",
            "--restore-root",
            str(tmp_path / "root-a"),
            "--actor",
            OPERATOR,
        ]
    )
    assert code == 1 and "DRILL_GRANT_NOT_FOUND" in capsys.readouterr().err
    code = cli.main(
        [
            "live",
            "restore-drill-create",
            "--intent-id",
            "no-such-intent",
            "--restore-root",
            str(tmp_path / "root-c"),
            "--actor",
            OPERATOR,
        ]
    )
    assert code == 1 and "DRILL_INTENT_NOT_FOUND" in capsys.readouterr().err
    assert _rows(fresh, "restore_drills") == 0


def test_the_create_drill_runs_through_the_drill_owner_for_one_intent(
    api: TestClient,  # noqa: F811
    served: Container,
    config: AppConfig,
    capsys: pytest.CaptureFixture[str],
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    frozen, _ = frozen_unit(api, served, config)
    root = tmp_path_factory.mktemp("restore-cli-create")
    code, drilled, err = _run(
        capsys,
        "restore-drill-create",
        "--intent-id",
        frozen.intent.intent_id,
        "--restore-root",
        str(root),
        "--actor",
        OPERATOR,
    )
    assert code == 0, err
    assert (drilled["verdict"], drilled["failure_code"]) == ("PASSED", None)
    # The proof is the owner's, for exactly the target it computed.
    stage_proofs = proofs(served)
    assert stage_proofs.restore_proof(MutationStage.CREATE, drilled["target_digest"])
    assert not stage_proofs.restore_proof(MutationStage.ASSET, drilled["target_digest"])
    # The same root again is not fresh: the owner refuses and records nothing more.
    code, _, err = _run(
        capsys,
        "restore-drill-create",
        "--intent-id",
        frozen.intent.intent_id,
        "--restore-root",
        str(root),
        "--actor",
        OPERATOR,
    )
    assert code == 1 and "DRILL_RESTORE_ROOT_INVALID" in err


# ---------------------------------------------------------------- residual-risk acceptance (§6.1)

USER_ACCEPTANCE = "github_issue_comment:5950000001@" + "a" * 64
ARCHITECT_ACCEPTANCE = "github_issue_comment:5950000002@" + "b" * 64


def test_the_residual_risk_acceptance_proof_is_recorded_by_its_command(
    served: Container, config: AppConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.capabilities.live_safety.residual_risk import (
        CONTRACT_NOT_CURRENT,
        RESIDUAL_RISK_CONTRACT_VERSION,
    )
    from tests.support.register_support import MARKET, establish

    account = establish(served, config, MARKET, "uid-market-a-1")
    base = ["record-residual-risk-acceptance", "--marketplace-key", MARKET, "--account", account]
    tail = ["--user-acceptance", USER_ACCEPTANCE, "--architect-acceptance", ARCHITECT_ACCEPTANCE]
    # A contract that is not the current one is refused with the owner's code; nothing recorded.
    code, _, err = _run(
        capsys,
        *base,
        "--risk-contract",
        "adr-0018-6.1-residual-risk/v0",
        *tail,
        "--actor",
        OPERATOR,
    )
    assert code == 1 and CONTRACT_NOT_CURRENT in err
    assert served.residual_risk.accepted(MARKET, account) is False
    code, shown, _ = _run(
        capsys, *base, "--risk-contract", RESIDUAL_RISK_CONTRACT_VERSION, *tail, "--actor", OPERATOR
    )
    assert code == 0
    assert shown["seq"] == 1 and shown["marketplace_account_id"] == account
    assert shown["user_acceptance"]["comment_id"] == "5950000001"
    assert shown["architect_acceptance"]["comment_id"] == "5950000002"
    assert served.residual_risk.accepted(MARKET, account) is True


def test_an_unsupported_acceptance_form_is_refused_and_records_nothing(
    fresh: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.capabilities.live_safety.residual_risk import (
        EVIDENCE_UNSUPPORTED,
        RESIDUAL_RISK_CONTRACT_VERSION,
    )

    code, _, err = _run(
        capsys,
        "record-residual-risk-acceptance",
        "--marketplace-key",
        "smartstore",
        "--account",
        "acct-1",
        "--risk-contract",
        RESIDUAL_RISK_CONTRACT_VERSION,
        "--user-acceptance",
        "https://github.com/o/r/issues/89#issuecomment-5950000001",
        "--architect-acceptance",
        ARCHITECT_ACCEPTANCE,
        "--actor",
        OPERATOR,
    )
    assert code == 1 and EVIDENCE_UNSUPPORTED in err
    assert _rows(fresh, "residual_risk_acceptances") == 0
