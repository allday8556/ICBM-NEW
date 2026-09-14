"""SmartStore failure classification (ERRORS.md §5, §8-§10, §18; Issue #39 §6)."""

import pytest

from app.core.errors import ErrorClass
from integrations.marketplaces.smartstore import classify
from integrations.marketplaces.smartstore.classify import Basis, FailureLayer
from integrations.marketplaces.smartstore.transmission import Phase

NOT_FOUND_CODES = (
    "CHANNEL_NOT_FOUND",
    "STORE_NOT_FOUND",
    "REPRESENT_NOT_FOUND",
    "MEMBER_NOT_FOUND",
    "INTERLOCK_NOT_FOUND",
)
SELLER_ACCOUNT_CODES = (
    "GENERAL_ERROR",
    "UNAUTHORIZED",
    "ROLE_NOT_FOUND",
    "PROVISION_NOT_FOUND",
    "INVALID_CHANNEL_STATUS",
    "INVALID_STORE_STATUS",
    "INVALID_REPRESENT_STATUS",
    "INVALID_MEMBER_STATUS",
    "INVALID_INTERLOCK_STATUS",
    "RESOURCE_NOT_AVAILABLE",
    *NOT_FOUND_CODES,
    "PARSING_FAIL",
    "SERDES_FAIL",
    "ENCDEC_FAIL",
)
GATEWAY_CODES = (
    "GW.AUTHN",
    "GW.IP_NOT_ALLOWED",
    "GW.NOT_FOUND",
    "GW.RATE_LIMIT",
    "GW.QUOTA_LIMIT",
    *(f"GW.PROXY.0{n}" for n in range(1, 6)),
    "GW.INTERNAL_SERVER_ERROR",
    "GW.BLOCK.01",
    "GW.BLOCK.02",
    "GW.TIMEOUT.01",
    "GW.TIMEOUT.02",
)
ALL_CODES = (None, "NOT_FOUND", "BAD_REQUEST", "FORBIDDEN", *SELLER_ACCOUNT_CODES, *GATEWAY_CODES)


@pytest.mark.parametrize("code", [None, "NOT_FOUND", "GW.NOT_FOUND", *NOT_FOUND_CODES])
def test_a_provider_404_is_never_the_icbm_local_not_found(code: str | None) -> None:
    classification = classify.response(404, code)
    assert classification.error_class is ErrorClass.UNKNOWN
    assert classification.error_class is not ErrorClass.NOT_FOUND


def test_no_provider_status_or_code_ever_classifies_as_not_found() -> None:
    # ADR-0008 C: NOT_FOUND is ICBM-local only. Sweep every status and every documented code.
    produced = {
        classify.response(status, code).error_class
        for status in range(100, 600)
        for code in ALL_CODES
    }
    assert ErrorClass.NOT_FOUND not in produced
    # Without corroborating evidence PR-A never asserts AUTH, VALIDATION or POLICY_BLOCKED.
    assert produced == {ErrorClass.UNKNOWN, ErrorClass.TRANSIENT, ErrorClass.RATE_LIMITED}


def test_the_class_is_not_an_unconditional_status_table() -> None:
    # One status, different classes by provider evidence; one class, different statuses.
    assert {classify.response(403, c).error_class for c in ("ROLE_NOT_FOUND", "GW.RATE_LIMIT")} == {
        ErrorClass.UNKNOWN,
        ErrorClass.RATE_LIMITED,
    }
    assert {classify.response(500, c).error_class for c in ("PARSING_FAIL", "GW.AUTHN")} == {
        ErrorClass.TRANSIENT,
        ErrorClass.UNKNOWN,
    }


@pytest.mark.parametrize(
    ("status", "code", "error_class", "layer"),
    [
        (400, "GENERAL_ERROR", ErrorClass.UNKNOWN, FailureLayer.API_SERVER_DOMAIN),
        (400, None, ErrorClass.UNKNOWN, FailureLayer.API_SERVER_STANDARD),
        (401, "UNAUTHORIZED", ErrorClass.UNKNOWN, FailureLayer.API_SERVER_DOMAIN),
        (403, "PROVISION_NOT_FOUND", ErrorClass.UNKNOWN, FailureLayer.API_SERVER_DOMAIN),
        (403, "INVALID_STORE_STATUS", ErrorClass.UNKNOWN, FailureLayer.API_SERVER_DOMAIN),
        (500, "GENERAL_ERROR", ErrorClass.TRANSIENT, FailureLayer.API_SERVER_DOMAIN),
        (503, None, ErrorClass.TRANSIENT, FailureLayer.API_SERVER_STANDARD),
        (429, None, ErrorClass.RATE_LIMITED, FailureLayer.API_SERVER_STANDARD),
        (401, "GW.AUTHN", ErrorClass.UNKNOWN, FailureLayer.GATEWAY),
        (401, "GW.IP_NOT_ALLOWED", ErrorClass.UNKNOWN, FailureLayer.GATEWAY),
        (404, "GW.NOT_FOUND", ErrorClass.UNKNOWN, FailureLayer.GATEWAY),
        (429, "GW.RATE_LIMIT", ErrorClass.RATE_LIMITED, FailureLayer.GATEWAY),
        (429, "GW.QUOTA_LIMIT", ErrorClass.RATE_LIMITED, FailureLayer.GATEWAY),
        (502, "GW.PROXY.03", ErrorClass.TRANSIENT, FailureLayer.GATEWAY),
        (500, "GW.INTERNAL_SERVER_ERROR", ErrorClass.TRANSIENT, FailureLayer.GATEWAY),
        (503, "GW.BLOCK.02", ErrorClass.TRANSIENT, FailureLayer.GATEWAY),
        (504, "GW.TIMEOUT.01", ErrorClass.TRANSIENT, FailureLayer.GATEWAY),
    ],
)
def test_documented_responses_map_by_layer_and_evidence(
    status: int, code: str | None, error_class: ErrorClass, layer: FailureLayer
) -> None:
    classification = classify.response(status, code)
    assert (classification.error_class, classification.layer) == (error_class, layer)
    assert classification.provider_code == code


@pytest.mark.parametrize("status", [200, 201, 204])
def test_a_2xx_that_fails_its_predicate_is_contract_drift(status: int) -> None:
    classification = classify.response(status, None)
    assert classification.error_class is ErrorClass.UNKNOWN
    assert classification.layer is FailureLayer.OPERATION_RESULT
    assert classification.code == "SMARTSTORE_SUCCESS_PREDICATE_FAILED"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_contract_evidence_never_a_result(status: int) -> None:
    classification = classify.response(status, None)
    assert classification.error_class is ErrorClass.UNKNOWN
    assert classification.code == "SMARTSTORE_UNEXPECTED_REDIRECT"


def test_icbm_egress_policy_is_policy_blocked() -> None:
    classification = classify.transport(Phase.EGRESS_BLOCKED, known_transport_failure=True)
    assert classification.error_class is ErrorClass.POLICY_BLOCKED


@pytest.mark.parametrize("phase", [p for p in Phase if p is not Phase.EGRESS_BLOCKED])
def test_a_known_transport_failure_is_transient_and_an_unknown_one_is_unknown(
    phase: Phase,
) -> None:
    known = classify.transport(phase, known_transport_failure=True)
    assert (known.error_class, known.layer) == (ErrorClass.TRANSIENT, FailureLayer.TRANSPORT)
    unknown = classify.transport(phase, known_transport_failure=False)
    assert (unknown.error_class, unknown.basis) == (ErrorClass.UNKNOWN, Basis.UNKNOWN)


def test_a_local_contract_defect_is_fatal() -> None:
    classification = classify.local_contract("SMARTSTORE_ENDPOINT_NOT_ADOPTED")
    assert classification.error_class is ErrorClass.FATAL
    assert classification.layer is FailureLayer.LOCAL_CONTRACT
