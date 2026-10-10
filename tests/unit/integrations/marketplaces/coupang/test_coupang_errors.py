"""Only officially evidenced Coupang auth failures receive auth-specific meanings."""

import pytest

from app.platform.core.errors import ErrorClass
from integrations.marketplaces.coupang.errors import Basis, classify_auth_response


@pytest.mark.parametrize(
    ("status", "message", "error_class", "code", "basis"),
    [
        (
            401,
            "Specified signature is expired",
            ErrorClass.AUTH,
            "COUPANG_SIGNATURE_EXPIRED",
            Basis.OFFICIAL_EXACT,
        ),
        (
            401,
            "invalid signature",
            ErrorClass.AUTH,
            "COUPANG_AUTH_REJECTED",
            Basis.OFFICIAL_CONTEXTUAL,
        ),
        (
            403,
            "[FORBIDDEN] Not allowed IP",
            ErrorClass.POLICY_BLOCKED,
            "COUPANG_IP_NOT_ALLOWED",
            Basis.OFFICIAL_EXACT,
        ),
        (403, "other forbidden", ErrorClass.UNKNOWN, "COUPANG_HTTP_403", Basis.UNKNOWN),
        (400, "invalid field", ErrorClass.UNKNOWN, "COUPANG_HTTP_400", Basis.UNKNOWN),
        (429, None, ErrorClass.UNKNOWN, "COUPANG_HTTP_429", Basis.UNKNOWN),
        (503, None, ErrorClass.UNKNOWN, "COUPANG_HTTP_503", Basis.UNKNOWN),
    ],
)
def test_auth_response_classification_is_evidence_bounded(
    status: int,
    message: str | None,
    error_class: ErrorClass,
    code: str,
    basis: Basis,
) -> None:
    answer = classify_auth_response(status, message)
    assert (answer.error_class, answer.code, answer.basis) == (error_class, code, basis)


def test_provider_text_is_not_retained_in_the_classification() -> None:
    marker = "sensitive-fixture-text"
    answer = classify_auth_response(403, marker)

    assert marker not in repr(answer)
