import pytest

from app.core.errors import (
    AUTO_RETRYABLE,
    HTTP_STATUS,
    ErrorClass,
    PolicyBlockedError,
    RateLimitedError,
    TransientError,
    classify,
)


def test_only_transient_and_rate_limited_are_auto_retryable() -> None:
    assert frozenset({ErrorClass.TRANSIENT, ErrorClass.RATE_LIMITED}) == AUTO_RETRYABLE


def test_every_error_class_maps_to_an_http_status() -> None:
    assert set(HTTP_STATUS) == set(ErrorClass)


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
