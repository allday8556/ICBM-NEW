"""B-UX2: the fix-only projection of Registration Management, through the real application on a
migrated database. No provider is contacted.

- a unit the owner could not evaluate is a fix with its real refusal code and the surface;
- an evaluated unit's reasons keep their owner's code, status and subject; only the actionable ones
  are listed as fixes, and every row is counted by actionability;
- REGISTER never reads the review owner: the composite lives in the screens layer.
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

FIXES = "/api/v1/screens/register/fixes"
ACTIONABILITY = {"FIX_AVAILABLE", "RECHECK", "WAITING", "NO_OPERATOR_ACTION", "NOT_IMPLEMENTED"}


def fixes(api: TestClient) -> dict[str, Any]:  # noqa: F811
    response = api.get(FIXES, headers=CLIENT)
    assert response.status_code == 200, response.text
    found = dict(response.json())
    assert set(found["counts"]) == ACTIONABILITY
    assert all(row["actionability"] in {"FIX_AVAILABLE", "RECHECK"} for row in found["fixes"])
    assert all(row["surface"] and row["surface_label"] for row in found["fixes"])
    return found


def test_a_unit_without_a_preparation_is_a_fix_with_its_real_code(
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, _ = draft
    found = fixes(api)
    (row,) = found["fixes"]
    assert (row["source"], row["code"], row["draft_id"]) == (
        "NOT_EVALUATED",
        "REGISTER_PREPARATION_ABSENT",
        draft_id,
    )
    assert (row["actionability"], row["surface"]) == ("FIX_AVAILABLE", "REGISTER_PREPARATION")
    assert found["counts"]["FIX_AVAILABLE"] == 1 and sum(found["counts"].values()) == 1


def test_an_evaluated_units_reasons_are_projected_as_returned(
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, item_id = draft
    saved = author(api, draft_id, item_id)
    preflight = evaluate(api, saved["preparation_id"])
    found = fixes(api)
    # Every reason is counted once; only the actionable ones are listed.
    assert sum(found["counts"].values()) == len(preflight["reasons"])
    listed = {(r["code"], r["subject"]) for r in found["fixes"]}
    returned = {(r["code"], r["subject"]): r for r in preflight["reasons"]}
    assert listed <= set(returned)
    (unowned,) = [r for r in found["fixes"] if r["code"] == AUTHORING_REVISIONS_UNOWNED]
    assert (unowned["status"], unowned["areas"]) == ("REVIEW_REQUIRED", ["CATEGORY", "DETAIL"])
    assert (unowned["actionability"], unowned["surface"]) == (
        "FIX_AVAILABLE",
        "SETTINGS_TARGET_POLICY",
    )
    # A reason with no screen is counted, never offered as a fix.
    assert "DUPLICATE_EVIDENCE_MISSING" not in {r["code"] for r in found["fixes"]}
