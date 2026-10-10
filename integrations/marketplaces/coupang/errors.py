"""Evidence-bounded Coupang authentication failure classification (C-AUTH-1)."""

from dataclasses import dataclass
from enum import StrEnum

from app.platform.core.errors import ErrorClass


class Basis(StrEnum):
    OFFICIAL_EXACT = "OFFICIAL_EXACT"
    OFFICIAL_CONTEXTUAL = "OFFICIAL_CONTEXTUAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Classification:
    error_class: ErrorClass
    code: str
    basis: Basis


def classify_auth_response(status: int, provider_message: str | None) -> Classification:
    """Classify only the cases supported by Coupang's official auth FAQ.

    Provider text is transient evidence and is not retained. An unrelated 403 is not called an IP
    failure, and an unrelated 4xx is not promoted to credential invalidity.
    """

    message = " ".join(provider_message.lower().split()) if provider_message else ""
    if status == 401 and "specified signature is expired" in message:
        return Classification(ErrorClass.AUTH, "COUPANG_SIGNATURE_EXPIRED", Basis.OFFICIAL_EXACT)
    if status == 401:
        return Classification(ErrorClass.AUTH, "COUPANG_AUTH_REJECTED", Basis.OFFICIAL_CONTEXTUAL)
    if status == 403 and "not allowed ip" in message:
        return Classification(
            ErrorClass.POLICY_BLOCKED, "COUPANG_IP_NOT_ALLOWED", Basis.OFFICIAL_EXACT
        )
    return Classification(ErrorClass.UNKNOWN, f"COUPANG_HTTP_{status}", Basis.UNKNOWN)
