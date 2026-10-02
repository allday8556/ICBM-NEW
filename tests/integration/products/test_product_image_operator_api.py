"""The operator's image decisions over HTTP (M4 PR-E, ADR-0013 §9; owner decision 2026-10-03).

The routes show the CONFIRMED source images of an Item's current bound revision, serve exactly
those bytes, and hand an operator's complete selection and QA verdict to the image owner, which
decides every rule as before. Nothing is selected on an operator's behalf.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from tests.support.product_support import Collections, product

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}


@pytest.fixture
def item(client: TestClient, config: AppConfig) -> str:
    services: Container = client.app.state.container  # type: ignore[attr-defined]
    run_id, _revision = Collections.of(services, config).collect(product())
    result = services.materializer.materialize_run(run_id)
    assert result.item_id is not None
    return result.item_id


def _candidates(client: TestClient, item_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/products/items/{item_id}/image-candidates")
    assert response.status_code == 200, response.text
    return response.json()


def _select_all(client: TestClient, item_id: str, found: dict[str, Any]) -> Any:
    first = found["images"][0]
    return client.post(
        f"/api/v1/products/items/{item_id}/image-selection",
        json={
            "source_revision_id": found["source_revision_id"],
            "decisions": [
                {
                    "role": image["role"],
                    "ordinal": image["ordinal"],
                    "sha256": image["sha256"],
                    "decision": "USE_SOURCE",
                }
                for image in found["images"]
            ],
            "outputs": [
                {
                    "role": first["role"],
                    "source_role": first["role"],
                    "source_ordinal": first["ordinal"],
                }
            ],
            "actor": "operator",
        },
        headers=CLIENT,
    )


def test_the_candidates_are_the_current_bound_revision_and_nothing_is_selected(
    client: TestClient, item: str
) -> None:
    found = _candidates(client, item)
    assert found["source_revision_id"]
    assert found["images"] and all(image["qa_verdict"] is None for image in found["images"])
    # Nothing selects an image by default.
    assert found["current_selection"] is None


def test_only_a_candidate_image_is_served(client: TestClient, item: str) -> None:
    sha = _candidates(client, item)["images"][0]["sha256"]
    served = client.get(f"/api/v1/products/items/{item}/images/{sha}")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/")
    assert served.headers["cache-control"] == "no-store"
    other = client.get(f"/api/v1/products/items/{item}/images/{'0' * 64}")
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "PRODUCTS_IMAGE_NOT_A_CANDIDATE"


def test_a_complete_operator_selection_and_a_qa_verdict_are_recorded_by_the_owner(
    client: TestClient, item: str
) -> None:
    found = _candidates(client, item)
    recorded = _select_all(client, item, found)
    assert recorded.status_code == 200, recorded.text
    selection = recorded.json()
    assert selection["decided_by"] == "operator" and selection["revision_no"] == 1
    sha = found["images"][0]["sha256"]
    qa = client.post(
        "/api/v1/products/image-qa",
        json={
            "asset_kind": "SOURCE_ASSET",
            "sha256": sha,
            "validated_source_revision_id": found["source_revision_id"],
            "verdict": "PASS",
            "actor": "operator",
        },
        headers=CLIENT,
    )
    assert qa.status_code == 200, qa.text
    assert qa.json()["verdict"] == "PASS"
    after = _candidates(client, item)
    assert after["current_selection"]["selection_revision_id"] == selection["selection_revision_id"]
    assert {i["sha256"]: i["qa_verdict"] for i in after["images"]}[sha] == "PASS"


def test_the_owner_still_refuses_an_incomplete_selection_and_a_conflicting_verdict(
    client: TestClient, item: str
) -> None:
    found = _candidates(client, item)
    incomplete = client.post(
        f"/api/v1/products/items/{item}/image-selection",
        json={
            "source_revision_id": found["source_revision_id"],
            "decisions": [],
            "outputs": [],
            "actor": "operator",
        },
        headers=CLIENT,
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["code"] == "PRODUCTS_IMAGE_SELECTION_INCOMPLETE"
    body = {
        "asset_kind": "SOURCE_ASSET",
        "sha256": found["images"][0]["sha256"],
        "validated_source_revision_id": found["source_revision_id"],
        "verdict": "PASS",
        "actor": "operator",
    }
    assert client.post("/api/v1/products/image-qa", json=body, headers=CLIENT).status_code == 200
    conflict = client.post(
        "/api/v1/products/image-qa", json=body | {"verdict": "FAIL"}, headers=CLIENT
    )
    assert conflict.status_code == 409


def test_an_unknown_item_has_no_candidates(client: TestClient) -> None:
    response = client.get("/api/v1/products/items/no-such-item/image-candidates")
    assert response.status_code == 404
