import pytest

from app.core.errors import (
    AUTO_RETRYABLE,
    HTTP_STATUS,
    ErrorClass,
    PolicyBlockedError,
    RateLimitedError,
    TransientError,
    classify,
    error_envelope,
)

ADDED_BY_ADR_0008 = (
    ErrorClass.CONFLICT,
    ErrorClass.DUPLICATE,
    ErrorClass.REVIEW_REQUIRED,
    ErrorClass.FATAL,
)


def test_the_taxonomy_is_the_adr_0008_aligned_set_in_order() -> None:
    assert [c.value for c in ErrorClass] == [
        "TRANSIENT",
        "RATE_LIMITED",
        "AUTH",
        "VALIDATION",
        "POLICY_BLOCKED",
        "NOT_FOUND",
        "CONFLICT",
        "DUPLICATE",
        "REVIEW_REQUIRED",
        "FATAL",
        "UNKNOWN",
    ]


def test_only_transient_and_rate_limited_are_auto_retryable() -> None:
    assert frozenset({ErrorClass.TRANSIENT, ErrorClass.RATE_LIMITED}) == AUTO_RETRYABLE


def test_every_error_class_maps_to_its_adr_0008_http_status() -> None:
    assert {
        ErrorClass.TRANSIENT: 503,
        ErrorClass.RATE_LIMITED: 429,
        ErrorClass.AUTH: 401,
        ErrorClass.VALIDATION: 422,
        ErrorClass.POLICY_BLOCKED: 403,
        ErrorClass.NOT_FOUND: 404,
        ErrorClass.CONFLICT: 409,
        ErrorClass.DUPLICATE: 409,
        ErrorClass.REVIEW_REQUIRED: 409,
        ErrorClass.FATAL: 500,
        ErrorClass.UNKNOWN: 500,
    } == HTTP_STATUS


@pytest.mark.parametrize("error_class", ADDED_BY_ADR_0008)
def test_the_classes_added_by_adr_0008_are_never_retryable(error_class: ErrorClass) -> None:
    assert error_class not in AUTO_RETRYABLE
    body = error_envelope(error_class, "X", "m", None)
    assert (body["error"]["class"], body["error"]["retryable"]) == (error_class.value, False)


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (TransientError("X", "m"), True),
        (RateLimitedError("X", "m"), True),
        (PolicyBlockedError("X", "m"), False),
    ],
)
def test_retryable_follows_the_class(error: TransientError, retryable: bool) -> None:
    assert error.retryable is retryable


def test_non_contract_exceptions_classify_as_unknown() -> None:
    error_class, code, message = classify(ValueError("boom"))
    assert error_class is ErrorClass.UNKNOWN
    assert code == "UNHANDLED_EXCEPTION"
    assert "boom" in message


def test_envelope_shape() -> None:
    body = PolicyBlockedError("M0_LIVE_FORBIDDEN", "no", details={"a": 1}).envelope("cid-12345678")
    assert body == {
        "error": {
            "class": "POLICY_BLOCKED",
            "code": "M0_LIVE_FORBIDDEN",
            "message": "no",
            "retryable": False,
            "correlation_id": "cid-12345678",
            "details": {"a": 1},
        }
    }
