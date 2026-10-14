"""ADR-0038 §3, §5: the pure rules of the overseas capability.

Proven here:
- business days are Monday to Friday, with no holiday calendar, and FX-03 turns a snapshot stale
  only after more than three of them; no snapshot (or one from after the day) is unavailable;
- the KST day of a moment;
- a percentage is exact decimal text, stored as basis points and shown canonically; a save request
  is refused whole when any value is out of its bounds, and nothing is defaulted.
"""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from app.capabilities.overseas.fx import (
    PRICING_FX_STALE,
    PRICING_FX_UNAVAILABLE,
    FxSnapshot,
    business_days_after,
    fx_reason,
    kst_day,
)
from app.capabilities.overseas.settings import (
    OverseasSettings,
    SaveOverseasSettingsRequest,
    bp_to_percent,
    content_fingerprint,
    percent_to_bp,
)

MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 10, d) for d in range(12, 19))
NEXT_MON, NEXT_TUE, NEXT_WED, NEXT_THU = (date(2026, 10, d) for d in range(19, 23))


def _snapshot(published: date) -> FxSnapshot:
    return FxSnapshot(
        snapshot_id="s-1",
        publication_date=published,
        currency="JPY",
        rate_type="TTS",
        source_code="JPY(100)",
        unit=100,
        tts="938.4",
        deal_bas_r="929.11",
        source="koreaexim/AP01",
        fetched_at=datetime(2026, 10, 12, 3, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("start", "end", "count"),
    [
        (MON, MON, 0),
        (MON, TUE, 1),
        (MON, FRI, 4),
        (FRI, SAT, 0),
        (FRI, SUN, 0),
        (FRI, NEXT_MON, 1),
        (THU, NEXT_MON, 2),
        (MON, NEXT_MON, 5),
        (MON, date(2026, 11, 9), 20),
        (SAT, NEXT_MON, 1),
        (TUE, MON, 0),
    ],
)
def test_business_days_are_monday_to_friday(start: date, end: date, count: int) -> None:
    assert business_days_after(start, end) == count


@pytest.mark.parametrize(
    ("published", "today", "reason"),
    [
        (MON, MON, None),
        (MON, THU, None),
        (MON, FRI, PRICING_FX_STALE),
        (FRI, NEXT_WED, None),
        (FRI, NEXT_THU, PRICING_FX_STALE),
        (THU, NEXT_TUE, None),
        (WED, NEXT_MON, None),
        (WED, NEXT_TUE, PRICING_FX_STALE),
        # A weekend adds nothing: Friday's rate is fresh on Saturday and Sunday.
        (FRI, SUN, None),
        # A rate from after the day is no rate known on it.
        (TUE, MON, PRICING_FX_UNAVAILABLE),
    ],
)
def test_fx03_staleness(published: date, today: date, reason: str | None) -> None:
    assert fx_reason(_snapshot(published), today) == reason


def test_no_snapshot_is_unavailable() -> None:
    assert fx_reason(None, MON) == PRICING_FX_UNAVAILABLE


def test_the_kst_day_of_a_moment() -> None:
    assert kst_day(datetime(2026, 10, 14, 14, 59, tzinfo=UTC)) == WED
    assert kst_day(datetime(2026, 10, 14, 15, 0, tzinfo=UTC)) == THU


def test_a_jpy_rate_divides_by_its_unit_exactly() -> None:
    assert str(_snapshot(MON).tts_per_unit()) == "9.384"


@pytest.mark.parametrize(
    ("text", "bp", "shown"),
    [
        ("0", 0, "0"),
        ("5", 500, "5"),
        ("5.5", 550, "5.5"),
        ("5.50", 550, "5.5"),
        ("5.05", 505, "5.05"),
        ("12.34", 1234, "12.34"),
        ("100", 10000, "100"),
    ],
)
def test_a_percentage_is_basis_points_shown_canonically(text: str, bp: int, shown: str) -> None:
    assert percent_to_bp(text) == bp
    assert bp_to_percent(bp) == shown


def _request(**override: Any) -> dict[str, Any]:
    return {
        "expected_revision_no": None,
        "fx_margin_percent": "5",
        "overseas_shipping_default_krw": None,
        "duties_included_over_usd150": True,
        "duty_rate_percent": "8",
        "smartstore_overseas_shipping_address_id": None,
        "overseas_delivery_days_min": 7,
        "overseas_delivery_days_max": 14,
        "importer_label": None,
        **override,
    }


def test_a_valid_request_is_read_and_its_text_trimmed() -> None:
    request = SaveOverseasSettingsRequest(
        **_request(
            fx_margin_percent="5.25",
            overseas_shipping_default_krw=0,
            smartstore_overseas_shipping_address_id=" 100234 ",
            importer_label="  ICBM 상점  ",
            overseas_delivery_days_min=10,
            overseas_delivery_days_max=10,
        )
    )
    assert request.smartstore_overseas_shipping_address_id == "100234"
    assert request.importer_label == "ICBM 상점"


@pytest.mark.parametrize(
    "override",
    [
        {"fx_margin_percent": "5.123"},
        {"fx_margin_percent": "100.01"},
        {"fx_margin_percent": "-1"},
        {"fx_margin_percent": "05"},
        {"fx_margin_percent": "5%"},
        {"fx_margin_percent": 5},
        {"fx_margin_percent": 5.0},
        {"duty_rate_percent": "101"},
        {"overseas_shipping_default_krw": -1},
        {"overseas_shipping_default_krw": 10_000_001},
        {"overseas_shipping_default_krw": "3000"},
        {"overseas_shipping_default_krw": 3000.0},
        {"duties_included_over_usd150": "yes"},
        {"overseas_delivery_days_min": 0},
        {"overseas_delivery_days_max": 61},
        {"overseas_delivery_days_min": 15, "overseas_delivery_days_max": 14},
        {"smartstore_overseas_shipping_address_id": "abc"},
        {"smartstore_overseas_shipping_address_id": "   "},
        {"smartstore_overseas_shipping_address_id": "1" * 21},
        {"importer_label": ""},
        {"importer_label": "가" * 51},
        {"expected_revision_no": "1"},
        {"surprise": True},
    ],
)
def test_an_out_of_bounds_request_is_refused_whole(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        SaveOverseasSettingsRequest(**_request(**override))


@pytest.mark.parametrize("missing", ["fx_margin_percent", "importer_label", "expected_revision_no"])
def test_nothing_is_defaulted_by_the_server(missing: str) -> None:
    values = _request()
    del values[missing]
    with pytest.raises(ValidationError):
        SaveOverseasSettingsRequest(**values)


def test_the_defaults_and_the_fingerprint() -> None:
    defaults = OverseasSettings()
    assert defaults.content() == {
        "fx_margin_bp": 500,
        "overseas_shipping_default_krw": None,
        "duties_included_over_usd150": True,
        "duty_rate_bp": 800,
        "smartstore_overseas_shipping_address_id": None,
        "overseas_delivery_days_min": 7,
        "overseas_delivery_days_max": 14,
        "importer_label": None,
    }
    assert defaults.pin() is None
    same = content_fingerprint(defaults.content())
    assert len(same) == 64 and same == content_fingerprint(OverseasSettings().content())
    assert same != content_fingerprint(OverseasSettings(fx_margin_bp=501).content())
