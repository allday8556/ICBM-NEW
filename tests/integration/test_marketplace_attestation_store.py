"""SMARTSTORE-A0-PERMISSION handling through persistence, restart and the API (M2 PR-C).

``test_s17_18_*`` are the named tests for CAPABILITY_MAPPING.md §17 target 18 (owner PR-C): A0
save, read-back, validation, invalidation and rendering make zero SmartStore calls, and operator
attestation never becomes R0 or MACHINE_VERIFIED evidence. The ``a0_NN`` part maps each test to the
numbered PASS condition of docs/acceptance/M2.md §4. The application identity and the mapping
revision come from test fixtures — the seams PR-A implements — never from a production value.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.connect.marketplace.attestation import (
    SELF_AUTH_MODE,
    SMARTSTORE_PROVIDER,
    ApiGroup,
    ApplicationIdentity,
    EvidenceFreshness,
    Invalidation,
    Promotion,
)
from app.connect.marketplace.capability import (
    ContractFreshness,
    EvidenceGrade,
    EvidenceStrength,
    PauseReason,
    WriteScopeStatus,
    WriteStatus,
)
from app.container import Container, build_container
from app.core.egress import EGRESS
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore
from app.main import create_app
from app.system.secret_scan import scan
from tests.conftest import LOCAL
from tests.support import FakeClock

pytestmark = pytest.mark.integration

KEY = "smartstore"
URL = f"/api/v1/connect/marketplaces/{KEY}/permission-attestation"
FRESHNESS_URL = f"/api/v1/connect/marketplaces/{KEY}/contract-freshness"
CAPABILITY_URL = f"/api/v1/connect/marketplaces/{KEY}/capability"
CLIENT = {"X-ICBM-Client": "pytest"}
CLIENT_ID = "fixture-client-id-7f3a9c21d4e8"
OTHER_CLIENT_ID = "fixture-client-id-other-51be90"
REVISION = "fixture-mapping-revision-1"  # a test fixture, never a production value
ACTOR = "operator:test"
ROWS = "SELECT COUNT(*) FROM marketplace_permission_attestations"


class FixtureIdentity:
    """Stands in for PR-A's configured SmartStore application."""

    def __init__(self, client_id: str | None = CLIENT_ID) -> None:
        self.client_id = client_id

    def current_identity(self) -> ApplicationIdentity | None:
        if self.client_id is None:
            return None
        return ApplicationIdentity(SMARTSTORE_PROVIDER, SELF_AUTH_MODE, self.client_id)


class FixtureRevision:
    """Stands in for PR-A's endpoint-registry mapping revision."""

    def __init__(self, revision: str = REVISION) -> None:
        self.revision = revision

    def current_revision(self) -> str:
        return self.revision


@pytest.fixture
def bounded(config: AppConfig) -> AppConfig:
    """A configured evidence-age policy (the value itself awaits the architect, Q5)."""
    return config.with_overrides(smartstore_a0_max_age_days=30)


@contextmanager
def _process(
    config: AppConfig,
    clock: FakeClock,
    *,
    identity: FixtureIdentity | None = None,
    revision: FixtureRevision | None = None,
    secrets: MemorySecretStore | None = None,
) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=secrets or MemorySecretStore(),
            application_identity=identity,
            mapping_revision=revision,
        )
        try:
            yield built
        finally:
            built.db.dispose()


def _rows(config: AppConfig, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        return raw.execute(sql).fetchall()


def _current(container: Container) -> None:
    container.marketplace_capability.record_contract_freshness(
        KEY, ContractFreshness.CURRENT, actor=ACTOR
    )


# ---------------------------------------------------------------- M2.md §4 A0 PASS conditions


def test_s17_18_a0_01_02_an_attestation_is_stored_as_operator_attested_evidence_with_provenance(
    bounded: AppConfig, clock: FakeClock
) -> None:
    with _process(bounded, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        view = p.permission_attestation.attest(
            KEY, [ApiGroup.SELLER_INFO, ApiGroup.PRODUCT], actor=ACTOR
        )
    record = view.attestation
    assert record is not None
    assert record.evidence_strength is EvidenceStrength.OPERATOR_ATTESTED
    assert record.evidence_source == "OPERATOR_ATTESTED_PROVIDER_ADMIN"
    assert record.observed_at == clock.now()
    assert record.required_groups == [ApiGroup.PRODUCT]
    assert record.observed_groups == [ApiGroup.PRODUCT, ApiGroup.SELLER_INFO]
    assert (record.endpoint_mapping_revision, record.recorded_by) == (REVISION, ACTOR)
    assert record.attested_status is WriteScopeStatus.READY
    [(strength, source, fingerprint, required, observed, revision)] = _rows(
        bounded,
        "SELECT evidence_strength, evidence_source, application_fingerprint, required_groups,"
        " observed_groups, endpoint_mapping_revision FROM marketplace_permission_attestations",
    )
    assert (strength, source, required, observed, revision) == (
        "OPERATOR_ATTESTED",
        "OPERATOR_ATTESTED_PROVIDER_ADMIN",
        "PRODUCT",
        "PRODUCT,SELLER_INFO",
        REVISION,
    )
    assert isinstance(fingerprint, str) and len(fingerprint) == 64
    assert CLIENT_ID not in fingerprint


def test_s17_18_a0_03_positive_evidence_is_limited_strength_and_never_machine_verified(
    bounded: AppConfig, clock: FakeClock
) -> None:
    with _process(bounded, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        _current(p)
        view = p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
    assert view.promotion is Promotion.APPLIED
    assert view.capability_write_scope.evidence_strength is EvidenceStrength.OPERATOR_ATTESTED
    assert view.capability_write_scope.evidence_grade is EvidenceGrade.LIMITED
    # The database itself refuses an attestation stored as machine-verified.
    with (
        sqlite3.connect(bounded.data_dir / "icbm.db") as raw,
        pytest.raises(sqlite3.IntegrityError),
    ):
        raw.execute(
            "INSERT INTO marketplace_permission_attestations (marketplace_key, evidence_source,"
            " evidence_strength, observed_at, application_fingerprint, required_groups,"
            " observed_groups, endpoint_mapping_revision, recorded_by) VALUES ('smartstore',"
            " 'OPERATOR_ATTESTED_PROVIDER_ADMIN', 'MACHINE_VERIFIED', '2026-09-14 00:00:00',"
            " 'f', 'PRODUCT', 'PRODUCT', 'r', 'x')"
        )


def test_s17_18_a0_04_an_attestation_alone_never_sets_write_ready(
    bounded: AppConfig, clock: FakeClock
) -> None:
    with _process(bounded, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        _current(p)
        p.permission_attestation.attest(KEY, list(ApiGroup), actor=ACTOR)
        capability = p.marketplace_capability.capability(KEY)
    assert capability.write_scope.status is WriteScopeStatus.READY
    assert capability.write.status is WriteStatus.UNVERIFIED


def test_s17_18_a0_05_a_different_application_demotes_the_evidence_to_unknown(
    bounded: AppConfig, clock: FakeClock
) -> None:
    identity = FixtureIdentity()
    with _process(bounded, clock, identity=identity, revision=FixtureRevision()) as p:
        _current(p)
        attested = p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        assert attested.promotion is Promotion.APPLIED
        identity.client_id = OTHER_CLIENT_ID
        moved = p.permission_attestation.attestation(KEY)
        assert moved.capability_write_scope.status is WriteScopeStatus.UNKNOWN
        assert moved.evaluation is not None
        assert moved.evaluation.invalidations == [Invalidation.APPLICATION_FINGERPRINT_MISMATCH]
        assert moved.promotion is Promotion.NOT_CURRENT
        assert moved.attestation == attested.attestation  # the old evidence stays on record
        # Only fresh evidence for the new application counts.
        fresh = p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        assert fresh.promotion is Promotion.APPLIED


def test_s17_18_a0_06_a_changed_mapping_revision_or_requirement_invalidates(
    bounded: AppConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = FixtureRevision()
    with _process(bounded, clock, identity=FixtureIdentity(), revision=revision) as p:
        _current(p)
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        revision.revision = "fixture-mapping-revision-2"
        changed = p.permission_attestation.attestation(KEY)
        assert changed.capability_write_scope.status is WriteScopeStatus.UNKNOWN
        assert changed.evaluation is not None
        assert changed.evaluation.invalidations == [Invalidation.MAPPING_REVISION_CHANGED]
        revision.revision = REVISION
        assert p.permission_attestation.attestation(KEY).promotion is Promotion.APPLIED
        monkeypatch.setattr(
            "app.connect.marketplace.attestation_service.PRODUCT_REGISTRATION_REQUIRED_GROUPS",
            frozenset({ApiGroup.PRODUCT, ApiGroup.SELLER_INFO}),
        )
        widened = p.permission_attestation.attestation(KEY)
        assert widened.capability_write_scope.status is WriteScopeStatus.UNKNOWN
        assert widened.evaluation is not None
        assert widened.evaluation.invalidations == [Invalidation.REQUIRED_GROUPS_CHANGED]


def test_s17_18_a0_07_expired_or_unbounded_evidence_converges_to_unknown(
    config: AppConfig, bounded: AppConfig, clock: FakeClock
) -> None:
    with _process(bounded, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        _current(p)
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        clock.advance(timedelta(days=30).total_seconds())
        assert p.permission_attestation.attestation(KEY).promotion is Promotion.APPLIED
        clock.advance(1)
        expired = p.permission_attestation.attestation(KEY)
    assert expired.capability_write_scope.status is WriteScopeStatus.UNKNOWN
    assert expired.evaluation is not None
    assert expired.evaluation.freshness_status is EvidenceFreshness.EXPIRED
    assert expired.evaluation.invalidations == [Invalidation.EXPIRED]
    # With no bounded age policy configured, operator evidence is never current (§8, Q5).
    with _process(config, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        unbounded = p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
    assert unbounded.max_age_days is None
    assert unbounded.capability_write_scope.status is WriteScopeStatus.UNKNOWN
    assert unbounded.evaluation is not None
    assert Invalidation.NO_AGE_POLICY in unbounded.evaluation.invalidations


def test_s17_18_a0_08_malformed_or_incomplete_evidence_never_becomes_ready(
    bounded: AppConfig,
) -> None:
    app = create_app(
        bounded, application_identity=FixtureIdentity(), mapping_revision=FixtureRevision()
    )
    with TestClient(app, base_url=LOCAL) as client:
        client.post(FRESHNESS_URL, json={"contract_freshness": "CURRENT"}, headers=CLIENT)
        for body in ({"observed_groups": ["상품"]}, {"observed_groups": "PRODUCT"}, {}):
            assert client.post(URL, json=body, headers=CLIENT).status_code == 422, body
        assert _rows(bounded, ROWS) == [(0,)]
        # A stored row that no longer rebuilds is malformed evidence: it counts for nothing.
        with sqlite3.connect(bounded.data_dir / "icbm.db") as raw:
            raw.execute(
                "INSERT INTO marketplace_permission_attestations (marketplace_key,"
                " evidence_source, evidence_strength, observed_at, application_fingerprint,"
                " required_groups, observed_groups, endpoint_mapping_revision, recorded_by)"
                " VALUES ('smartstore', 'OPERATOR_ATTESTED_PROVIDER_ADMIN', 'OPERATOR_ATTESTED',"
                " '2026-09-14 00:00:00', 'f', 'PRODUCT', 'PRODUCT,NOT_A_GROUP',"
                f" '{REVISION}', 'x')"
            )
        body = client.get(URL).json()
    assert body["attestation"] is None
    assert body["evaluation"]["invalidations"] == ["MALFORMED"]
    assert body["promotion"] == "NOT_CURRENT"
    assert body["capability_write_scope"]["status"] == "UNKNOWN"


def test_s17_18_a0_09_evidence_and_its_invalidation_survive_a_restart(
    bounded: AppConfig, clock: FakeClock
) -> None:
    secrets = MemorySecretStore()  # stands in for the OS secret store, which outlives a process
    revision = FixtureRevision()
    with _process(
        bounded, clock, identity=FixtureIdentity(), revision=revision, secrets=secrets
    ) as first:
        _current(first)
        before = first.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
    with _process(
        bounded, clock, identity=FixtureIdentity(), revision=revision, secrets=secrets
    ) as second:
        after = second.permission_attestation.attestation(KEY)
    assert after.attestation == before.attestation
    assert (after.promotion, after.capability_write_scope.status) == (
        Promotion.APPLIED,
        WriteScopeStatus.READY,
    )
    with _process(
        bounded,
        clock,
        identity=FixtureIdentity(OTHER_CLIENT_ID),
        revision=revision,
        secrets=secrets,
    ) as third:
        moved = third.permission_attestation.attestation(KEY)
    assert moved.capability_write_scope.status is WriteScopeStatus.UNKNOWN
    assert moved.evaluation is not None
    assert moved.evaluation.invalidations == [Invalidation.APPLICATION_FINGERPRINT_MISMATCH]


def test_s17_18_a0_10_11_attestation_handling_makes_no_network_call(bounded: AppConfig) -> None:
    # Save, validate, read back, invalidate and render: the egress guard sees no attempt and no
    # grant. The structural rule test_marketplace_capability_code_cannot_reach_a_provider covers
    # every A0 module too, so no product endpoint can be probed "for confirmation".
    EGRESS.install()
    before = EGRESS.snapshot()
    identity = FixtureIdentity()
    app = create_app(bounded, application_identity=identity, mapping_revision=FixtureRevision())
    with TestClient(app, base_url=LOCAL) as client:
        assert client.get(URL).status_code == 200
        saved = client.post(URL, json={"observed_groups": ["PRODUCT"]}, headers=CLIENT)
        assert saved.status_code == 200, saved.text
        assert (
            client.post(URL, json={"observed_groups": ["NOPE"]}, headers=CLIENT).status_code == 422
        )
        recorded = client.post(
            FRESHNESS_URL, json={"contract_freshness": "CURRENT"}, headers=CLIENT
        )
        assert recorded.status_code == 200
        assert client.get(URL).json()["promotion"] == "APPLIED"
        identity.client_id = OTHER_CLIENT_ID
        assert client.get(URL).json()["promotion"] == "NOT_CURRENT"
        assert client.get(CAPABILITY_URL).json()["write_scope"]["status"] == "UNKNOWN"
        assert client.get("/api/v1/screens/settings").status_code == 200
        assert client.get("/js/pages/permission-attestation.js").status_code == 200
    after = EGRESS.snapshot()
    assert after["external_attempts"] == before["external_attempts"]
    assert after["granted_events"] == before["granted_events"]


def test_s17_18_a0_12_evidence_logs_and_responses_carry_no_client_id(bounded: AppConfig) -> None:
    app = create_app(
        bounded, application_identity=FixtureIdentity(), mapping_revision=FixtureRevision()
    )
    responses = []
    with TestClient(app, base_url=LOCAL) as client:
        responses.append(
            client.post(FRESHNESS_URL, json={"contract_freshness": "CURRENT"}, headers=CLIENT).text
        )
        responses.append(
            client.post(URL, json={"observed_groups": ["PRODUCT"]}, headers=CLIENT).text
        )
        responses.append(client.get(URL).text)
        responses.append(client.get(CAPABILITY_URL).text)
    dump = bounded.data_dir / "api-responses.txt"
    dump.write_text("\n".join(responses), encoding="utf-8")
    report = scan([bounded.data_dir], {"client_id": CLIENT_ID})
    assert report["total_hits"] == 0
    assert isinstance(report["files_scanned"], int) and report["files_scanned"] >= 2


# ---------------------------------------------------------------- stored vs refused vs waiting


def test_s17_18_stored_evidence_waits_on_an_unrecorded_contract_instead_of_being_refused(
    bounded: AppConfig, clock: FakeClock
) -> None:
    with _process(bounded, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        view = p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        # Stored and current — but READY is new trust, and the contract is UNRECORDED (F5).
        assert view.attestation is not None
        assert view.evaluation is not None and view.evaluation.invalidations == []
        assert view.evaluation.write_scope.status is WriteScopeStatus.READY
        assert (view.contract_freshness, view.promotion) == (
            ContractFreshness.UNRECORDED,
            Promotion.BLOCKED_BY_CONTRACT_FRESHNESS,
        )
        assert view.capability_write_scope.status is WriteScopeStatus.UNKNOWN
        # The reviewed recording lets the waiting evidence converge; no re-attestation needed.
        _current(p)
        after = p.permission_attestation.attestation(KEY)
    assert (after.promotion, after.capability_write_scope.status) == (
        Promotion.APPLIED,
        WriteScopeStatus.READY,
    )


def test_s17_18_positive_absence_applies_even_before_the_contract_is_recorded(
    bounded: AppConfig, clock: FakeClock
) -> None:
    with _process(bounded, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        view = p.permission_attestation.attest(KEY, [ApiGroup.SELLER_INFO], actor=ACTOR)
        capability = p.marketplace_capability.capability(KEY)
    # Fail-closed evidence is not new trust, so it never waits (S2).
    assert (view.contract_freshness, view.promotion) == (
        ContractFreshness.UNRECORDED,
        Promotion.APPLIED,
    )
    assert capability.write_scope.status is WriteScopeStatus.MISSING
    assert capability.write.status is WriteStatus.BLOCKED
    assert [o.reason_code for o in capability.workflow] == [PauseReason.SCOPE_INSUFFICIENT]


def test_s17_18_without_an_application_or_a_mapping_revision_nothing_is_recorded(
    bounded: AppConfig,
) -> None:
    # Production wiring before PR-A: no seam is implemented, so recording is refused outright.
    with TestClient(create_app(bounded), base_url=LOCAL) as client:
        view = client.get(URL).json()
        assert (view["recording_available"], view["recording_refusal"]) == (
            False,
            "APPLICATION_NOT_CONFIGURED",
        )
        refused = client.post(URL, json={"observed_groups": ["PRODUCT"]}, headers=CLIENT)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "MARKETPLACE_ATTESTATION_REFUSED"
    assert refused.json()["error"]["details"] == {"reason": "APPLICATION_NOT_CONFIGURED"}
    with TestClient(
        create_app(bounded, application_identity=FixtureIdentity()), base_url=LOCAL
    ) as client:
        refused = client.post(URL, json={"observed_groups": ["PRODUCT"]}, headers=CLIENT)
    assert refused.json()["error"]["details"] == {"reason": "MAPPING_REVISION_UNAVAILABLE"}
    assert _rows(bounded, ROWS) == [(0,)]


def test_attestation_history_is_append_only(bounded: AppConfig, clock: FakeClock) -> None:
    with _process(bounded, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
    with sqlite3.connect(bounded.data_dir / "icbm.db") as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE marketplace_permission_attestations SET observed_groups = ''")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM marketplace_permission_attestations")


def test_every_attestation_is_audited_with_bindings_never_the_application(
    bounded: AppConfig, clock: FakeClock
) -> None:
    with _process(bounded, clock, identity=FixtureIdentity(), revision=FixtureRevision()) as p:
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        [event] = [
            e
            for e in p.audit.list_events(limit=20)
            if e.event_type == "MARKETPLACE_PERMISSION_ATTESTED"
        ]
    [(fingerprint,)] = _rows(
        bounded, "SELECT application_fingerprint FROM marketplace_permission_attestations"
    )
    assert (event.actor, event.outcome) == (ACTOR, "ALLOWED")
    assert event.details["observed_groups"] == ["PRODUCT"]
    assert event.details["evidence_strength"] == "OPERATOR_ATTESTED"
    assert event.details["endpoint_mapping_revision"] == REVISION
    dumped = json.dumps(event.model_dump(mode="json"))
    assert CLIENT_ID not in dumped
    assert isinstance(fingerprint, str) and fingerprint not in dumped
