"""The application freeze path uses the exact current inputs the owners hold (Issue #89 architect
follow-up 5919917893 §3).

``POST /api/v1/register/preparations/{id}/freeze`` freezes the unit the preparation authored. Its
final preflight needs two inputs the application already owns:

- when the target policy requires duplicate proof, the admissible evidence of the
  provider-neutral duplicate-evidence owner seam — the same evidence the ASSET stage and the first
  CREATE copy consume (D4);
- when the policy needs provider asset identities, the provider assets the durable ASSET
  upload-attempt owner holds as ``APPLIED_PROVEN`` for exactly this preparation revision and this
  candidate fingerprint.

The ASSET stage runs for real here — grant, permitted stack, a scripted sender that applies — so
the freeze consumes what that stage produced, and ASSET and CREATE bind one candidate. Missing
evidence, missing assets and a preparation revised after the upload each refuse, freeze nothing
and open no Intent. Nothing reaches a provider.
"""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.live_safety.assets import (
    AssetUploadRequest,
    AssetUploadService,
    PreparationCandidateGate,
    UploadSendResult,
)
from app.capabilities.live_safety.model import UploadAttemptState, WireHostPolicy
from app.capabilities.live_safety.stack import SafetyStack
from app.capabilities.live_safety.store import ArtifactRef, LiveAuthorityStore
from app.config import AppConfig
from app.container import Container
from app.stages.register.authoring import decode_inputs, encode_inputs
from app.stages.register.model import RegistrationConflictError
from tests.integration.live_safety.test_g3a_live_create import _bytes_of
from tests.integration.live_safety.test_g3b_restore_retention import (  # noqa: F401 - fixtures
    PREPARATIONS,
    api,
    container,
    frozen_unit,
)
from tests.support.gate1_support import CLIENT, OPERATOR
from tests.support.live_safety_support import PermittedMode, ProvenProofs, ScriptedSender
from tests.support.product_support import count
from tests.support.register_support import MARKET, FakeDuplicateLookup

pytestmark = pytest.mark.integration

CID = "corr-freeze-inputs"
# A synthetic approval reference of the owners' comment-id form; no real approval is implied.
APPROVAL = "1000000001"
HOSTS = WireHostPolicy({MARKET: "api.commerce.naver.com"})


class _Authored(Exception):
    """Stops the shared fixture once the unit is authored, before anything is frozen."""


def _authored(api: TestClient, container: Container, config: AppConfig) -> str:  # noqa: F811
    """One authored, READY-able preparation under a duplicate-proof policy, not frozen."""
    seen: dict[str, str] = {}

    def stop(preparation_id: str) -> None:
        seen["id"] = preparation_id
        raise _Authored

    with pytest.raises(_Authored):
        frozen_unit(api, container, config, duplicate_proof=True, before_freeze=stop)
    return seen["id"]


def _selected(candidate: Any) -> list[ArtifactRef]:
    return sorted(
        {
            ArtifactRef(image.asset_kind, image.sha256, image.derivation_id)
            for item in candidate.resolved.items
            for image in item.images
        },
        key=lambda artifact: artifact.sha256,
    )


def _asset_stage(container: Container, preparation_id: str) -> dict[tuple[str, str], str]:  # noqa: F811
    """The real ASSET stage: the grant on the stage candidate, then one applied upload per
    selected artifact through the upload owner. Returns the provider reference per artifact."""
    candidate = container.registration_preparations.stage_candidate(preparation_id)
    preparation = container.registrations.preparation(preparation_id)
    assert preparation is not None
    selected = _selected(candidate)
    assert selected, "the unit selected no image"
    now = container.clock.now()
    grant = container.live_authority.issue_asset_grant(
        marketplace_key=preparation.marketplace_key,
        marketplace_account_id=preparation.marketplace_account_id,
        preparation_revision_id=preparation.current.preparation_revision_id,
        candidate_fingerprint=candidate.candidate_fingerprint,
        artifacts=selected,
        asset_profile=candidate.resolved.target.asset_policy.profile,
        budget=len(selected),
        not_before=now,
        expires_at=now + timedelta(hours=1),
        approved_by=OPERATOR,
        authorization_ref=APPROVAL,
        correlation_id=CID,
    )
    store = LiveAuthorityStore(container.db, container.clock, container.audit)
    with store.transaction() as unit:
        unit.release(
            actor=OPERATOR,
            reason_code="CANARY_WINDOW",
            authorization_ref=APPROVAL,
            correlation_id=CID,
        )
    references = {
        (artifact.sha256, artifact.derivation_id or ""): (
            f"https://shop-phinf.pstatic.net/20261001_1/{artifact.sha256[:12]}.png"
        )
        for artifact in selected
    }
    sender = ScriptedSender(
        script=[
            UploadSendResult(
                UploadAttemptState.APPLIED_PROVEN,
                provider_asset_ref=references[(artifact.sha256, artifact.derivation_id or "")],
            )
            for artifact in selected
        ]
    )
    uploads = AssetUploadService(
        store=store,
        stack=SafetyStack(
            store=store, mode=PermittedMode(), proofs=ProvenProofs(), clock=container.clock
        ),
        sender=sender,
        hosts=HOSTS,
        candidates=PreparationCandidateGate(
            container.registration_preparations, container.registrations
        ),
        clock=container.clock,
    )
    for artifact in selected:
        result = uploads.upload(
            AssetUploadRequest(
                grant_id=grant.grant_id,
                artifact=artifact,
                content=_bytes_of(container, artifact),
                file_name=f"{artifact.sha256[:8]}.png",
                media_type="image/png",
                actor=OPERATOR,
                correlation_id=CID,
            )
        )
        assert result.attempt.state is UploadAttemptState.APPLIED_PROVEN
    return references


def _freeze(api: TestClient, preparation_id: str) -> Any:  # noqa: F811
    return api.post(
        f"{PREPARATIONS}/{preparation_id}/freeze", json={"actor": OPERATOR}, headers=CLIENT
    )


def test_the_application_freeze_consumes_the_owners_evidence_and_prepared_assets(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    preparation_id = _authored(api, container, config)
    references = _asset_stage(container, preparation_id)
    stage = container.registration_preparations.stage_candidate(preparation_id)
    response = _freeze(api, preparation_id)
    assert response.status_code == 200, response.text
    frozen = response.json()
    assert frozen["intent_state"] == "PREPARED"
    # The Snapshot was frozen on the candidate the ASSET stage bound, with exactly the provider
    # assets the upload owner holds for it: the first CREATE copy reproduces it from the owners.
    copy = container.registration_preparations.execution_copy(frozen["registration_snapshot_id"])
    assert copy.final.candidate_fingerprint == stage.candidate_fingerprint
    assert copy.request.duplicate_evidence is not None
    assert {
        (asset.sha256, asset.derivation_id or ""): asset.provider_asset_ref
        for asset in copy.final.prepared_assets
    } == references
    assert all(
        asset.candidate_fingerprint == stage.candidate_fingerprint
        for asset in copy.final.prepared_assets
    )


def test_a_freeze_without_prepared_assets_freezes_nothing(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    preparation_id = _authored(api, container, config)
    response = _freeze(api, preparation_id)
    # The final preflight has no provider asset for the selected artifacts: not READY.
    assert response.status_code == 422
    assert "REGISTER_PREFLIGHT_NOT_READY" in response.text
    assert count(container.config, "registration_snapshots") == 0
    assert count(container.config, "registration_intents") == 0


def test_a_freeze_without_admissible_duplicate_evidence_freezes_nothing(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    preparation_id = _authored(api, container, config)
    _asset_stage(container, preparation_id)
    authoring = container.registration_preparations
    for unavailable in (
        FakeDuplicateLookup(available_result=False),
        FakeDuplicateLookup(return_none=True),
    ):
        authoring._duplicate_lookup = unavailable
        # The stage candidate cannot be built; the final preflight names the missing evidence.
        with pytest.raises(RegistrationConflictError) as refused:
            authoring.stage_candidate(preparation_id)
        assert refused.value.code == "REGISTER_DUPLICATE_EVIDENCE_UNAVAILABLE"
        response = _freeze(api, preparation_id)
        assert response.status_code == 422
        assert "REGISTER_PREFLIGHT_NOT_READY" in response.text
    assert count(container.config, "registration_snapshots") == 0


def test_assets_prepared_for_an_earlier_revision_never_freeze_a_later_one(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    preparation_id = _authored(api, container, config)
    _asset_stage(container, preparation_id)
    preparation = container.registrations.preparation(preparation_id)
    assert preparation is not None
    with container.registrations.transaction() as unit:
        unit.revise_preparation(
            preparation_id,
            item_ids=list(preparation.current.item_ids),
            inputs=encode_inputs(decode_inputs(preparation.current)),
            authored_by=OPERATOR,
            correlation_id=CID,
        )
    # The applied uploads belong to the earlier revision: none is taken for this one.
    revised = container.registrations.preparation(preparation_id)
    assert revised is not None
    stage = container.registration_preparations.stage_candidate(preparation_id)
    assert (
        container.registration_preparations._prepared_assets.prepared_assets(
            marketplace_key=revised.marketplace_key,
            marketplace_account_id=revised.marketplace_account_id,
            preparation_revision_id=revised.current.preparation_revision_id,
            candidate_fingerprint=stage.candidate_fingerprint,
        )
        == ()
    )
    response = _freeze(api, preparation_id)
    assert response.status_code == 422
    assert "REGISTER_PREFLIGHT_NOT_READY" in response.text
    assert count(container.config, "registration_snapshots") == 0
