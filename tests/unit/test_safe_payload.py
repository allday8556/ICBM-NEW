from datetime import UTC, datetime

import pytest

from app.connect.state import ConnectionState
from app.core.safe_payload import SAFE_FIELDS, UnsafePayloadError, safe_payload


def test_approved_scalars_are_emitted_and_normalised() -> None:
    at = datetime(2026, 9, 13, tzinfo=UTC)
    assert safe_payload(
        supplier_key="kmretail",
        state_from=ConnectionState.READY,
        started_at=at,
        latency_ms=12.5,
        retry_count=0,
        auto_connect=True,
        http_status=None,
        signals=("state_logoff", "login_check_redirect"),
    ) == {
        "supplier_key": "kmretail",
        "state_from": "READY",
        "started_at": at.isoformat(),
        "latency_ms": 12.5,
        "retry_count": 0,
        "auto_connect": True,
        "http_status": None,
        "signals": ["state_logoff", "login_check_redirect"],
    }


@pytest.mark.parametrize("field", ["password", "cookie", "headers", "session", "username", "body"])
def test_any_field_not_on_the_allowlist_is_rejected(field: str) -> None:
    assert field not in SAFE_FIELDS
    with pytest.raises(UnsafePayloadError, match=field):
        safe_payload(**{field: "x"})


@pytest.mark.parametrize(
    "value",
    [
        {"Cookie": "a=b"},
        b"raw-bytes",
        ["contains space"],
        ("<html>",),
        "x" * 201,
        "line\nbreak",
        object(),
    ],
)
def test_structured_or_free_text_values_are_rejected(value: object) -> None:
    with pytest.raises(UnsafePayloadError):
        safe_payload(signals=value) if isinstance(value, list | tuple) else safe_payload(
            error_code=value
        )
