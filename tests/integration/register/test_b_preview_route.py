"""B-PREVIEW through the real application: a frozen Snapshot's preview is served read-only, an
unknown Snapshot is refused, and no provider URL reaches the response. No provider is contacted."""

import json

import pytest
from fastapi.testclient import TestClient

from app.container import Container
from tests.integration.register.test_authoring_unowned_revisions import (  # noqa: F401 - fixtures
    api,
    container,
    draft,
)
from tests.integration.review.test_g2c_review_counts import _freeze
from tests.support.gate1_support import CLIENT

pytestmark = pytest.mark.integration


def test_a_frozen_snapshot_is_previewed_read_only(
    api: TestClient,  # noqa: F811
    container: Container,  # noqa: F811
    draft: tuple[str, str],  # noqa: F811
) -> None:
    draft_id, item_id = draft
    snapshot_id = _freeze(container, draft_id, item_id)
    stored = container.registrations.snapshot_payload(snapshot_id)
    response = api.get(f"/api/v1/register/snapshots/{snapshot_id}/preview", headers=CLIENT)
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["registration_snapshot_id"] == snapshot_id
    # This fixture freezes a minimal payload the projection refuses: the preview shows the refusal
    # with the adapter's own code, never a guess.
    assert stored == {"name": "invented listing name", "items": 1}
    assert (view["projected"], view["document"]) == (False, [])
    assert view["refusal_code"].startswith("WIRE_")
    assert "://" not in json.dumps(view, ensure_ascii=False)
    # Reading it changed nothing.
    assert container.registrations.snapshot_payload(snapshot_id) == stored
    missing = api.get("/api/v1/register/snapshots/no-such-snapshot/preview", headers=CLIENT)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "REGISTER_SNAPSHOT_NOT_FOUND"
