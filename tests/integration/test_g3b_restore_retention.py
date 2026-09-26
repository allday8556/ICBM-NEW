"""Gate 3 area 2 (ADR-0018 §7, §8): the restore drill and the evidence-retention proof.

Provider-zero. The unit under test is built through the application's own durable owners — a
durable target policy, reviewed category metadata, a collected and materialized M4 Item with its
image selection and QA, a Draft and an authored preparation — so every element a drill compares
is a real owner row. Every drill restores into a separate fresh root under ``tmp_path``.
"""

import sqlite3
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.core.errors import InputValidationError
from app.db.migrate import head_revision
from app.live import model as live_model
from app.live.gates import create_stage_gate
from app.live.model import MutationStage, ProofVerdict
from app.live.proofs import DurableStageProofs
from app.live.retention import RetentionProofService
from app.live.stack import SafetyStack, StageGate
from app.live.store import ArtifactRef, LiveAuthorityStore
from app.main import create_app
from app.register.execution import CREATE_ENDPOINT_GROUP
from app.register.model import ListingShape
from tests.conftest import LOCAL
from tests.gate1_support import CLIENT, MARKET, OPERATOR, record_reviewed_metadata, save_policy
from tests.integration.test_g2c_review_paths import authored_inputs
from tests.live_support import PermittedMode, ProvenProofs
from tests.product_support import Collections, count, product
from tests.register_support import establish, no_match, prepared, select_and_pass

pytestmark = pytest.mark.integration

APPROVAL = "5841516679"
CID = "corr-g3b"
PREPARATIONS = "/api/v1/register/preparations"


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def fresh(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A fresh restore root outside the active data root (which is the test's ``tmp_path``)."""

    def make(name: str) -> Path:
        return tmp_path_factory.mktemp(f"restore-{name}")

    return make


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


def durable_unit(api: TestClient, container: Container, config: AppConfig) -> dict[str, str]:
    """One authored preparation of one priced, image-selected M4 Item under durable owners."""
    account = establish(container, config, MARKET, f"provider-{uuid.uuid4().hex[:8]}")
    save_policy(api, account)
    record_reviewed_metadata(api)
    run_id, revision = Collections.of(container, config).collect(
        product(), source_product_id="1234"
    )
    result = container.materializer.materialize_run(run_id)
    assert result.item_id is not None
    select_and_pass(container, result.item_id, revision)
    policy = container.registration_preflight.target_policy(MARKET, account)
    assert policy is not None
    pin = container.pricing.price(result.item_id, policy.pricing_context).snapshot
    assert pin is not None
    with container.registrations.transaction() as unit:
        draft = unit.create_draft(
            MARKET, account, ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR, correlation_id=CID,
        )  # fmt: skip
        unit.add_draft_item(
            draft.draft_id, result.item_id, pin.pricing_snapshot_id,
            added_by=OPERATOR, correlation_id=CID,
        )  # fmt: skip
    response = api.post(
        PREPARATIONS,
        json={
            "draft_id": draft.draft_id,
            "item_ids": [result.item_id],
            "actor": OPERATOR,
            "inputs": authored_inputs(),
        },
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    return {
        "account": account,
        "draft_id": draft.draft_id,
        "item_id": result.item_id,
        "preparation_id": response.json()["preparation_id"],
    }


def live(container: Container) -> LiveAuthorityStore:
    return LiveAuthorityStore(container.db, container.clock, container.audit)


def asset_grant(container: Container, unit: dict[str, str]) -> tuple[str, Any]:
    """An ASSET grant bound to the unit's current candidate, recorded directly by the owner."""
    candidate = container.registration_preparations.evaluate(unit["preparation_id"])
    preparation = container.registrations.preparation(unit["preparation_id"])
    assert preparation is not None
    artifacts = [
        ArtifactRef(image.asset_kind, image.sha256, image.derivation_id)
        for item in candidate.resolved.items
        for image in item.images
    ]
    assert artifacts, "the unit selected no image"
    now = container.clock.now()
    with live(container).transaction() as store:
        grant = store.issue_asset_grant(
            marketplace_key=MARKET,
            marketplace_account_id=unit["account"],
            preparation_revision_id=preparation.current.preparation_revision_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            artifacts=artifacts,
            asset_profile=candidate.resolved.target.asset_policy.profile,
            budget=2,
            not_before=now,
            expires_at=now + timedelta(hours=1),
            approved_by=OPERATOR,
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )
    return grant.grant_id, candidate


def proofs(container: Container) -> DurableStageProofs:
    return DurableStageProofs(
        store=live(container), retention=container.retention, schema_head=head_revision
    )


def rows_outside_the_proof_owners(config: AppConfig) -> dict[str, int]:
    with sqlite3.connect(config.data_dir / "runtime" / "icbm.db") as raw:
        tables = [
            r[0]
            for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT IN ('restore_drills', 'audit_events', 'alembic_version')"
                " AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


# ---------------------------------------------------------------- the ASSET drill


def test_an_asset_drill_restores_the_exact_pre_upload_chain_into_a_fresh_root(
    api: TestClient, container: Container, config: AppConfig, fresh: Any
) -> None:
    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    before = rows_outside_the_proof_owners(config)
    root = fresh("asset")
    result = container.restore_drills.drill_asset(
        grant_id, restore_root=root, actor=OPERATOR, correlation_id=CID
    )
    assert (result.verdict, result.failure_code) == (ProofVerdict.PASSED, None), result.evidence
    evidence = result.evidence
    assert evidence["integrity"] == "ok" and evidence["schema_head"] == head_revision()
    by_name = {record["element"]: record for record in evidence["elements"]}
    # Identity and state of every element, source and restored, match.
    assert all(record["match"] for record in evidence["elements"])
    for required in ("canonical_account", "draft", "preparation_revision", "grant",
                     "target_policy_revision", "category_metadata_revision"):  # fmt: skip
        assert by_name[required]["state"] == "PRESENT", required
    # What cannot exist before the upload is recorded as absent, never created.
    assert by_name["snapshot_from_revision"]["state"] == "ABSENT"
    scopes = [name for name in by_name if name.startswith("replay_scope:")]
    assert scopes and all(by_name[name]["state"] == "ABSENT" for name in scopes)
    assert "snapshot_from_revision" in evidence["absent"]
    assert evidence["artifacts"] and all(a["match"] for a in evidence["artifacts"])
    # The restore is a separate root with the backup and the artifacts; the active root changed
    # only by the drill's own record and its audit event.
    assert (root / "runtime" / "icbm.db").is_file()
    assert rows_outside_the_proof_owners(config) == before
    # It is a restore proof for exactly this stage and target, and for nothing else.
    stage_proofs = proofs(container)
    assert stage_proofs.restore_proof(MutationStage.ASSET, result.target_digest)
    assert not stage_proofs.restore_proof(MutationStage.CREATE, result.target_digest)
    assert container.asset_uploads.restore_target(grant_id) == result.target_digest
    # A drill proves its own schema head only.
    other_head = DurableStageProofs(
        store=live(container), retention=container.retention, schema_head=lambda: "other-head"
    )
    assert not other_head.restore_proof(MutationStage.ASSET, result.target_digest)
    # The drill record is append-only.
    with sqlite3.connect(config.data_dir / "runtime" / "icbm.db") as raw:
        for statement in (
            "UPDATE restore_drills SET verdict = 'FAILED', failure_code = 'X'",
            "DELETE FROM restore_drills",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)


def test_an_asset_restore_proof_is_stale_once_the_proved_state_moves(
    api: TestClient, container: Container, config: AppConfig, fresh: Any
) -> None:
    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    result = container.restore_drills.drill_asset(
        grant_id, restore_root=fresh("r1"), actor=OPERATOR, correlation_id=CID
    )
    assert result.verdict is ProofVerdict.PASSED, result.evidence
    # A new authored revision moves the candidate and the preparation: a new target, no proof.
    with container.registrations.transaction() as store:
        record = container.registrations.preparation(unit["preparation_id"])
        assert record is not None
        from app.register.authoring import decode_inputs, encode_inputs

        store.revise_preparation(
            unit["preparation_id"],
            item_ids=list(record.current.item_ids),
            inputs=encode_inputs(decode_inputs(record.current)),
            authored_by=OPERATOR,
            correlation_id=CID,
        )
    now = container.asset_uploads.restore_target(grant_id)
    assert now is not None and now != result.target_digest
    assert not proofs(container).restore_proof(MutationStage.ASSET, now)


@pytest.mark.parametrize("where", ["inside-active-root", "the-active-root", "not-fresh"])
def test_a_drill_never_restores_over_or_inside_the_active_root_or_into_a_used_root(
    api: TestClient, container: Container, config: AppConfig, fresh: Any, where: str
) -> None:
    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    roots = {
        "inside-active-root": config.data_dir / "backups" / "drill",
        "the-active-root": config.data_dir,
        "not-fresh": fresh("used"),
    }
    (roots["not-fresh"] / "left-over.txt").write_text("x")
    with pytest.raises(InputValidationError) as refused:
        container.restore_drills.drill_asset(
            grant_id, restore_root=roots[where], actor=OPERATOR, correlation_id=CID
        )
    assert refused.value.code == "DRILL_RESTORE_ROOT_INVALID"
    assert count(config, "restore_drills") == 0


def test_a_source_that_moves_during_the_drill_fails_it(
    api: TestClient,
    container: Container,
    config: AppConfig,
    fresh: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    evaluate = container.registration_preparations.evaluate

    def evaluate_then_write(preparation_id: str, **kwargs: Any) -> Any:
        result = evaluate(preparation_id, **kwargs)
        container.live_authority.engage_brake(
            actor=OPERATOR, reason_code="CONCURRENT_WRITE", correlation_id=CID
        )
        return result

    monkeypatch.setattr(container.registration_preparations, "evaluate", evaluate_then_write)
    result = container.restore_drills.drill_asset(
        grant_id, restore_root=fresh("moved"), actor=OPERATOR, correlation_id=CID
    )
    assert (result.verdict, result.failure_code) == (
        ProofVerdict.FAILED,
        live_model.DRILL_SOURCE_MOVED,
    )
    assert not proofs(container).restore_proof(MutationStage.ASSET, result.target_digest)


def test_a_lost_artifact_or_a_missing_required_element_fails_the_drill(
    api: TestClient,
    container: Container,
    config: AppConfig,
    fresh: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = durable_unit(api, container, config)
    grant_id, candidate = asset_grant(container, unit)
    image = candidate.resolved.items[0].images[0]
    folder = "source-assets" if image.asset_kind.value == "SOURCE_ASSET" else "derived-images"
    stored = config.data_dir / folder / "sha256" / image.sha256[:2] / image.sha256
    original = stored.read_bytes()
    stored.unlink()
    try:
        lost = container.restore_drills.drill_asset(
            grant_id, restore_root=fresh("lost"), actor=OPERATOR, correlation_id=CID
        )
    finally:
        stored.write_bytes(original)
    assert lost.failure_code == live_model.DRILL_ARTIFACT_MISMATCH
    from app.live import drill as drill_module

    real = drill_module.unit_elements

    def with_a_bogus_required_element(*args: Any) -> Any:
        return [
            *real(*args),
            drill_module.Element("bogus", "product_items", (("item_id", "no-such-item"),)),
        ]

    monkeypatch.setattr(drill_module, "unit_elements", with_a_bogus_required_element)
    absent = container.restore_drills.drill_asset(
        grant_id, restore_root=fresh("absent"), actor=OPERATOR, correlation_id=CID
    )
    assert absent.failure_code == live_model.DRILL_REQUIRED_ELEMENT_ABSENT


# ---------------------------------------------------------------- the CREATE drill


def frozen_unit(api: TestClient, container: Container, config: AppConfig) -> tuple[Any, dict]:
    """One frozen unit with its Intent, authored through the application's own preparation owner.

    No durable policy can reach READY at this main (its authoring revisions have no owner), so the
    served preflight reads the test's static sources — pointed at **real durable revision rows**,
    so the policy and metadata revisions a CREATE drill proves are owner rows, not labels.
    """
    from app.register.category_metadata import CategoryMetadataStore
    from app.register.policy import StaticRegistrationMetadata, StaticRegistrationPolicy
    from app.register.target_policy import TargetPolicyStore
    from tests.integration.test_m5_register_api import _inputs
    from tests.register_support import MARKET as UNIT_MARKET
    from tests.register_support import (
        FakeCapability,
        FakeDuplicateLookup,
        draft,
        metadata,
        ready_item,
        target,
    )

    account = establish(container, config, UNIT_MARKET, "uid-market-a-1")
    policy = TargetPolicyStore(container.db, container.clock, container.audit).append(
        UNIT_MARKET, account,
        {"marketplace_key": UNIT_MARKET, "marketplace_account_id": account, "source": "g3b"},
        expected_current_revision=None, authored_by=OPERATOR, correlation_id=CID,
    )  # fmt: skip
    reviewed = metadata()
    category = CategoryMetadataStore(container.db, container.clock, container.audit).append(
        UNIT_MARKET, reviewed.taxonomy_revision, reviewed.category_id,
        {
            "marketplace_key": UNIT_MARKET,
            "taxonomy_revision": reviewed.taxonomy_revision,
            "category_id": reviewed.category_id,
            "content_provenance": "OPERATOR_CONFIRMED",
        },
        reviewed=True,
        expected_current_revision=None, recorded_by=OPERATOR, correlation_id=CID,
    )  # fmt: skip
    served = container.registration_preflight
    served._policies = StaticRegistrationPolicy(
        (target(account, policy_revision=policy.policy_revision),)
    )
    served._metadata = StaticRegistrationMetadata(
        (metadata(metadata_revision=category.metadata_revision),), marketplace_key=UNIT_MARKET
    )
    served._capability = FakeCapability()
    # The provider-neutral lookup the first CREATE copy reads (no provider lookup is adopted).
    container.registration_preparations._duplicate_lookup = FakeDuplicateLookup()
    item = ready_item(container, Collections.of(container, config), "1234")
    draft_id = draft(container.registrations, account, [item])
    created = api.post(
        PREPARATIONS,
        json={"draft_id": draft_id, "item_ids": [item.item_id], "actor": OPERATOR,
              "inputs": _inputs(item)},
        headers=CLIENT,
    )  # fmt: skip
    assert created.status_code == 200, created.text
    preparation_id = created.json()["preparation_id"]
    authoring = container.registration_preparations
    evidence = no_match(authoring.evaluate(preparation_id))
    ready = authoring.evaluate(preparation_id, duplicate_evidence=evidence)
    assert ready.status.value == "READY", ready.codes
    frozen = authoring.freeze(
        preparation_id, actor=OPERATOR, duplicate_evidence=evidence,
        prepared_assets=prepared(ready),
    )  # fmt: skip
    return frozen, {
        "account": account,
        "draft_id": draft_id,
        "preparation_id": preparation_id,
        "market": UNIT_MARKET,
    }


def test_a_create_drill_proves_the_register_chain_after_the_freeze(
    api: TestClient, container: Container, config: AppConfig, fresh: Any
) -> None:
    frozen, unit = frozen_unit(api, container, config)
    intent_id = frozen.intent.intent_id
    result = container.restore_drills.drill_create(
        intent_id, restore_root=fresh("create"), actor=OPERATOR, correlation_id=CID
    )
    assert (result.verdict, result.failure_code) == (ProofVerdict.PASSED, None), result.evidence
    by_name = {record["element"]: record for record in result.evidence["elements"]}
    # The Snapshot and the Intent are never absent in a CREATE proof.
    for present in ("snapshot", "item_snapshots", "snapshot_provenance", "batch", "intent"):
        assert by_name[present]["state"] == "PRESENT", present
    # Only what cannot exist yet before the CREATE is absent.
    assert by_name["attempts"]["state"] == "ABSENT"
    assert by_name["registration"]["state"] == "ABSENT"
    assert result.evidence["scope_state"] == "ACTIVE"
    stage_proofs = proofs(container)
    assert stage_proofs.restore_proof(MutationStage.CREATE, result.target_digest)
    assert not stage_proofs.restore_proof(MutationStage.ASSET, result.target_digest)
    # After the freeze, an ASSET drill of the same revision is no longer the ASSET stage.
    preparation = container.registrations.preparation(unit["preparation_id"])
    assert preparation is not None
    now = container.clock.now()
    with live(container).transaction() as store:
        candidate = container.registration_preparations.evaluate(unit["preparation_id"])
        late = store.issue_asset_grant(
            marketplace_key=unit["market"],
            marketplace_account_id=unit["account"],
            preparation_revision_id=preparation.current.preparation_revision_id,
            candidate_fingerprint=candidate.candidate_fingerprint,
            artifacts=[
                ArtifactRef(i.asset_kind, i.sha256, i.derivation_id)
                for item in candidate.resolved.items
                for i in item.images
            ],
            asset_profile=candidate.resolved.target.asset_policy.profile,
            budget=1,
            not_before=now,
            expires_at=now + timedelta(hours=1),
            approved_by=OPERATOR,
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )
    # This unit's marketplace is a test one: give the server-owned host rule its canonical host.
    from app.live.model import WireHostPolicy

    container.asset_uploads._hosts = WireHostPolicy(
        {unit["market"]: "api.commerce.naver.com", MARKET: "api.commerce.naver.com"}
    )
    stale = container.restore_drills.drill_asset(
        late.grant_id, restore_root=fresh("late"), actor=OPERATOR, correlation_id=CID
    )
    assert stale.failure_code == live_model.DRILL_STAGE_MISMATCH


def test_a_scope_change_stales_the_create_restore_proof(
    api: TestClient, container: Container, config: AppConfig, fresh: Any
) -> None:
    frozen, unit = frozen_unit(api, container, config)
    result = container.restore_drills.drill_create(
        frozen.intent.intent_id, restore_root=fresh("c"), actor=OPERATOR, correlation_id=CID
    )
    assert result.verdict is ProofVerdict.PASSED, result.evidence
    from app.core.errors import ErrorClass
    from app.register.model import OPERATOR_RESUMABLE, ScopePauseReason

    with container.registrations.transaction() as store:
        store.pause_scope(
            unit["market"], unit["account"], CREATE_ENDPOINT_GROUP,
            reason=ScopePauseReason.POLICY, policy_version="registration-execution-policy/v1",
            error_class=ErrorClass.POLICY_BLOCKED, actor=OPERATOR, correlation_id=CID,
        )  # fmt: skip
        store.resume_scope(
            unit["market"], unit["account"], CREATE_ENDPOINT_GROUP,
            actor=OPERATOR, reason="OPERATOR_REVIEWED", correlation_id=CID,
            allowed_reasons=OPERATOR_RESUMABLE,
        )  # fmt: skip
    intent = container.registrations.intent(frozen.intent.intent_id)
    assert intent is not None
    now = container.safety_stack.create_restore_target(
        intent,
        attempt_no=1,
        scope=container.registrations.execution_scope(
            MARKET, unit["account"], CREATE_ENDPOINT_GROUP
        ),
    )
    assert now != result.target_digest
    assert not proofs(container).restore_proof(MutationStage.CREATE, now)


# ---------------------------------------------------------------- the CREATE stage gate


def test_create_readiness_carries_the_stage_gate_the_owners_derive(
    api: TestClient, container: Container, config: AppConfig
) -> None:
    frozen, unit = frozen_unit(api, container, config)
    intent = container.registrations.intent(frozen.intent.intent_id)
    assert intent is not None
    gate = create_stage_gate(
        registrations=container.registrations,
        preparations=container.registration_preparations,
        intent=intent,
    )
    assert gate == StageGate(ready=True)
    stack = SafetyStack(
        store=live(container), mode=PermittedMode(), proofs=ProvenProofs(), clock=container.clock
    )
    scope = container.registrations.execution_scope(
        unit["market"], unit["account"], CREATE_ENDPOINT_GROUP
    )
    held = stack.create_readiness(
        intent,
        attempt_no=1,
        endpoint_adopted=True,
        scope=scope,
        stage_gate=StageGate(ready=False, reasons=("REGISTER_SEND_PREFLIGHT_NOT_READY",)),
    )
    assert live_model.CREATE_STAGE_GATE_NOT_READY in held.missing
    # The Draft moves: the execution copy of the frozen revision no longer matches the Snapshot.
    run_id, revision = Collections.of(container, config).collect(
        product(), source_product_id="7777"
    )
    other = container.materializer.materialize_run(run_id)
    assert other.item_id is not None
    select_and_pass(container, other.item_id, revision)
    policy = container.registration_preflight.target_policy(unit["market"], unit["account"])
    assert policy is not None
    pin = container.pricing.price(other.item_id, policy.pricing_context).snapshot
    assert pin is not None
    with container.registrations.transaction() as store:
        store.add_draft_item(
            unit["draft_id"], other.item_id, pin.pricing_snapshot_id,
            added_by=OPERATOR, correlation_id=CID,
        )  # fmt: skip
    moved = create_stage_gate(
        registrations=container.registrations,
        preparations=container.registration_preparations,
        intent=intent,
    )
    assert not moved.ready and moved.reasons


# ---------------------------------------------------------------- evidence retention (§8)


def test_evidence_retention_is_proven_from_the_live_checks_and_fails_closed(
    api: TestClient, container: Container, config: AppConfig
) -> None:
    retention = container.retention
    assert not retention.ready()  # nothing recorded yet: never asserted
    proof_id, verdict = retention.prove(actor=OPERATOR, correlation_id=CID)
    assert verdict is ProofVerdict.PASSED and retention.ready()
    checks = retention.checks().checks
    assert all(checks["no_delete_triggers"].values())
    assert checks["deleting_job_types"] == [] and checks["automatic_deletion_authorized"] is False
    # A deleting job type makes the checks fail: no automatic deletion is authorized.
    purging = RetentionProofService(
        db=container.db,
        store=live(container),
        job_types=lambda: ["register.create", "evidence.purge"],
        safe_retention_profile_version="smartstore-safe-retention/v1",
        schema_head=head_revision,
    )
    assert not purging.checks().passed and not purging.ready()
    # Checks that still pass but differ from what was proven are not proven: a new profile version.
    moved = RetentionProofService(
        db=container.db,
        store=live(container),
        job_types=lambda: ["register.create"],
        safe_retention_profile_version="smartstore-safe-retention/v2",
        schema_head=head_revision,
    )
    assert moved.checks().passed and not moved.ready()
    # A lost delete guard fails the live checks: the earlier proof is no longer current.
    with sqlite3.connect(config.data_dir / "runtime" / "icbm.db") as raw:
        raw.execute("DROP TRIGGER trg_registration_intents_no_delete")
    assert not retention.checks().passed and not retention.ready()
    _, failed = retention.prove(actor=OPERATOR, correlation_id=CID)
    assert failed is ProofVerdict.FAILED
    with sqlite3.connect(config.data_dir / "runtime" / "icbm.db") as raw:
        for statement in (
            "UPDATE retention_proofs SET verdict = 'PASSED'",
            "DELETE FROM retention_proofs",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)
    assert proof_id


def test_the_production_stack_now_reads_the_durable_proofs_but_stays_blocked(
    api: TestClient, container: Container, config: AppConfig, fresh: Any
) -> None:
    # Restore and retention can now be proven, yet M0, eligibility and visual still refuse.
    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    container.retention.prove(actor=OPERATOR, correlation_id=CID)
    container.restore_drills.drill_asset(
        grant_id, restore_root=fresh("p"), actor=OPERATOR, correlation_id=CID
    )
    readiness = container.asset_uploads.readiness(grant_id)
    missing = set(readiness.missing)
    assert live_model.RESTORE_PROOF_ABSENT not in missing
    assert live_model.RETENTION_UNPROVEN not in missing
    assert {
        live_model.MODE_NOT_LIVE,
        live_model.ELIGIBILITY_UNPROVEN,
        live_model.VISUAL_UNRECORDED,
        live_model.SENDER_NOT_WIRED,
    } <= missing


def test_a_restore_that_changes_an_element_fails_the_drill(
    api: TestClient,
    container: Container,
    config: AppConfig,
    fresh: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    from app.live import drill as drill_module

    backup = drill_module._backup_into_fresh_root

    def backup_then_tamper(source: sqlite3.Connection, restored_db: Path) -> None:
        backup(source, restored_db)
        with sqlite3.connect(restored_db) as restored:
            for (name,) in restored.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                " AND tbl_name = 'registration_drafts'"
            ).fetchall():
                restored.execute(f"DROP TRIGGER {name}")
            restored.execute("UPDATE registration_drafts SET draft_revision = draft_revision + 9")

    monkeypatch.setattr(drill_module, "_backup_into_fresh_root", backup_then_tamper)
    result = container.restore_drills.drill_asset(
        grant_id, restore_root=fresh("tampered"), actor=OPERATOR, correlation_id=CID
    )
    assert result.failure_code == live_model.DRILL_ELEMENT_MISMATCH
    by_name = {record["element"]: record for record in result.evidence["elements"]}
    assert by_name["draft"]["match"] is False
    assert not proofs(container).restore_proof(MutationStage.ASSET, result.target_digest)
