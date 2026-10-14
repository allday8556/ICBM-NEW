"""ADR-0038 §5: the 해외 구매대행 settings owner on a migrated database.

Proven here:
- with no revision the defaults apply and nothing is seeded;
- a save appends one audited revision with exact basis points; a save against a moved revision
  or one that changes nothing is refused and writes nothing;
- the revisions are append-only, numbered without gaps, and the database refuses out-of-bounds
  values on its own;
- the API reads and saves through the CSRF guard, refuses an invalid request whole, and the
  settings contract lists the surface as editable.
"""

import contextlib
import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.overseas.settings import (
    OVERSEAS_SETTINGS_REVISION_MOVED,
    OVERSEAS_SETTINGS_UNCHANGED,
    OverseasSettingsConflictError,
    SaveOverseasSettingsRequest,
)
from app.config import AppConfig
from app.container import Container
from tests.support.product_support import count, raw

pytestmark = pytest.mark.integration

HEADERS = {"X-ICBM-Client": "pytest"}
OPERATOR = "operator:local"
CID = "cid-overseas"
PATH = "/api/v1/settings/overseas"
TABLE = "overseas_settings_revisions"


def _body(**override: Any) -> dict[str, Any]:
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


def _save(container: Container, **override: Any) -> Any:
    return container.overseas_settings.save(
        SaveOverseasSettingsRequest(**_body(**override)), actor=OPERATOR, correlation_id=CID
    )


def test_the_defaults_apply_with_no_revision(container: Container, config: AppConfig) -> None:
    view = container.overseas_settings.current()
    assert view.model_dump(exclude={"changed_at"}) == {
        "surface": "OVERSEAS_SETTINGS",
        "editable": True,
        "fx_margin_percent": "5",
        "overseas_shipping_default_krw": None,
        "duties_included_over_usd150": True,
        "duty_rate_percent": "8",
        "smartstore_overseas_shipping_address_id": None,
        "overseas_delivery_days_min": 7,
        "overseas_delivery_days_max": 14,
        "importer_label": None,
        "revision_no": None,
        "fingerprint": None,
        "changed_by": None,
    }
    assert count(config, TABLE) == 0


def test_a_save_appends_one_audited_revision(container: Container, config: AppConfig) -> None:
    saved = _save(
        container,
        fx_margin_percent="5.5",
        overseas_shipping_default_krw=6000,
        duties_included_over_usd150=False,
        duty_rate_percent="13",
        smartstore_overseas_shipping_address_id="100234567",
        overseas_delivery_days_min=10,
        overseas_delivery_days_max=20,
        importer_label="ICBM 상점",
    )
    assert (saved.revision_no, saved.changed_by, saved.fx_margin_percent) == (1, OPERATOR, "5.5")
    assert saved.fingerprint is not None and saved.changed_at is not None
    with contextlib.closing(raw(config)) as connection:
        (row,) = connection.execute(
            f"SELECT fx_margin_bp, duty_rate_bp, overseas_shipping_default_krw,"
            f" duties_included_over_usd150, importer_label FROM {TABLE}"
        ).fetchall()
        events = connection.execute(
            "SELECT actor, target_ref, correlation_id, after_json FROM audit_events"
            " WHERE event_type = 'OVERSEAS_SETTINGS_REVISED'"
        ).fetchall()
    assert row == (550, 1300, 6000, 0, "ICBM 상점")
    ((actor, target, cid, after),) = events
    assert (actor, target, cid) == (OPERATOR, "OVERSEAS_SETTINGS", CID)
    assert json.loads(after)["fx_margin_bp"] == 550 and json.loads(after)["revision_no"] == 1
    second = _save(container, expected_revision_no=1, fx_margin_percent="6")
    assert (second.revision_no, second.fx_margin_percent, second.importer_label) == (2, "6", None)
    assert container.overseas_settings.current().revision_no == 2


def test_a_moved_or_unchanged_save_is_refused(container: Container, config: AppConfig) -> None:
    with pytest.raises(OverseasSettingsConflictError) as unchanged:
        _save(container)  # exactly the defaults
    assert unchanged.value.code == OVERSEAS_SETTINGS_UNCHANGED
    # "5.00" is the same 5%: still unchanged.
    with pytest.raises(OverseasSettingsConflictError):
        _save(container, fx_margin_percent="5.00")
    _save(container, fx_margin_percent="7")
    for stale in (None, 2):
        with pytest.raises(OverseasSettingsConflictError) as moved:
            _save(container, expected_revision_no=stale, fx_margin_percent="8")
        assert moved.value.code == OVERSEAS_SETTINGS_REVISION_MOVED
        assert moved.value.details == {"current_revision_no": 1}
    assert count(config, TABLE) == 1
    assert count(config, "audit_events", "event_type = 'OVERSEAS_SETTINGS_REVISED'") == 1


def test_the_revisions_are_append_only_and_bounded(container: Container, config: AppConfig) -> None:
    _save(container, fx_margin_percent="7")
    insert = (
        f"INSERT INTO {TABLE} (revision_id, revision_no, fx_margin_bp,"
        " overseas_shipping_default_krw, duties_included_over_usd150, duty_rate_bp,"
        " smartstore_overseas_shipping_address_id, overseas_delivery_days_min,"
        " overseas_delivery_days_max, importer_label, fingerprint, changed_by, correlation_id,"
        " changed_at) VALUES (:id, :no, :margin, NULL, 1, 800, :address, :low, :high, :label,"
        " :fp, 'x', 'cid', '2026-10-14 03:00:00')"
    )
    good = {
        "id": "r-x",
        "no": 2,
        "margin": 500,
        "address": None,
        "low": 7,
        "high": 14,
        "label": None,
        "fp": "a" * 64,
    }
    with contextlib.closing(raw(config)) as connection:
        for statement in (f"UPDATE {TABLE} SET fx_margin_bp = 1", f"DELETE FROM {TABLE}"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
        for bad in (
            {"no": 3},  # a gap in the numbering
            {"margin": 10001},
            {"margin": -1},
            {"low": 0},
            {"low": 15},
            {"high": 61},
            {"address": "12a"},
            {"label": ""},
            {"fp": "Z" * 64},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, good | bad)
        connection.execute(insert, good)  # the same row, in bounds, is accepted
        connection.rollback()
    assert count(config, TABLE) == 1


def test_the_api_reads_saves_and_refuses(client: TestClient, config: AppConfig) -> None:
    read = client.get(PATH)
    assert read.status_code == 200, read.text
    assert (read.json()["revision_no"], read.json()["fx_margin_percent"]) == (None, "5")
    body = _body(fx_margin_percent="4.75", importer_label="수입자")
    # The global CSRF guard: no client header, no write.
    assert client.put(PATH, json=body).status_code == 403
    assert count(config, TABLE) == 0
    for invalid in (
        body | {"fx_margin_percent": "4.755"},
        body | {"fx_margin_percent": 4.75},
        body | {"overseas_delivery_days_min": 20, "overseas_delivery_days_max": 10},
        body | {"smartstore_overseas_shipping_address_id": "addr-1"},
        body | {"surprise": 1},
        {key: value for key, value in body.items() if key != "duty_rate_percent"},
    ):
        refused = client.put(PATH, json=invalid, headers=HEADERS)
        assert refused.status_code == 422, invalid
        assert refused.json()["error"]["code"] == "REQUEST_INVALID"
    saved = client.put(PATH, json=body, headers=HEADERS)
    assert saved.status_code == 200, saved.text
    assert (saved.json()["revision_no"], saved.json()["fx_margin_percent"]) == (1, "4.75")
    assert saved.json()["changed_by"] == config.operator_actor
    moved = client.put(PATH, json=body | {"fx_margin_percent": "3"}, headers=HEADERS)
    assert moved.status_code == 409
    assert moved.json()["error"]["code"] == OVERSEAS_SETTINGS_REVISION_MOVED
    unchanged = client.put(PATH, json=body | {"expected_revision_no": 1}, headers=HEADERS)
    assert unchanged.status_code == 409
    assert unchanged.json()["error"]["code"] == OVERSEAS_SETTINGS_UNCHANGED
    assert count(config, TABLE) == 1
    settings = client.get("/api/v1/screens/settings").json()
    assert "OVERSEAS_SETTINGS" in settings["editable_surfaces"]
