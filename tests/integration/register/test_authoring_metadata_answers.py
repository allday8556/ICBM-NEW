"""What the authoring-metadata read answers (2026-10-15 usability check).

- a category without reviewed metadata under the account's taxonomy is a typed 409
  ``REGISTER_CATEGORY_METADATA_MISSING`` (class CONFLICT, the category named in its details),
  never a 500 of class UNKNOWN;
- each SmartStore notice field carries the provider schema's own Korean label, and the notice type
  its Korean name; an attribute, which that schema does not name, carries none. A label is display
  only: the keys, rules and revisions are unchanged.
"""

import pytest
from fastapi.testclient import TestClient

from integrations.marketplaces.smartstore.notice_schema import load_notice_schema
from tests.integration.register.test_authoring_unowned_revisions import (  # noqa: F401 - fixtures
    api,
    container,
    draft,
)
from tests.support.gate1_support import CATEGORY, CLIENT, ETC_NOTICE_KEYS

pytestmark = pytest.mark.integration


def test_a_category_without_reviewed_metadata_is_a_typed_conflict(
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, _ = draft
    response = api.get(
        f"/api/v1/register/drafts/{draft_id}/authoring-metadata/50002608", headers=CLIENT
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert (error["class"], error["code"]) == ("CONFLICT", "REGISTER_CATEGORY_METADATA_MISSING")
    assert error["retryable"] is False
    assert error["details"]["category_id"] == "50002608"


def test_notice_fields_carry_the_schemas_korean_labels(
    api: TestClient,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, _ = draft
    response = api.get(
        f"/api/v1/register/drafts/{draft_id}/authoring-metadata/{CATEGORY}", headers=CLIENT
    )
    assert response.status_code == 200
    body = response.json()
    etc = load_notice_schema().types["ETC"]
    assert body["notice_type"] == "ETC"
    assert body["notice_type_label"] == etc.name
    labels = {field["key"]: field["label"] for field in body["notice_fields"]}
    assert list(labels) == ETC_NOTICE_KEYS
    for key, label in labels.items():
        found = etc.field(key)
        assert found is not None and label == found.label
    assert {field["key"]: field["label"] for field in body["attributes"]} == {
        "brand": None,
        "color": None,
    }
