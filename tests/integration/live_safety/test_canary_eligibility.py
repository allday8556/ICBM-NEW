"""The durable canary-eligibility owner (ADR-0018 §5.1; Issue #89 resolution 5910018106).

Through the real application on a migrated database, with no provider contacted:
- the review packet is built by the server from the canonical owners, deterministically, and its
  digest is what a record stores;
- only the closed v1 checklist with admissible evidence proves ``CANARY_NON_REGULATED``: an
  operator assertion, a missing, extra, unknown or in-scope key, or evidence that does not name
  the packet's own metadata revision or digest never does;
- the record is append-only: the current one is the highest ``seq`` of its exact scope, and a
  re-review or a rollback is another record;
- a proof holds for one exact lineage only and goes stale by itself on any drift — a new
  preparation revision, another candidate fingerprint, another category-metadata revision or
  policy revision — with no operator action reviving it;
- the ASSET stage and the CREATE stage read the same record through the lineage each derives, the
  CREATE one from the Intent's Snapshot and the authored revision that froze it, inside the unit
  that admits the attempt;
- it is never a ``COMPLIANCE PASS``: no other owner changes.
"""

import contextlib
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from fastapi.testclient import TestClient

from app.capabilities.audit.models import AuditEventType
from app.capabilities.live_safety import eligibility as owner
from app.capabilities.live_safety import model as live_model
from app.capabilities.live_safety.assets import PreparationCandidateGate
from app.capabilities.live_safety.eligibility import (
    CHECKLIST_INVALID,
    EVIDENCE_REFUSED,
    PACKET_MOVED,
    PACKET_UNAVAILABLE,
    EligibilityBinding,
    EligibilityVerdict,
    ScopeKey,
    asset_unit_ref,
    create_unit_ref,
)
from app.capabilities.live_safety.gates import create_stage_gate
from app.capabilities.live_safety.model import Layer, MutationStage
from app.capabilities.live_safety.stack import SafetyStack, Verdict
from app.config import AppConfig
from app.container import Container
from app.platform.core.errors import InputValidationError
from app.platform.db.migrate import alembic_config, upgrade_to_head
from app.stages.register.execution import CREATE_ENDPOINT_GROUP
from app.stages.register.model import RegistrationConflictError, sanitized_digest
from tests.integration.live_safety.test_g3b_restore_retention import (  # noqa: F401 - fixtures
    PREPARATIONS,
    api,
    asset_grant,
    container,
    durable_unit,
    frozen_unit,
    live,
    proofs,
)
from tests.integration.review.test_g2c_review_paths import authored_inputs
from tests.support.gate1_support import (
    CATEGORY,
    CLIENT,
    MARKET,
    OPERATOR,
    TAXONOMY,
    owned_revisions,
    save_policy,
)
from tests.support.live_safety_support import PermittedMode, ProvenProofs
from tests.support.product_support import raw

pytestmark = pytest.mark.integration

CID = "corr-eligibility"
TABLE = "canary_eligibility_records"
SCOPE = "smartstore-canary-nonregulated/v1"
KEYS = [key.value for key in ScopeKey]
AT = "2026-09-30 00:00:00"
FROZEN_TABLES = (
    "registration_preparation_revisions",
    "registration_category_metadata_revisions",
    "registration_target_policy_revisions",
    "registration_snapshots",
    "registration_intents",
    "live_grants",
)


def proven_checks(packet: owner.ReviewPacket, **overrides: Any) -> dict[str, Any]:
    """The closed checklist with every key excluded on admissible evidence."""
    metadata = {
        "finding": "OUTSIDE_SCOPE",
        "evidence_kind": "CATEGORY_METADATA",
        "evidence_ref": packet.binding.category_metadata_revision,
    }
    checks: dict[str, Any] = {
        "HEALTH_FUNCTIONAL_FOOD": dict(metadata),
        "KC_CERTIFICATION_REQUIRED": dict(metadata),
        "MFDS_NOTICE_OR_APPROVAL": {
            "finding": "OUTSIDE_SCOPE",
            "evidence_kind": "OFFICIAL_RULE",
            "evidence_ref": "reviewed-rule-reference-1",
        },
        "PROHIBITED_OR_RESTRICTED_WORDING": {
            "finding": "OUTSIDE_SCOPE",
            "evidence_kind": "LISTING_REVIEW_PACKET",
            "evidence_ref": packet.digest,
        },
        "OTHER_REGULATED_OR_RESTRICTED_CATEGORY": dict(metadata),
    }
    checks.update(overrides)
    return checks


def record(container: Container, preparation_id: str, **overrides: Any) -> Any:  # noqa: F811
    packet = container.canary_eligibility.review_packet(preparation_id)
    return container.canary_eligibility.record(
        preparation_id=preparation_id,
        expected_packet_digest=packet.digest,
        checks=proven_checks(packet, **overrides),
        actor=OPERATOR,
        correlation_id=CID,
    )


def asset_binding(container: Container, preparation_id: str) -> EligibilityBinding | None:  # noqa: F811
    """The lineage the ASSET stage derives, from the very gate its readiness reads."""
    preparation = container.registrations.preparation(preparation_id)
    assert preparation is not None
    gate = PreparationCandidateGate(container.registration_preparations, container.registrations)
    return gate.current(preparation.current.preparation_revision_id).eligibility


def asset_proven(container: Container, binding: EligibilityBinding | None) -> bool:  # noqa: F811
    if binding is None:
        return False
    return proofs(container).canary_non_regulated(MutationStage.ASSET, binding.unit_ref, binding)


def rows(config: AppConfig) -> list[tuple[Any, ...]]:
    with contextlib.closing(raw(config)) as connection:
        return connection.execute(
            f"SELECT seq, verdict, review_packet_digest, candidate_fingerprint FROM {TABLE}"
            " ORDER BY recorded_at, seq"
        ).fetchall()


def frozen_counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in FROZEN_TABLES
        }


def eligibility_layer(readiness: Any) -> bool:
    """Whether the stack still has an eligibility layer. ADR-0018 §5 amendment (owner decision
    2026-10-03): a regulated category is the seller's risk, so it has none."""
    return any(view.layer is Layer.CANARY_NON_REGULATED for view in readiness.layers)


# ------------------------------------------------------------------ the review packet


def test_the_server_builds_a_deterministic_packet_from_the_owners(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = durable_unit(api, container, config)
    service = container.canary_eligibility
    first = service.review_packet(unit["preparation_id"])
    again = service.review_packet(unit["preparation_id"])
    assert first == again and first.digest == sanitized_digest(first.packet)
    preparation = container.registrations.preparation(unit["preparation_id"])
    assert preparation is not None
    revision_id = preparation.current.preparation_revision_id
    # The candidate both stages bind, duplicate evidence included (5915900049 D4).
    candidate = container.registration_preparations.stage_candidate(unit["preparation_id"])
    metadata = candidate.resolved.metadata
    assert metadata is not None
    packet = first.packet
    assert {k: packet[k] for k in packet if k != "publication_text"} == {
        "packet_version": "canary-eligibility-review-packet/v1",
        "scope_version": SCOPE,
        "marketplace_key": MARKET,
        "marketplace_account_id": unit["account"],
        "preparation_revision_id": revision_id,
        "candidate_fingerprint": candidate.candidate_fingerprint,
        "taxonomy_revision": TAXONOMY,
        "category_id": CATEGORY,
        "category_metadata_revision": metadata.metadata_revision,
        "category_metadata": {"current": True, "reviewed": True, "leaf": True, "registrable": True},
        "checklist": KEYS,
    }
    # The publication-facing text of exactly this preparation, for the wording review.
    text = packet["publication_text"]
    assert text["name"] == {"value": "합성 상품", "provenance": "OPERATOR_CONFIRMED"}
    # B-DETAIL: the sections are the owned composition profile's, server-derived.
    assert text["detail"] == {"sections": ["DETAIL_IMAGES", "BODY"], "body": "상세 본문"}
    assert text["attributes"]["brand"]["value"] == "합성 브랜드"
    assert text["notices"]["returnCostReason"]["detail_page_reference"] is True
    assert first.binding == EligibilityBinding(
        unit_ref=asset_unit_ref(revision_id),
        marketplace_key=MARKET,
        marketplace_account_id=unit["account"],
        preparation_revision_id=revision_id,
        candidate_fingerprint=candidate.candidate_fingerprint,
        taxonomy_revision=TAXONOMY,
        category_id=CATEGORY,
        category_metadata_revision=metadata.metadata_revision,
        scope_version=SCOPE,
        review_packet_digest=first.digest,
    )
    # Reading a packet records nothing.
    assert rows(config) == []


def test_no_packet_exists_without_reviewed_metadata_or_a_defined_scope(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = durable_unit(api, container, config)
    service = container.canary_eligibility
    evaluated = container.registration_preparations.evaluate(unit["preparation_id"])
    metadata = evaluated.resolved.metadata
    assert metadata is not None
    for change in (
        {"reviewed": False},
        {"leaf": False},
        {"registrable": False},
        {"category_id": "another-category"},
        {"taxonomy_revision": "another-taxonomy"},
    ):
        moved = replace(
            evaluated, resolved=replace(evaluated.resolved, metadata=replace(metadata, **change))
        )
        assert owner.binding_of(moved, "revision-1", unit_ref="u") is None, change
    for moved in (
        replace(evaluated, resolved=replace(evaluated.resolved, metadata=None)),
        replace(evaluated, request=replace(evaluated.request, category=None)),
    ):
        assert owner.binding_of(moved, "revision-1", unit_ref="u") is None
    assert owner.binding_of(evaluated, None, unit_ref="u") is None
    assert owner.binding_of(None, "revision-1", unit_ref="u") is None
    # A marketplace with no defined regulated scope has no eligibility proof at all.
    monkeypatch.setattr(owner, "SCOPE_VERSIONS", {})
    assert asset_binding(container, unit["preparation_id"]) is None
    with pytest.raises(InputValidationError) as refused:
        service.review_packet(unit["preparation_id"])
    assert refused.value.code == PACKET_UNAVAILABLE
    assert rows(config) == []


# ------------------------------------------------------------------ recording


def test_a_fully_reviewed_lineage_proves_the_asset_stage(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    binding = asset_binding(container, unit["preparation_id"])
    assert binding is not None and not asset_proven(container, binding)
    assert not eligibility_layer(container.asset_uploads.readiness(grant_id))
    before = frozen_counts(config)

    recorded = record(container, unit["preparation_id"])
    assert (recorded.seq, recorded.verdict) == (1, EligibilityVerdict.PROVEN_OUTSIDE)
    assert recorded.recorded_by == OPERATOR and set(recorded.checks) == set(KEYS)
    # Every stored identity is the server's own derivation.
    assert recorded.review_packet_digest == binding.review_packet_digest
    assert (
        recorded.marketplace_account_id,
        recorded.preparation_revision_id,
        recorded.candidate_fingerprint,
        recorded.taxonomy_revision,
        recorded.category_id,
        recorded.category_metadata_revision,
        recorded.scope_version,
    ) == (
        unit["account"],
        binding.preparation_revision_id,
        binding.candidate_fingerprint,
        TAXONOMY,
        CATEGORY,
        binding.category_metadata_revision,
        SCOPE,
    )
    assert container.canary_eligibility.current(binding) == recorded

    # The record does not move the candidate it was reviewed for: the same lineage is derived.
    assert asset_binding(container, unit["preparation_id"]) == binding
    assert asset_proven(container, binding)
    # The production stack no longer has an eligibility layer (ADR-0018 §5 amendment): the record
    # stays evidence the owner answers, and every other layer of this main still refuses.
    readiness = container.asset_uploads.readiness(grant_id)
    assert not eligibility_layer(readiness)
    assert readiness.verdict is not Verdict.READY
    assert live_model.ELIGIBILITY_UNPROVEN not in readiness.missing
    assert live_model.MODE_NOT_LIVE in readiness.missing

    # It is evidence for the canary only: no other owner's rows changed, and the audit event
    # carries identifiers and the digest, never an evidence reference.
    assert frozen_counts(config) == before
    (event,) = [
        e
        for e in container.audit.list_events(limit=500)
        if e.event_type == AuditEventType.CANARY_ELIGIBILITY_RECORDED
    ]
    assert event.after == {
        "eligibility_id": recorded.eligibility_id,
        "seq": 1,
        "verdict": "PROVEN_OUTSIDE",
        "review_packet_digest": recorded.review_packet_digest,
    }
    assert "reviewed-rule-reference-1" not in str(event.model_dump())
    assert "COMPLIANCE" not in json.dumps(dict(recorded.checks)) + recorded.verdict.value


REFUSED = {
    "operator-assertion": (
        lambda c, p: c["HEALTH_FUNCTIONAL_FOOD"].update(evidence_kind="OPERATOR_ASSERTION"),
        EVIDENCE_REFUSED,
    ),
    "free-form-kind": (
        lambda c, p: c["KC_CERTIFICATION_REQUIRED"].update(evidence_kind="TRUST_ME"),
        EVIDENCE_REFUSED,
    ),
    "excluded-without-evidence": (
        lambda c, p: c["MFDS_NOTICE_OR_APPROVAL"].update(evidence_kind=None, evidence_ref=None),
        EVIDENCE_REFUSED,
    ),
    "kind-without-reference": (
        lambda c, p: c["MFDS_NOTICE_OR_APPROVAL"].update(evidence_ref=None),
        CHECKLIST_INVALID,
    ),
    "empty-reference": (
        lambda c, p: c["MFDS_NOTICE_OR_APPROVAL"].update(evidence_ref="   "),
        EVIDENCE_REFUSED,
    ),
    "url-reference": (
        lambda c, p: c["MFDS_NOTICE_OR_APPROVAL"].update(evidence_ref="https://x.invalid/rule"),
        EVIDENCE_REFUSED,
    ),
    "long-reference": (
        lambda c, p: c["MFDS_NOTICE_OR_APPROVAL"].update(evidence_ref="r" * 201),
        EVIDENCE_REFUSED,
    ),
    "another-metadata-revision": (
        lambda c, p: c["HEALTH_FUNCTIONAL_FOOD"].update(evidence_ref="metadata-of-another"),
        EVIDENCE_REFUSED,
    ),
    "another-packet": (
        lambda c, p: c["PROHIBITED_OR_RESTRICTED_WORDING"].update(evidence_ref="a" * 64),
        EVIDENCE_REFUSED,
    ),
    "missing-key": (
        lambda c, p: c.pop("OTHER_REGULATED_OR_RESTRICTED_CATEGORY"),
        CHECKLIST_INVALID,
    ),
    "extra-key": (lambda c, p: c.update(EXTRA_SCOPE=dict(c[KEYS[0]])), CHECKLIST_INVALID),
    "unknown-finding": (lambda c, p: c[KEYS[0]].update(finding="MAYBE"), CHECKLIST_INVALID),
    "extra-member": (lambda c, p: c[KEYS[0]].update(note="looks fine"), CHECKLIST_INVALID),
    "missing-member": (lambda c, p: c[KEYS[0]].pop("evidence_ref"), CHECKLIST_INVALID),
    "bare-string": (lambda c, p: c.update({KEYS[0]: "OUTSIDE_SCOPE"}), CHECKLIST_INVALID),
    "reference-not-text": (lambda c, p: c[KEYS[0]].update(evidence_ref=1), CHECKLIST_INVALID),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_an_inadmissible_review_is_refused_whole_and_writes_nothing(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    case: str,
) -> None:
    unit = durable_unit(api, container, config)
    service = container.canary_eligibility
    packet = service.review_packet(unit["preparation_id"])
    checks = proven_checks(packet)
    mutate, code = REFUSED[case]
    mutate(checks, packet)
    with pytest.raises(InputValidationError) as refused:
        service.record(
            preparation_id=unit["preparation_id"],
            expected_packet_digest=packet.digest,
            checks=checks,
            actor=OPERATOR,
            correlation_id=CID,
        )
    assert refused.value.code == code
    assert rows(config) == []
    assert not asset_proven(container, asset_binding(container, unit["preparation_id"]))


def test_a_client_cannot_name_the_packet_or_record_without_an_author(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = durable_unit(api, container, config)
    service = container.canary_eligibility
    packet = service.review_packet(unit["preparation_id"])
    # The digest a client sends is only the expectation of what it reviewed: another one refuses.
    for invented in ("a" * 64, "", packet.binding.candidate_fingerprint):
        with pytest.raises(RegistrationConflictError) as moved:
            service.record(
                preparation_id=unit["preparation_id"],
                expected_packet_digest=invented,
                checks=proven_checks(packet),
                actor=OPERATOR,
                correlation_id=CID,
            )
        assert moved.value.code == PACKET_MOVED
    with pytest.raises(InputValidationError):
        service.record(
            preparation_id=unit["preparation_id"],
            expected_packet_digest=packet.digest,
            checks=proven_checks(packet),
            actor="  ",
            correlation_id=CID,
        )
    assert rows(config) == []


@pytest.mark.parametrize("finding", ["IN_SCOPE", "UNKNOWN"])
def test_any_key_not_excluded_is_recorded_unproven(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    finding: str,
) -> None:
    unit = durable_unit(api, container, config)
    packet = container.canary_eligibility.review_packet(unit["preparation_id"])
    for evidence in (
        {"evidence_kind": None, "evidence_ref": None},
        {"evidence_kind": "OFFICIAL_RULE", "evidence_ref": "reviewed-rule-reference-2"},
    ):
        recorded = record(
            container,
            unit["preparation_id"],
            OTHER_REGULATED_OR_RESTRICTED_CATEGORY={"finding": finding, **evidence},
        )
        assert recorded.verdict is EligibilityVerdict.UNPROVEN
        assert not asset_proven(container, packet.binding)
    assert [row[:2] for row in rows(config)] == [(1, "UNPROVEN"), (2, "UNPROVEN")]


def test_the_current_record_is_the_highest_seq_and_a_re_review_is_appended(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = durable_unit(api, container, config)
    service = container.canary_eligibility
    binding = service.review_packet(unit["preparation_id"]).binding
    unknown = {"finding": "UNKNOWN", "evidence_kind": None, "evidence_ref": None}
    first = record(container, unit["preparation_id"], KC_CERTIFICATION_REQUIRED=unknown)
    assert not asset_proven(container, binding)
    second = record(container, unit["preparation_id"])
    assert asset_proven(container, binding)
    # A later review that no longer excludes a key is the current one: a rollback is a record.
    third = record(container, unit["preparation_id"], KC_CERTIFICATION_REQUIRED=unknown)
    assert not asset_proven(container, binding)
    assert [r.seq for r in (first, second, third)] == [1, 2, 3]
    history = service.history(binding)
    assert [r.eligibility_id for r in history] == [
        first.eligibility_id,
        second.eligibility_id,
        third.eligibility_id,
    ]
    assert [r.verdict.value for r in history] == ["UNPROVEN", "PROVEN_OUTSIDE", "UNPROVEN"]
    assert service.current(binding) == third
    assert history[0] == first and history[1] == second


# ------------------------------------------------------------------ exact lineage and staleness


def test_a_proof_never_proves_another_lineage(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = durable_unit(api, container, config)
    record(container, unit["preparation_id"])
    binding = asset_binding(container, unit["preparation_id"])
    assert binding is not None and asset_proven(container, binding)
    stage_proofs = proofs(container)
    for other in (
        replace(binding, marketplace_key="another-market"),
        replace(binding, marketplace_account_id="mpa-" + "0" * 32),
        replace(binding, preparation_revision_id="another-revision"),
        replace(binding, candidate_fingerprint="b" * 64),
        replace(binding, taxonomy_revision="another-taxonomy"),
        replace(binding, category_id="another-category"),
        replace(binding, category_metadata_revision="another-metadata-revision"),
        replace(binding, scope_version="smartstore-canary-nonregulated/v2"),
        replace(binding, review_packet_digest="c" * 64),
    ):
        assert not stage_proofs.canary_non_regulated(MutationStage.ASSET, other.unit_ref, other)
    # No lineage, another unit, or the other stage: unproven.
    assert not stage_proofs.canary_non_regulated(MutationStage.ASSET, binding.unit_ref, None)
    assert not stage_proofs.canary_non_regulated(
        MutationStage.ASSET, "preparation_revision:x", binding
    )
    assert not stage_proofs.canary_non_regulated(MutationStage.CREATE, binding.unit_ref, binding)
    forged = replace(binding, unit_ref=create_unit_ref("no-such-intent"))
    assert not stage_proofs.canary_non_regulated(MutationStage.CREATE, forged.unit_ref, forged)


def test_a_new_preparation_revision_makes_the_proof_stale(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = durable_unit(api, container, config)
    record(container, unit["preparation_id"])
    proven = asset_binding(container, unit["preparation_id"])
    assert proven is not None and asset_proven(container, proven)
    revised = api.post(
        f"{PREPARATIONS}/{unit['preparation_id']}",
        json={
            "item_ids": [unit["item_id"]],
            "actor": OPERATOR,
            "inputs": authored_inputs(
                body="새 본문", revisions=owned_revisions(container, unit["account"])
            ),
        },
        headers=CLIENT,
    )
    assert revised.status_code == 200, revised.text
    # The reviewed revision is no longer the current one, and the new one was never reviewed.
    assert not asset_proven(container, proven)
    current = asset_binding(container, unit["preparation_id"])
    assert current is not None and current != proven
    assert current.preparation_revision_id != proven.preparation_revision_id
    assert current.review_packet_digest != proven.review_packet_digest
    assert not asset_proven(container, current)
    # Nothing revives the old record; only a review of the new lineage proves it.
    assert len(rows(config)) == 1
    record(container, unit["preparation_id"])
    assert asset_proven(container, current) and not asset_proven(container, proven)


def test_a_new_metadata_or_policy_revision_makes_the_proof_stale(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = durable_unit(api, container, config)
    record(container, unit["preparation_id"])
    proven = asset_binding(container, unit["preparation_id"])
    assert proven is not None and asset_proven(container, proven)

    # Another target-policy revision: the same preparation revision, another candidate.
    save_policy(api, unit["account"], account_scoped=True)
    after_policy = asset_binding(container, unit["preparation_id"])
    assert after_policy is not None
    assert after_policy.preparation_revision_id == proven.preparation_revision_id
    assert after_policy.candidate_fingerprint != proven.candidate_fingerprint
    assert not asset_proven(container, after_policy)
    record(container, unit["preparation_id"])
    assert asset_proven(container, after_policy)

    # Another current category-metadata revision: the reviewed one is no longer current.
    current = api.get(
        f"/api/v1/settings/category-metadata/{MARKET}/{TAXONOMY}/{CATEGORY}", headers=CLIENT
    ).json()
    content = dict(current["content"])
    content["name_max_length"] = 90
    moved = api.post(
        f"/api/v1/settings/category-metadata/{MARKET}/{TAXONOMY}/{CATEGORY}/revisions",
        json={
            "actor": OPERATOR,
            "expected_current_revision": current["current"]["metadata_revision"],
            "content_provenance": "OPERATOR_CONFIRMED",
            "evidence_reference": "seller-center/category-50000803/second",
            "reviewed": True,
            "content": content,
        },
        headers=CLIENT,
    )
    assert moved.status_code == 200, moved.text
    after_metadata = asset_binding(container, unit["preparation_id"])
    assert after_metadata is not None
    assert after_metadata.category_metadata_revision != after_policy.category_metadata_revision
    assert not asset_proven(container, after_metadata)
    # The earlier lineages are what they were, and neither is what the stage derives now.
    assert after_metadata not in (proven, after_policy)
    assert [row[:2] for row in rows(config)] == [(1, "PROVEN_OUTSIDE"), (1, "PROVEN_OUTSIDE")]


# ------------------------------------------------------------------ the CREATE stage


def _frozen(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    *,
    review: bool,
) -> tuple[Any, dict[str, Any]]:
    """One frozen unit with its Intent; reviewed for eligibility before the freeze when asked.

    The register test marketplace is given the v1 scope here only: production defines it for
    SmartStore alone."""
    monkeypatch.setitem(owner.SCOPE_VERSIONS, "market_a", SCOPE)  # type: ignore[index]

    def before(preparation_id: str) -> None:
        if review:
            record(container, preparation_id)

    return frozen_unit(api, container, config, duplicate_proof=False, before_freeze=before)


def _scope(container: Container, intent: Any) -> Any:  # noqa: F811
    return container.registrations.execution_scope(
        intent.marketplace_key, intent.marketplace_account_id, CREATE_ENDPOINT_GROUP
    )


def test_the_create_stage_reads_the_same_record_through_the_intents_lineage(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen, _ = _frozen(api, container, config, monkeypatch, review=True)
    intent = frozen.intent
    gate = create_stage_gate(
        registrations=container.registrations,
        preparations=container.registration_preparations,
        intent=intent,
    )
    binding = gate.eligibility
    assert binding is not None and binding.unit_ref == create_unit_ref(intent.intent_id)
    assert binding.preparation_revision_id == frozen.preparation_revision_id
    # The one record made before the freeze: the CREATE lineage is the ASSET one.
    (row,) = rows(config)
    assert row == (1, "PROVEN_OUTSIDE", binding.review_packet_digest, binding.candidate_fingerprint)
    stage_proofs = proofs(container)
    assert stage_proofs.canary_non_regulated(MutationStage.CREATE, binding.unit_ref, binding)
    readiness = container.safety_stack.create_readiness(
        intent,
        attempt_no=1,
        endpoint_adopted=True,
        reconcile_path_adopted=True,
        scope=_scope(container, intent),
        stage_gate=gate,
    )
    # The owner still proves the record (above); the stack has no eligibility layer any more
    # (ADR-0018 §5 amendment), and every other layer of this main still refuses.
    assert not eligibility_layer(readiness) and readiness.verdict is not Verdict.READY
    # Another Intent, another authored revision or the ASSET unit never stands in for it.
    other_intent = replace(binding, unit_ref=create_unit_ref("another-intent"))
    assert not stage_proofs.canary_non_regulated(
        MutationStage.CREATE, other_intent.unit_ref, other_intent
    )
    other_revision = replace(binding, preparation_revision_id="another-revision")
    assert not stage_proofs.canary_non_regulated(
        MutationStage.CREATE, other_revision.unit_ref, other_revision
    )
    assert not stage_proofs.canary_non_regulated(MutationStage.ASSET, binding.unit_ref, binding)


def test_an_unreviewed_create_lineage_is_unproven(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen, _ = _frozen(api, container, config, monkeypatch, review=False)
    intent = frozen.intent
    gate = create_stage_gate(
        registrations=container.registrations,
        preparations=container.registration_preparations,
        intent=intent,
    )
    assert gate.eligibility is not None and rows(config) == []
    readiness = container.safety_stack.create_readiness(
        intent,
        attempt_no=1,
        endpoint_adopted=True,
        reconcile_path_adopted=True,
        scope=_scope(container, intent),
        stage_gate=gate,
    )
    # Unreviewed, and still no refusal on it: eligibility is not a layer any more.
    assert not eligibility_layer(readiness)
    assert live_model.ELIGIBILITY_UNPROVEN not in readiness.missing


class _EligibilityFromTheOwner(ProvenProofs):
    """Every other prerequisite declared proven; eligibility answered by the durable owner."""

    def __init__(self, durable: Any) -> None:
        super().__init__()
        self._durable = durable

    def canary_non_regulated(self, stage: MutationStage, unit_ref: str, binding: Any) -> bool:
        return bool(self._durable.canary_non_regulated(stage, unit_ref, binding))


@pytest.mark.parametrize("review", [True, False])
def test_admission_proves_eligibility_inside_the_unit_that_opens_the_attempt(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    review: bool,
) -> None:
    frozen, _ = _frozen(api, container, config, monkeypatch, review=review)
    intent = frozen.intent
    now = container.clock.now()
    container.live_authority.issue_create_grant(
        intent_id=intent.intent_id,
        not_before=now,
        expires_at=now + timedelta(hours=1),
        approved_by=OPERATOR,
        authorization_ref="5910018106",
        correlation_id=CID,
    )
    container.live_authority.release_brake(
        actor=OPERATOR,
        reason_code="CANARY_WINDOW",
        authorization_ref="5910018106",
        correlation_id=CID,
    )
    stack = SafetyStack(
        store=live(container),
        mode=PermittedMode(),
        proofs=_EligibilityFromTheOwner(proofs(container)),
        clock=container.clock,
    )
    copy = container.registration_preparations.execution_copy(intent.registration_snapshot_id)
    fence = stack.truth_fence()

    def admit(**lineage: Any) -> Any:
        with container.registrations.transaction() as unit:
            return stack.admit_create(
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
                correlation_id=CID,
                **lineage,
            )

    # ADR-0018 §5 amendment (owner decision 2026-10-03): admission no longer reads eligibility,
    # reviewed or not, with or without the lineage — every other layer still decides, and the
    # matching grant is spent in the write unit.
    del copy
    grant = admit()
    assert grant.budget_used == 1


def test_the_create_path_hands_its_send_gate_and_authored_revision_to_the_stack() -> None:
    import inspect

    from app.stages.register import execution

    source = inspect.getsource(execution.RegistrationExecutionService)
    assert "send_gate=fresh," in source
    assert "provenance = self._registrations.snapshot_preparation(" in source
    assert "None if provenance is None else provenance.preparation_revision_id" in source


# ------------------------------------------------------------------ the table


def _insert(
    connection: sqlite3.Connection, unit: dict[str, str], revision: str, **changes: Any
) -> None:
    excluded = {
        "finding": "OUTSIDE_SCOPE",
        "evidence_kind": "OFFICIAL_RULE",
        "evidence_ref": "reviewed-rule-reference-1",
    }
    checks: Any = {key: dict(excluded) for key in KEYS}
    values: dict[str, Any] = {
        "eligibility_id": "elig-x",
        "marketplace_key": MARKET,
        "marketplace_account_id": unit["account"],
        "preparation_revision_id": revision,
        "candidate_fingerprint": "a" * 64,
        "taxonomy_revision": TAXONOMY,
        "category_id": CATEGORY,
        "category_metadata_revision": "metadata-1",
        "scope_version": SCOPE,
        "seq": 1,
        "review_packet_digest": "b" * 64,
        "verdict": "PROVEN_OUTSIDE",
        "recorded_by": OPERATOR,
        "recorded_at": AT,
    }
    mutate = changes.pop("checks", None)
    values.update(changes)
    if callable(mutate):
        mutate(checks)
    elif mutate is not None:
        checks = mutate
    values["checks_json"] = checks if isinstance(checks, str) else json.dumps(checks)
    columns = ", ".join(values)
    connection.execute(
        f"INSERT INTO {TABLE} ({columns}) VALUES ({', '.join('?' for _ in values)})",
        tuple(values.values()),
    )


MALFORMED: dict[str, dict[str, Any]] = {
    "unknown-verdict": {"verdict": "COMPLIANCE_PASS"},
    "seq-zero": {"seq": 0},
    "seq-skipped": {"seq": 2},
    "fingerprint-uppercase": {"candidate_fingerprint": "A" * 64},
    "digest-short": {"review_packet_digest": "b" * 63},
    "no-author": {"recorded_by": ""},
    "no-scope-version": {"scope_version": ""},
    "unknown-account": {"marketplace_account_id": "mpa-" + "0" * 32},
    "unknown-revision": {"preparation_revision_id": "no-such-revision"},
    "checks-array": {"checks": "[]"},
    "checks-not-json": {"checks": "not json"},
    "proven-with-in-scope-key": {"checks": lambda c: c[KEYS[0]].update(finding="IN_SCOPE")},
    "proven-with-unknown-key": {"checks": lambda c: c[KEYS[1]].update(finding="UNKNOWN")},
    "proven-without-evidence": {
        "checks": lambda c: c[KEYS[2]].update(evidence_kind=None, evidence_ref=None)
    },
    "unproven-with-every-key-excluded": {"verdict": "UNPROVEN"},
    "operator-assertion-kind": {
        "checks": lambda c: c[KEYS[0]].update(evidence_kind="OPERATOR_ASSERTION")
    },
    "empty-reference": {"checks": lambda c: c[KEYS[0]].update(evidence_ref="")},
    "reference-not-text": {"checks": lambda c: c[KEYS[0]].update(evidence_ref=7)},
    "kind-without-reference": {"checks": lambda c: c[KEYS[0]].update(evidence_ref=None)},
    "unknown-finding": {"checks": lambda c: c[KEYS[0]].update(finding="PROBABLY_FINE")},
    "missing-key": {"checks": lambda c: c.pop(KEYS[4])},
    "extra-key": {"checks": lambda c: c.update(EXTRA_SCOPE=dict(c[KEYS[0]]))},
    "extra-member": {"checks": lambda c: c[KEYS[0]].update(note="looks fine")},
    "check-not-object": {"checks": lambda c: c.update({KEYS[0]: "OUTSIDE_SCOPE"})},
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_the_table_refuses_a_malformed_record(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
    case: str,
) -> None:
    unit = durable_unit(api, container, config)
    preparation = container.registrations.preparation(unit["preparation_id"])
    assert preparation is not None
    revision = preparation.current.preparation_revision_id
    with contextlib.closing(raw(config)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, unit, revision, **dict(MALFORMED[case]))
        connection.rollback()
        # The same row without the defect is accepted: the refusal is the defect's.
        _insert(connection, unit, revision)
        connection.rollback()
    assert rows(config) == []


def test_the_table_is_append_only_in_order_and_never_deleted(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    config: AppConfig,
) -> None:
    unit = durable_unit(api, container, config)
    recorded = record(container, unit["preparation_id"])
    before = rows(config)
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            f"UPDATE {TABLE} SET verdict = 'UNPROVEN'",
            f"UPDATE {TABLE} SET review_packet_digest = '{'c' * 64}'",
            f"UPDATE {TABLE} SET recorded_by = 'someone'",
            f"DELETE FROM {TABLE}",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
            connection.rollback()
        for seq in (1, 3):
            with pytest.raises(sqlite3.IntegrityError):
                _insert(
                    connection,
                    unit,
                    recorded.preparation_revision_id,
                    eligibility_id=f"elig-{seq}",
                    candidate_fingerprint=recorded.candidate_fingerprint,
                    seq=seq,
                )
            connection.rollback()
    assert rows(config) == before


def test_0033_is_additive_and_its_downgrade_fails_closed(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'icbm.db').as_posix()}"
    upgrade_to_head(url)

    def tables() -> set[str]:
        with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
            return {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }

    before = tables()
    command.downgrade(alembic_config(url), "0032_m5_registration_authoring_revisions")
    assert before - tables() == {
        TABLE,
        "extension_queues",
        "extension_queue_items",
        "residual_risk_acceptances",
        "registration_deletions",
        "synthetic_test_products",
        "supplier_common_image_decisions",
        "marketplace_category_catalog_snapshots",
        "marketplace_category_catalog_entries",
        "common_sales_option_revisions",
        "common_sales_option_axes",
        "common_sales_option_values",
        "current_common_sales_option_revision_moves",
        "common_option_fact_mapping_revisions",
        "common_option_fact_axis_mappings",
        "common_option_fact_value_mappings",
        "current_common_option_fact_mapping_moves",
        "atomic_sku_set_revisions",
        "atomic_skus",
        "atomic_sku_selections",
        "atomic_sku_revision_members",
        "atomic_sku_revision_selection_evidence",
        "current_atomic_sku_set_moves",
        "atomic_sku_product_items",
    }
    command.upgrade(alembic_config(url), "head")
    assert tables() == before
    with contextlib.closing(sqlite3.connect(tmp_path / "icbm.db")) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        _insert(connection, {"account": "mpa-" + "1" * 32}, "revision-1")
        connection.commit()
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(url), "0032_m5_registration_authoring_revisions")
    assert TABLE in tables()


def test_the_retention_owner_protects_the_table() -> None:
    from app.capabilities.live_safety.retention import PROTECTED_TABLES, RETENTION_CHECKS_VERSION

    assert TABLE in PROTECTED_TABLES
    assert RETENTION_CHECKS_VERSION == "evidence-retention-checks/v7"


def test_the_metadata_owner_gained_no_compliance_meaning() -> None:
    from dataclasses import fields

    from app.stages.register.policy import CategoryMetadata

    assert not any(
        word in field.name
        for field in fields(CategoryMetadata)
        for word in ("regulat", "complian", "eligib", "canary")
    )
