"""SMARTSTORE-A0-PERMISSION evidence model (M2 PR-C): pure domain, fixtures only, no provider call.

The persistence/service/API proofs of CAPABILITY_MAPPING §17 #18 are the ``test_s17_18_*`` tests
in tests/integration/test_marketplace_attestation_store.py; these tests pin the rules they rely on.
The mapping revision below is a test fixture, never a production value.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.connect.marketplace.attestation import (
    PRODUCT_REGISTRATION_REQUIRED_GROUPS,
    SELF_AUTH_MODE,
    SMARTSTORE_PROVIDER,
    ApiGroup,
    ApplicationIdentity,
    Attestation,
    AttestationContext,
    AttestationRefusedError,
    EvidenceFreshness,
    EvidenceSource,
    Invalidation,
    Promotion,
    application_fingerprint,
    attest,
    evaluate,
    promotion,
)
from app.connect.marketplace.capability import (
    PERMISSION_UNKNOWN,
    CapabilityInvariantError,
    ContractFreshness,
    EvidenceGrade,
    EvidenceStrength,
    WriteScope,
    WriteScopeStatus,
)

T0 = datetime(2026, 9, 14, tzinfo=UTC)
KEY = b"fixture-fingerprint-key-32-bytes!"
IDENTITY = ApplicationIdentity(SMARTSTORE_PROVIDER, SELF_AUTH_MODE, "fixture-client-id-A")
FINGERPRINT = application_fingerprint(IDENTITY, KEY)
REVISION = "fixture-mapping-revision-1"
MAX_AGE = timedelta(days=30)


def _context(**changes: object) -> AttestationContext:
    values: dict[str, object] = {
        "now": T0,
        "application_fingerprint": FINGERPRINT,
        "required_groups": PRODUCT_REGISTRATION_REQUIRED_GROUPS,
        "endpoint_mapping_revision": REVISION,
        "max_age": MAX_AGE,
    }
    values.update(changes)
    return AttestationContext(**values)  # type: ignore[arg-type]


def test_the_product_registration_requirement_is_the_contract_baseline() -> None:
    # PERMISSIONS_SCOPES §5.1 / Q4: {상품}; ENDPOINT_MATRIX §5 keeps it out of the M2 registry.
    assert frozenset({ApiGroup.PRODUCT}) == PRODUCT_REGISTRATION_REQUIRED_GROUPS


def test_an_attestation_is_operator_attested_and_never_machine_verified() -> None:
    record = attest([ApiGroup.PRODUCT], _context())
    assert record.evidence_strength is EvidenceStrength.OPERATOR_ATTESTED
    assert record.evidence_source is EvidenceSource.OPERATOR_ATTESTED_PROVIDER_ADMIN
    with pytest.raises(CapabilityInvariantError, match="never MACHINE_VERIFIED"):
        Attestation(
            observed_at=T0,
            application_fingerprint=FINGERPRINT,
            required_groups=PRODUCT_REGISTRATION_REQUIRED_GROUPS,
            observed_groups=frozenset({ApiGroup.PRODUCT}),
            endpoint_mapping_revision=REVISION,
            evidence_strength=EvidenceStrength.MACHINE_VERIFIED,
        )


def test_only_the_observed_groups_come_from_the_operator() -> None:
    later = T0 + timedelta(hours=2)
    record = attest([ApiGroup.PRODUCT, ApiGroup.INQUIRY], _context(now=later))
    assert record.observed_at == later
    assert record.application_fingerprint == FINGERPRINT
    assert record.required_groups == PRODUCT_REGISTRATION_REQUIRED_GROUPS
    assert record.endpoint_mapping_revision == REVISION
    assert record.observed_groups == frozenset({ApiGroup.PRODUCT, ApiGroup.INQUIRY})


def test_the_attested_status_is_positive_evidence_derived_from_the_groups() -> None:
    assert attest([ApiGroup.PRODUCT], _context()).attested_status is WriteScopeStatus.READY
    assert attest(list(ApiGroup), _context()).attested_status is WriteScopeStatus.READY
    # A visibly absent required group is positive absence (§5.2), not "unknown".
    assert attest([ApiGroup.SELLER_INFO], _context()).attested_status is WriteScopeStatus.MISSING
    assert attest([], _context()).attested_status is WriteScopeStatus.MISSING


def test_nothing_is_recorded_without_an_application_or_a_mapping_revision() -> None:
    with pytest.raises(AttestationRefusedError) as missing_application:
        attest([ApiGroup.PRODUCT], _context(application_fingerprint=None))
    assert missing_application.value.reason is Invalidation.APPLICATION_NOT_CONFIGURED
    with pytest.raises(AttestationRefusedError) as missing_revision:
        attest([ApiGroup.PRODUCT], _context(endpoint_mapping_revision=None))
    assert missing_revision.value.reason is Invalidation.MAPPING_REVISION_UNAVAILABLE


def test_free_text_is_not_an_api_group() -> None:
    for text in ("PRODUCT", "상품"):
        with pytest.raises(CapabilityInvariantError, match="ApiGroup"):
            attest([text], _context())


def test_current_evidence_supports_exactly_its_attested_scope() -> None:
    ready = evaluate(attest([ApiGroup.PRODUCT], _context()), _context())
    assert ready.current and ready.freshness is EvidenceFreshness.FRESH
    assert ready.write_scope == WriteScope(
        WriteScopeStatus.READY, EvidenceStrength.OPERATOR_ATTESTED
    )
    assert ready.write_scope.evidence_grade is EvidenceGrade.LIMITED  # ◐, never ●
    missing = evaluate(attest([], _context()), _context())
    assert missing.write_scope.status is WriteScopeStatus.MISSING
    # Still inside the bound at exactly max_age.
    assert evaluate(attest([ApiGroup.PRODUCT], _context()), _context(now=T0 + MAX_AGE)).current


OTHER_FINGERPRINT = application_fingerprint(
    ApplicationIdentity(SMARTSTORE_PROVIDER, SELF_AUTH_MODE, "fixture-client-id-B"), KEY
)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"application_fingerprint": None}, Invalidation.APPLICATION_NOT_CONFIGURED),
        (
            {"application_fingerprint": OTHER_FINGERPRINT},
            Invalidation.APPLICATION_FINGERPRINT_MISMATCH,
        ),
        (
            {"required_groups": frozenset({ApiGroup.PRODUCT, ApiGroup.SELLER_INFO})},
            Invalidation.REQUIRED_GROUPS_CHANGED,
        ),
        ({"endpoint_mapping_revision": None}, Invalidation.MAPPING_REVISION_UNAVAILABLE),
        (
            {"endpoint_mapping_revision": "fixture-mapping-revision-2"},
            Invalidation.MAPPING_REVISION_CHANGED,
        ),
        ({"now": T0 + MAX_AGE + timedelta(seconds=1)}, Invalidation.EXPIRED),
        ({"max_age": None}, Invalidation.NO_AGE_POLICY),
        ({"now": T0 - timedelta(seconds=1)}, Invalidation.MALFORMED),
    ],
    ids=lambda value: value.value if isinstance(value, Invalidation) else "",
)
@pytest.mark.parametrize("observed", [[ApiGroup.PRODUCT], []], ids=["ready", "missing"])
def test_each_invalidation_fails_closed_to_unknown(
    changes: dict[str, object], expected: Invalidation, observed: list[ApiGroup]
) -> None:
    record = attest(observed, _context())
    evaluation = evaluate(record, _context(**changes))
    assert expected in evaluation.invalidations
    assert not evaluation.current
    # Never READY, and never a fabricated MISSING: stale evidence is insufficient evidence (§6.5).
    assert evaluation.write_scope == PERMISSION_UNKNOWN


def test_the_fingerprint_is_keyed_non_reversible_and_bound_to_the_application() -> None:
    assert len(FINGERPRINT) == 64 and "fixture-client-id-A" not in FINGERPRINT
    assert application_fingerprint(IDENTITY, KEY) == FINGERPRINT
    assert FINGERPRINT != OTHER_FINGERPRINT
    assert application_fingerprint(IDENTITY, b"another-fingerprint-key-32-bytes") != FINGERPRINT
    assert (
        application_fingerprint(
            ApplicationIdentity(SMARTSTORE_PROVIDER, "SELLER", "fixture-client-id-A"), KEY
        )
        != FINGERPRINT
    )
    with pytest.raises(CapabilityInvariantError, match="256-bit"):
        application_fingerprint(IDENTITY, b"short")
    with pytest.raises(CapabilityInvariantError):
        ApplicationIdentity(SMARTSTORE_PROVIDER, SELF_AUTH_MODE, " ")


def test_promotion_says_what_the_evidence_did_to_capability_truth() -> None:
    current_ready = evaluate(attest([ApiGroup.PRODUCT], _context()), _context())
    stale = evaluate(attest([ApiGroup.PRODUCT], _context()), _context(max_age=None))
    unknown = PERMISSION_UNKNOWN
    assert promotion(None, unknown, ContractFreshness.CURRENT) is Promotion.NO_EVIDENCE
    assert promotion(stale, unknown, ContractFreshness.CURRENT) is Promotion.NOT_CURRENT
    assert (
        promotion(current_ready, current_ready.write_scope, ContractFreshness.CURRENT)
        is Promotion.APPLIED
    )
    # Stored and current, but READY is new trust the contract freshness does not allow yet (F5).
    for freshness in (
        ContractFreshness.UNRECORDED,
        ContractFreshness.STALE,
        ContractFreshness.REVIEW_REQUIRED,
    ):
        assert (
            promotion(current_ready, unknown, freshness) is Promotion.BLOCKED_BY_CONTRACT_FRESHNESS
        )
    assert promotion(current_ready, unknown, ContractFreshness.CURRENT) is Promotion.PENDING
