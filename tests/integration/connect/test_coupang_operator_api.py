"""Coupang credential setup through Settings' local API, with no provider call."""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.container import Container
from app.stages.connect.coupang.credentials import CoupangCredentialStore

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
ENDPOINT = "/api/v1/connect/marketplaces/coupang/credentials"
VENDOR_ID = "A00FIXTURE"
ACCESS_KEY = "fixture-access-key"
SECRET_KEY = "fixture-secret-key"


def _container(client: TestClient) -> Container:
    return client.app.state.container  # type: ignore[no-any-return,attr-defined]


def test_settings_saves_one_atomic_os_credential_bundle_without_returning_it(
    client: TestClient,
) -> None:
    assert client.get(ENDPOINT).json() == {
        "configured": False,
        "credential_generation": None,
    }

    payload = {
        "vendor_id": VENDOR_ID,
        "access_key": ACCESS_KEY,
        "secret_key": SECRET_KEY,
    }
    refused = client.put(ENDPOINT, json=payload)
    assert refused.status_code == 403
    assert ACCESS_KEY not in refused.text and SECRET_KEY not in refused.text

    saved = client.put(ENDPOINT, json=payload, headers=CLIENT)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"configured": True, "credential_generation": 1}
    assert ACCESS_KEY not in saved.text and SECRET_KEY not in saved.text
    assert client.get(ENDPOINT).json() == saved.json()

    committed = CoupangCredentialStore(_container(client).secrets).load()
    assert committed is not None
    assert (
        committed.vendor_id,
        committed.access_key,
        committed.secret_key,
        committed.credential_generation,
    ) == (VENDOR_ID, ACCESS_KEY, SECRET_KEY, 1)


def test_replacement_increments_generation_and_audit_contains_no_credentials(
    client: TestClient,
) -> None:
    first = {
        "vendor_id": VENDOR_ID,
        "access_key": ACCESS_KEY,
        "secret_key": SECRET_KEY,
    }
    second = {
        "vendor_id": "A00FIXTURE2",
        "access_key": "fixture-access-key-2",
        "secret_key": "fixture-secret-key-2",
    }
    assert client.put(ENDPOINT, json=first, headers=CLIENT).json()["credential_generation"] == 1
    assert client.put(ENDPOINT, json=second, headers=CLIENT).json()["credential_generation"] == 2

    container = _container(client)
    with sqlite3.connect(container.config.database_path) as raw:
        rows = raw.execute(
            "SELECT target_ref, details_json FROM audit_events"
            " WHERE event_type = 'MARKETPLACE_CREDENTIALS_UPDATED'"
            " ORDER BY seq"
        ).fetchall()
    assert len(rows) == 2
    assert [json.loads(details)["credential_generation"] for _, details in rows] == [1, 2]
    joined = json.dumps(rows)
    for forbidden in (*first.values(), *second.values()):
        assert forbidden not in joined


def test_invalid_bundle_is_refused_without_replacing_the_committed_generation(
    client: TestClient,
) -> None:
    valid = {
        "vendor_id": VENDOR_ID,
        "access_key": ACCESS_KEY,
        "secret_key": SECRET_KEY,
    }
    assert client.put(ENDPOINT, json=valid, headers=CLIENT).status_code == 200
    invalid = {**valid, "secret_key": ""}
    refused = client.put(ENDPOINT, json=invalid, headers=CLIENT)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "COUPANG_CREDENTIALS_INVALID"
    assert client.get(ENDPOINT).json() == {"configured": True, "credential_generation": 1}
