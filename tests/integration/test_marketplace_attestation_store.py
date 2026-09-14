"""SMARTSTORE-A0-PERMISSION handling through persistence, restart and the API (M2 PR-C).

``test_s17_18_*`` are the named tests for CAPABILITY_MAPPING.md §17 target 18 (owner PR-C): A0
save, read-back, validation, invalidation and rendering make zero SmartStore calls, and operator
attestation never becomes R0 or MACHINE_VERIFIED evidence. The ``a0_NN`` part maps each test to the
numbered PASS condition of docs/acceptance/M2.md §4. ``test_s17_21_*``, ``test_s17_22_*`` and
``test_s17_23_*`` are the named tests for targets 21–23 (Issue #32): what is stored, expiry at the
30-day bound, and read-path convergence audited exactly once.

The application identity and the mapping revision come from test fixtures — the seams PR-A
implements — never from a production value. The fake clock drives every expiry: nothing sleeps
and nothing reads the wall clock.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.audit.service import AuditEventRecord
from app.config import AppConfig
from app.connect.marketplace.attestation import (
    A0_MAX_AGE_DAYS,
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
TABLE = "marketplace_permission_attestations"
ROWS = f"SELECT COUNT(*) FROM {TABLE}"
ALL_ROWS = f"SELECT * FROM {TABLE} ORDER BY seq"
BOUND = timedelta(days=A0_MAX_AGE_DAYS)


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


def _seams() -> dict[str, object]:
    return {"identity": FixtureIdentity(), "revision": FixtureRevision()}


def _rows(config: AppConfig, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        return raw.execute(sql).fetchall()


def _insert(config: AppConfig, **changes: object) -> None:
    """A raw row, bypassing the service, to prove what the database itself refuses."""
    row: dict[str, object] = {
        "marketplace_key": KEY,
        "evidence_source": "OPERATOR_ATTESTED_PROVIDER_ADMIN",
        "evidence_strength": "OPERATOR_ATTESTED",
        "observed_at": "2026-09-13 00:00:00",
        "application_fingerprint": "f",
        "required_groups": "PRODUCT",
        "observed_groups": "PRODUCT",
        "endpoint_mapping_revision": REVISION,
        "attested_status": "READY",
        "freshness_policy_max_age_days": A0_MAX_AGE_DAYS,
        "recorded_by": "x",
    }
    row.update(changes)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        raw.execute(f"INSERT INTO {TABLE} ({columns}) VALUES ({marks})", list(row.values()))


def _current(container: Container) -> None:
    container.marketplace_capability.record_contract_freshness(
        KEY, ContractFreshness.CURRENT, actor=ACTOR
    )


def _capability_events(container: Container) -> list[AuditEventRecord]:
    return [
        e
        for e in container.audit.list_events(limit=500)
        if e.event_type == "MARKETPLACE_CAPABILITY_CHANGED"
    ]


# ---------------------------------------------------------------- M2.md §4 A0 PASS conditions


def test_s17_18_a0_01_02_an_attestation_is_stored_as_operator_attested_evidence_with_provenance(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
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
    assert record.freshness_policy_max_age_days == A0_MAX_AGE_DAYS
    [(strength, source, fingerprint, required, observed, revision, status, days)] = _rows(
        config,
        "SELECT evidence_strength, evidence_source, application_fingerprint, required_groups,"
        " observed_groups, endpoint_mapping_revision, attested_status,"
        f" freshness_policy_max_age_days FROM {TABLE}",
    )
    assert (strength, source, required, observed, revision, status, days) == (
        "OPERATOR_ATTESTED",
        "OPERATOR_ATTESTED_PROVIDER_ADMIN",
        "PRODUCT",
        "PRODUCT,SELLER_INFO",
        REVISION,
        "READY",
        A0_MAX_AGE_DAYS,
    )
    assert isinstance(fingerprint, str) and len(fingerprint) == 64
    assert CLIENT_ID not in fingerprint


def test_s17_18_a0_03_positive_evidence_is_limited_strength_and_never_machine_verified(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
        _current(p)
        view = p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
    assert view.promotion is Promotion.APPLIED
    assert view.capability_write_scope.evidence_strength is EvidenceStrength.OPERATOR_ATTESTED
    assert view.capability_write_scope.evidence_grade is EvidenceGrade.LIMITED
    # The database itself refuses an attestation stored as machine-verified.
    with pytest.raises(sqlite3.IntegrityError):
        _insert(config, evidence_strength="MACHINE_VERIFIED")


def test_s17_18_a0_04_an_attestation_alone_never_sets_write_ready(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
        _current(p)
        p.permission_attestation.attest(KEY, list(ApiGroup), actor=ACTOR)
        capability = p.marketplace_capability.capability(KEY)
    assert capability.write_scope.status is WriteScopeStatus.READY
    assert capability.write.status is WriteStatus.UNVERIFIED


def test_s17_18_a0_05_a_different_application_demotes_the_evidence_to_unknown(
    config: AppConfig, clock: FakeClock
) -> None:
    identity = FixtureIdentity()
    with _process(config, clock, identity=identity, revision=FixtureRevision()) as p:
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
    config: AppConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = FixtureRevision()
    with _process(config, clock, identity=FixtureIdentity(), revision=revision) as p:
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


def test_s17_18_a0_07_expired_evidence_converges_to_unknown_and_a_tighter_policy_applies_at_once(
    config: AppConfig, clock: FakeClock
) -> None:
    secrets = MemorySecretStore()
    with _process(config, clock, secrets=secrets, **_seams()) as p:  # type: ignore[arg-type]
        _current(p)
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        clock.advance(BOUND.total_seconds() + 1)
        expired = p.permission_attestation.attestation(KEY)
        assert expired.capability_write_scope.status is WriteScopeStatus.UNKNOWN
        assert expired.evaluation is not None
        assert expired.evaluation.freshness_status is EvidenceFreshness.EXPIRED
        assert expired.evaluation.invalidations == [Invalidation.EXPIRED]
        assert expired.promotion is Promotion.NOT_CURRENT
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)  # recorded under 30
    # An override may only tighten the canonical bound, and it applies to existing evidence at once.
    tightened = config.with_overrides(smartstore_a0_max_age_days=7)
    with _process(tightened, clock, secrets=secrets, **_seams()) as p:  # type: ignore[arg-type]
        assert p.permission_attestation.attestation(KEY).promotion is Promotion.APPLIED
        clock.advance(timedelta(days=7).total_seconds() + 1)
        later = p.permission_attestation.attestation(KEY)
    assert later.max_age_days == 7
    assert later.attestation is not None
    assert later.attestation.freshness_policy_max_age_days == A0_MAX_AGE_DAYS
    assert later.evaluation is not None
    assert (later.evaluation.invalidations, later.evaluation.applicable_max_age_days) == (
        [Invalidation.EXPIRED],
        7,
    )
    assert later.capability_write_scope.status is WriteScopeStatus.UNKNOWN


def test_s17_18_a0_08_malformed_or_incomplete_evidence_never_becomes_ready(
    config: AppConfig,
) -> None:
    app = create_app(
        config, application_identity=FixtureIdentity(), mapping_revision=FixtureRevision()
    )
    with TestClient(app, base_url=LOCAL) as client:
        client.post(FRESHNESS_URL, json={"contract_freshness": "CURRENT"}, headers=CLIENT)
        for body in ({"observed_groups": ["상품"]}, {"observed_groups": "PRODUCT"}, {}):
            assert client.post(URL, json=body, headers=CLIENT).status_code == 422, body
        assert _rows(config, ROWS) == [(0,)]
        # A stored row that no longer rebuilds is malformed evidence: it counts for nothing.
        _insert(config, observed_groups="PRODUCT,NOT_A_GROUP")
        body = client.get(URL).json()
    assert body["attestation"] is None
    assert body["evaluation"]["invalidations"] == ["MALFORMED"]
    assert body["promotion"] == "NOT_CURRENT"
    assert body["capability_write_scope"]["status"] == "UNKNOWN"


def test_s17_18_a0_09_evidence_and_its_invalidation_survive_a_restart(
    config: AppConfig, clock: FakeClock
) -> None:
    secrets = MemorySecretStore()  # stands in for the OS secret store, which outlives a process
    revision = FixtureRevision()
    with _process(
        config, clock, identity=FixtureIdentity(), revision=revision, secrets=secrets
    ) as first:
        _current(first)
        before = first.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
    with _process(
        config, clock, identity=FixtureIdentity(), revision=revision, secrets=secrets
    ) as second:
        after = second.permission_attestation.attestation(KEY)
    assert after.attestation == before.attestation
    assert (after.promotion, after.capability_write_scope.status) == (
        Promotion.APPLIED,
        WriteScopeStatus.READY,
    )
    with _process(
        config,
        clock,
        identity=FixtureIdentity(OTHER_CLIENT_ID),
        revision=revision,
        secrets=secrets,
    ) as third:
        moved = third.permission_attestation.attestation(KEY)
    assert moved.capability_write_scope.status is WriteScopeStatus.UNKNOWN
    assert moved.evaluation is not None
    assert moved.evaluation.invalidations == [Invalidation.APPLICATION_FINGERPRINT_MISMATCH]


def test_s17_18_a0_10_11_attestation_handling_makes_no_network_call(config: AppConfig) -> None:
    # Save, validate, read back, invalidate and render: the egress guard sees no attempt and no
    # grant. The structural rule test_marketplace_capability_code_cannot_reach_a_provider covers
    # every A0 module too, so no product endpoint can be probed "for confirmation".
    EGRESS.install()
    before = EGRESS.snapshot()
    identity = FixtureIdentity()
    app = create_app(config, application_identity=identity, mapping_revision=FixtureRevision())
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


def test_s17_18_a0_12_evidence_logs_and_responses_carry_no_client_id(config: AppConfig) -> None:
    app = create_app(
        config, application_identity=FixtureIdentity(), mapping_revision=FixtureRevision()
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
    dump = config.data_dir / "api-responses.txt"
    dump.write_text("\n".join(responses), encoding="utf-8")
    report = scan([config.data_dir], {"client_id": CLIENT_ID})
    assert report["total_hits"] == 0
    assert isinstance(report["files_scanned"], int) and report["files_scanned"] >= 2


# ---------------------------------------------------------------- §17 #21: what is stored


def test_s17_21_evidence_keeps_its_attested_status_and_bound_but_never_current_freshness(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        p.permission_attestation.attest(KEY, [ApiGroup.SELLER_INFO], actor=ACTOR)
    assert _rows(
        config, f"SELECT attested_status, freshness_policy_max_age_days FROM {TABLE} ORDER BY seq"
    ) == [("READY", A0_MAX_AGE_DAYS), ("MISSING", A0_MAX_AGE_DAYS)]
    columns = {row[1] for row in _rows(config, f"PRAGMA table_info({TABLE})")}
    assert {"attested_status", "freshness_policy_max_age_days"} <= columns
    # Current truth is derived on every read, so nothing current is stored beside the evidence.
    assert not columns & {"freshness_status", "status", "write_scope_status", "current"}
    # The database refuses a stored status that contradicts the stored groups — for every group
    # of the live vocabulary, so a new ApiGroup without a new migration fails here.
    for group in ApiGroup:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(config, required_groups=group.value, observed_groups="")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(
                config,
                required_groups=group.value,
                observed_groups=group.value,
                attested_status="MISSING",
            )
    # ...and a bound outside the canonical 1..30 days.
    for days in (0, A0_MAX_AGE_DAYS + 1):
        with pytest.raises(sqlite3.IntegrityError):
            _insert(config, freshness_policy_max_age_days=days)
    assert _rows(config, ROWS) == [(2,)]


# ---------------------------------------------------------------- §17 #22: expiry


@pytest.mark.parametrize(
    ("observed", "attested"),
    [
        ([ApiGroup.PRODUCT], WriteScopeStatus.READY),
        ([ApiGroup.SELLER_INFO], WriteScopeStatus.MISSING),
    ],
    ids=["ready", "missing"],
)
def test_s17_22_ready_and_missing_evidence_expire_only_after_the_30_day_bound(
    config: AppConfig, clock: FakeClock, observed: list[ApiGroup], attested: WriteScopeStatus
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
        _current(p)
        p.permission_attestation.attest(KEY, observed, actor=ACTOR)
        clock.advance(BOUND.total_seconds() - 1)
        just_before = p.permission_attestation.attestation(KEY)
        clock.advance(1)
        exactly_at = p.permission_attestation.attestation(KEY)
        clock.advance(1)
        just_after = p.permission_attestation.attestation(KEY)
        capability = p.marketplace_capability.capability(KEY)
    for view in (just_before, exactly_at):
        assert view.evaluation is not None
        assert view.evaluation.freshness_status is EvidenceFreshness.FRESH
        assert view.capability_write_scope.status is attested
    assert just_after.evaluation is not None
    assert just_after.evaluation.freshness_status is EvidenceFreshness.EXPIRED
    assert just_after.evaluation.invalidations == [Invalidation.EXPIRED]
    assert just_after.capability_write_scope.status is WriteScopeStatus.UNKNOWN
    assert capability.write.status is WriteStatus.UNVERIFIED  # never promoted
    assert capability.workflow == []


def test_s17_22_expired_missing_evidence_releases_the_pause_without_granting_permission(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
        p.permission_attestation.attest(KEY, [ApiGroup.SELLER_INFO], actor=ACTOR)
        blocked = p.marketplace_capability.capability(KEY)
        stored = _rows(config, ALL_ROWS)
        clock.advance(BOUND.total_seconds() + 1)
        view = p.permission_attestation.attestation(KEY)
        released = p.marketplace_capability.capability(KEY)
    assert (blocked.write_scope.status, blocked.write.status) == (
        WriteScopeStatus.MISSING,
        WriteStatus.BLOCKED,
    )
    assert [o.reason_code for o in blocked.workflow] == [PauseReason.SCOPE_INSUFFICIENT]
    # The positive basis of the pause expired (CAPABILITY_MAPPING S6): the pause goes, and the
    # permission becomes unknown — it is not granted.
    assert released.write_scope.status is WriteScopeStatus.UNKNOWN
    assert released.write.status is WriteStatus.UNVERIFIED
    assert released.workflow == []
    # The projection says the evidence expired, not that the permission appeared.
    assert view.promotion is Promotion.NOT_CURRENT
    assert view.evaluation is not None
    assert view.evaluation.invalidations == [Invalidation.EXPIRED]
    assert view.attestation is not None
    assert view.attestation.attested_status is WriteScopeStatus.MISSING
    # The historical attestation is still on record, unchanged.
    assert _rows(config, ALL_ROWS) == stored


# ---------------------------------------------------------------- §17 #23: convergence and audit


def test_s17_23_the_first_read_after_the_bound_converges_and_audits_exactly_once(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
        p.permission_attestation.attest(KEY, [ApiGroup.SELLER_INFO], actor=ACTOR)
        stored = _rows(config, ALL_ROWS)
        clock.advance(BOUND.total_seconds())
        p.marketplace_capability.capability(KEY)  # exactly at the bound: still current
        baseline = len(_capability_events(p))
        clock.advance(1)
        first = p.marketplace_capability.capability(KEY)
        converged = _capability_events(p)
        for _ in range(3):  # later reads find nothing to change
            assert p.marketplace_capability.capability(KEY) == first
            p.permission_attestation.attestation(KEY)
        repeated = _capability_events(p)
    assert len(converged) == baseline + 1
    assert repeated == converged
    change = converged[0]  # newest first
    assert change.action == "OBSERVE_PERMISSION"
    assert change.occurred_at == clock.now()  # the transition time, from the injected clock
    assert change.details["invalidations"] == ["EXPIRED"]
    assert change.before is not None and change.after is not None
    assert (change.before["write_scope_status"], change.after["write_scope_status"]) == (
        "MISSING",
        "UNKNOWN",
    )
    assert (change.before["evidence_strength"], change.after["evidence_strength"]) == (
        "OPERATOR_ATTESTED",
        None,
    )
    assert change.before["workflow"] == ["PRODUCT_REGISTRATION:PAUSED:SCOPE_INSUFFICIENT"]
    assert change.after["workflow"] == []
    assert change.after["write_status"] == "UNVERIFIED"
    assert first.updated_at == clock.now()
    # The attestation row itself is never touched by its expiry.
    assert _rows(config, ALL_ROWS) == stored


# ---------------------------------------------------------------- stored vs refused vs waiting


def test_s17_18_stored_evidence_waits_on_an_unrecorded_contract_instead_of_being_refused(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
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
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
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
    config: AppConfig,
) -> None:
    # Production wiring before PR-A: no seam is implemented, so recording is refused outright.
    with TestClient(create_app(config), base_url=LOCAL) as client:
        view = client.get(URL).json()
        assert (view["recording_available"], view["recording_refusal"]) == (
            False,
            "APPLICATION_NOT_CONFIGURED",
        )
        assert view["max_age_days"] == A0_MAX_AGE_DAYS  # the canonical default, never "unset"
        refused = client.post(URL, json={"observed_groups": ["PRODUCT"]}, headers=CLIENT)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "MARKETPLACE_ATTESTATION_REFUSED"
    assert refused.json()["error"]["details"] == {"reason": "APPLICATION_NOT_CONFIGURED"}
    with TestClient(
        create_app(config, application_identity=FixtureIdentity()), base_url=LOCAL
    ) as client:
        refused = client.post(URL, json={"observed_groups": ["PRODUCT"]}, headers=CLIENT)
    assert refused.json()["error"]["details"] == {"reason": "MAPPING_REVISION_UNAVAILABLE"}
    assert _rows(config, ROWS) == [(0,)]


def test_attestation_history_is_append_only(config: AppConfig, clock: FakeClock) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
    with sqlite3.connect(config.data_dir / "icbm.db") as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(f"UPDATE {TABLE} SET observed_groups = ''")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(f"DELETE FROM {TABLE}")


def test_every_attestation_is_audited_with_bindings_never_the_application(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock, **_seams()) as p:  # type: ignore[arg-type]
        p.permission_attestation.attest(KEY, [ApiGroup.PRODUCT], actor=ACTOR)
        [event] = [
            e
            for e in p.audit.list_events(limit=20)
            if e.event_type == "MARKETPLACE_PERMISSION_ATTESTED"
        ]
    [(fingerprint,)] = _rows(config, f"SELECT application_fingerprint FROM {TABLE}")
    assert (event.actor, event.outcome) == (ACTOR, "ALLOWED")
    assert event.details["observed_groups"] == ["PRODUCT"]
    assert event.details["evidence_strength"] == "OPERATOR_ATTESTED"
    assert event.details["endpoint_mapping_revision"] == REVISION
    assert event.details["attested_status"] == "READY"
    assert event.details["freshness_policy_max_age_days"] == A0_MAX_AGE_DAYS
    dumped = json.dumps(event.model_dump(mode="json"))
    assert CLIENT_ID not in dumped
    assert isinstance(fingerprint, str) and fingerprint not in dumped
