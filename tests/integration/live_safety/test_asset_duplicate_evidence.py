"""The ASSET stage binds the same candidate the CREATE stage does (Issue #89 resolution 5915900049
D4).

When the target policy requires duplicate proof, the CREATE path — the final preflight of the
freeze and the first CREATE copy — evaluates the unit **with** the admissible duplicate evidence
of the provider-neutral owner seam, and that evidence is part of the candidate fingerprint. The
ASSET stage (the candidate gate of an upload, the ASSET grant and the eligibility review packet)
must evaluate the same canonical candidate the same way, so all of them bind one fingerprint:

- with the evidence available, the ASSET candidate is READY and its fingerprint is exactly the
  CREATE copy's candidate fingerprint, so a grant, a prepared asset and an eligibility record can
  all bind the unit the CREATE later sends;
- with the evidence unavailable, the ASSET stage stays fail-closed: nothing is granted and no
  upload can start;
- a policy that does not require duplicate proof never consults the lookup.

Nothing here defines a second fingerprint, adopts a lookup endpoint or contacts a provider.
"""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.live_safety.assets import PreparationCandidateGate
from app.capabilities.live_safety.store import ArtifactRef
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import AppError
from app.stages.register.preparation import DUPLICATE_EVIDENCE_MISSING
from tests.integration.live_safety.test_g3b_restore_retention import (  # noqa: F401 - fixtures
    api,
    container,
    durable_unit,
    frozen_unit,
)
from tests.support.gate1_support import OPERATOR
from tests.support.product_support import count
from tests.support.register_support import FakeDuplicateLookup

pytestmark = pytest.mark.integration

CID = "corr-asset-duplicate-evidence"
APPROVAL = "5915900049"


def _artifacts(candidate: Any) -> list[ArtifactRef]:
    return [
        ArtifactRef(image.asset_kind, image.sha256, image.derivation_id)
        for item in candidate.resolved.items
        for image in item.images
    ]


def _grant(container: Container, preparation_id: str, fingerprint: str | None) -> Any:  # noqa: F811
    preparation = container.registrations.preparation(preparation_id)
    assert preparation is not None
    candidate = container.registration_preparations.evaluate(preparation_id)
    now = container.clock.now()
    return container.live_authority.issue_asset_grant(
        marketplace_key=preparation.marketplace_key,
        marketplace_account_id=preparation.marketplace_account_id,
        preparation_revision_id=preparation.current.preparation_revision_id,
        candidate_fingerprint=fingerprint or "0" * 64,
        artifacts=_artifacts(candidate),
        asset_profile=candidate.resolved.target.asset_policy.profile,
        budget=1,
        not_before=now,
        expires_at=now + timedelta(hours=1),
        approved_by=OPERATOR,
        authorization_ref=APPROVAL,
        correlation_id=CID,
    )


def test_the_asset_stage_binds_the_create_candidate_under_required_duplicate_proof(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    seen: dict[str, Any] = {}

    def asset_stage(preparation_id: str) -> None:
        # Before the freeze, exactly as a canary runs: the ASSET stage comes first.
        preparation = container.registrations.preparation(preparation_id)
        assert preparation is not None
        revision = preparation.current.preparation_revision_id
        gate = PreparationCandidateGate(
            container.registration_preparations, container.registrations
        )
        seen["gate"] = gate.current(revision)
        seen["grant"] = _grant(container, preparation_id, seen["gate"].fingerprint)

    frozen, _ = frozen_unit(api, container, config, duplicate_proof=True, before_freeze=asset_stage)
    copy = container.registration_preparations.execution_copy(
        frozen.intent.registration_snapshot_id
    )
    create_candidate = copy.final.candidate_fingerprint
    gate = seen["gate"]
    # The candidate the ASSET stage evaluates is READY and is the CREATE copy's candidate.
    assert gate.ready is True
    assert gate.fingerprint == create_candidate
    # So the grant binds that one candidate, and so would an asset prepared under it.
    assert seen["grant"].candidate_fingerprint == create_candidate


def test_the_eligibility_packet_binds_the_same_candidate_as_the_asset_gate(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    """The review packet and the ASSET gate's eligibility lineage come from one evaluation — the
    one with the duplicate evidence — never from the evidence-less candidate."""
    unit = durable_unit(api, container, config)
    preparation_id = unit["preparation_id"]
    preparation = container.registrations.preparation(preparation_id)
    assert preparation is not None
    gate = PreparationCandidateGate(container.registration_preparations, container.registrations)
    state = gate.current(preparation.current.preparation_revision_id)
    packet = container.canary_eligibility.review_packet(preparation_id)
    staged = container.registration_preparations.stage_candidate(preparation_id)
    without = container.registration_preparations.evaluate(preparation_id)
    assert state.eligibility is not None
    assert (
        packet.binding.candidate_fingerprint
        == state.eligibility.candidate_fingerprint
        == state.fingerprint
        == staged.candidate_fingerprint
    )
    assert without.candidate_fingerprint != staged.candidate_fingerprint
    assert DUPLICATE_EVIDENCE_MISSING in without.codes


def test_without_admissible_duplicate_evidence_the_asset_stage_stays_closed(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    seen: dict[str, Any] = {}

    def asset_stage(preparation_id: str) -> None:
        authoring = container.registration_preparations
        lookup = authoring._duplicate_lookup
        for unavailable in (
            FakeDuplicateLookup(available_result=False),
            FakeDuplicateLookup(return_none=True),
        ):
            authoring._duplicate_lookup = unavailable
            preparation = container.registrations.preparation(preparation_id)
            assert preparation is not None
            gate = PreparationCandidateGate(authoring, container.registrations)
            state = gate.current(preparation.current.preparation_revision_id)
            assert state.ready is False
            assert state.eligibility is None
            # No grant is recorded, whatever fingerprint the caller names.
            without = authoring.evaluate(preparation_id)
            assert DUPLICATE_EVIDENCE_MISSING in without.codes
            for named in (state.fingerprint, without.candidate_fingerprint):
                with pytest.raises(AppError):
                    _grant(container, preparation_id, named)
            with pytest.raises(AppError):
                container.canary_eligibility.review_packet(preparation_id)
            seen.setdefault("refused", 0)
            seen["refused"] += 1
        authoring._duplicate_lookup = lookup

    frozen_unit(api, container, config, duplicate_proof=True, before_freeze=asset_stage)
    assert seen["refused"] == 2
    assert count(container.config, "live_grants") == 0


def test_a_policy_without_duplicate_proof_never_consults_the_lookup(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    seen: dict[str, Any] = {}

    def asset_stage(preparation_id: str) -> None:
        authoring = container.registration_preparations
        spy = FakeDuplicateLookup()
        authoring._duplicate_lookup = spy
        preparation = container.registrations.preparation(preparation_id)
        assert preparation is not None
        gate = PreparationCandidateGate(authoring, container.registrations)
        seen["gate"] = gate.current(preparation.current.preparation_revision_id)
        seen["plain"] = authoring.evaluate(preparation_id)
        seen["calls"] = list(spy.calls)

    frozen_unit(api, container, config, duplicate_proof=False, before_freeze=asset_stage)
    assert seen["calls"] == []
    assert seen["gate"].ready is True
    assert seen["gate"].fingerprint == seen["plain"].candidate_fingerprint
