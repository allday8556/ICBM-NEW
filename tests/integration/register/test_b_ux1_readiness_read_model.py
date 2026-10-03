"""B-UX1: the structured reason read model and the readiness summary of the pre-send population,
through the real application on a migrated database. No provider is contacted.

- every preflight reason reaches the API with its owner's code, status and subject, and its areas;
  the flat ``reason_codes`` stay exactly the same set;
- each Item's M4 base and pricing reasons are structured the same way;
- the summary counts every pre-send unit once: five statuses plus ``NOT_EVALUATED`` with the
  owner's real refusal code, summing to the population; areas count units and reasons and keep
  the real codes.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.stages.register.preparation import AUTHORING_REVISIONS_UNOWNED
from tests.integration.register.test_authoring_unowned_revisions import (  # noqa: F401 - fixtures
    api,
    author,
    container,
    draft,
    evaluate,
)
from tests.support.gate1_support import CLIENT

pytestmark = pytest.mark.integration

READINESS = "/api/v1/register/readiness"
STATUSES = {"READY", "REVIEW_REQUIRED", "BLOCKED", "DUPLICATE", "STALE", "NOT_EVALUATED"}


def summary(api: TestClient) -> dict[str, Any]:  # noqa: F811
    response = api.get(READINESS, headers=CLIENT)
    assert response.status_code == 200, response.text
    found = dict(response.json())
    assert set(found["statuses"]) == STATUSES
    assert sum(found["statuses"].values()) == found["population"]
    return found


def test_a_unit_without_a_preparation_is_not_evaluated_with_the_real_refusal(
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    found = summary(api)
    assert found["population"] == 1
    assert found["statuses"]["NOT_EVALUATED"] == 1
    assert found["not_evaluated"] == {"REGISTER_PREPARATION_ABSENT": 1}
    assert all(area["units"] == 0 for area in found["areas"])
    assert {area["area"] for area in found["areas"]} >= {"IMAGES", "DETAIL", "UNCLASSIFIED"}


def test_reasons_are_structured_exactly_as_the_owner_returned_them(
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, item_id = draft
    saved = author(api, draft_id, item_id)
    preflight = evaluate(api, saved["preparation_id"])
    reasons = preflight["reasons"]
    assert reasons
    assert {reason["code"] for reason in reasons} == set(preflight["reason_codes"])
    unowned = [r for r in reasons if r["code"] == AUTHORING_REVISIONS_UNOWNED]
    assert unowned == [
        {
            "code": AUTHORING_REVISIONS_UNOWNED,
            "status": "REVIEW_REQUIRED",
            "subject": "authoring",
            "areas": ["CATEGORY", "DETAIL"],
        }
    ]
    assert all(r["status"] in STATUSES - {"NOT_EVALUATED"} and r["areas"] for r in reasons)
    # The screen carries the same structured reasons, and each Item's M4 reasons.
    units = api.get("/api/v1/register/overview", headers=CLIENT).json()["units"]
    (unit,) = [u for u in units if u["draft_id"] == draft_id]
    assert unit["preflight"]["reasons"] == reasons
    for item in unit["items"]:
        assert [r["code"] for r in item["base_reasons"]] == item["base_reason_codes"]
        assert [r["code"] for r in item["pricing_reasons"]] == item["pricing_reason_codes"]


def test_the_summary_counts_the_evaluated_unit_by_status_and_area(
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, item_id = draft
    saved = author(api, draft_id, item_id)
    preflight = evaluate(api, saved["preparation_id"])
    found = summary(api)
    assert found["population"] == 1
    assert found["statuses"][preflight["status"]] == 1
    assert found["statuses"]["NOT_EVALUATED"] == 0 and found["not_evaluated"] == {}
    by_area = {area["area"]: area for area in found["areas"]}
    for area in ("CATEGORY", "DETAIL"):
        assert by_area[area]["units"] == 1
        assert AUTHORING_REVISIONS_UNOWNED in by_area[area]["codes"]
    reasons = sum(len(r["areas"]) for r in preflight["reasons"])
    assert sum(area["reasons"] for area in found["areas"]) == reasons
    assert by_area["UNCLASSIFIED"]["units"] == 0
