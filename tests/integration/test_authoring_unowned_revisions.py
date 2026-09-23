"""Authoring with unowned authoring revisions (Issue #89 architect decision 5800619183).

G1-A holds a durable target policy's ``category_mapping_revision`` and
``detail_composition_revision`` as ``null``: no owner exists for them yet. This proves the
compatibility the decision requires, through the real application on a migrated database:
- the authoring metadata of a reviewed G1-B category answers 200 with both revisions ``null``;
- a BODY-only preparation is saved with both ``null`` exactly, and a reload and a restart return
  the same ``null``, body and fingerprint; the fingerprint moves with authored content;
- the candidate preflight reports ``AUTHORING_REVISIONS_UNOWNED`` — not a missing policy or
  missing metadata — and still evaluates every other rule, so the unit is never READY;
- a freeze is refused and writes no Snapshot, Batch, Intent or Attempt, and the Snapshot builder
  refuses an unowned revision even if it is handed a READY result;
- a preparation carrying any revision other than the policy's own (``null``) is refused whole on
  create and update (decision 5801915996), G1-A still refuses a non-null value, and no stand-in
  revision is stored anywhere.

No provider is contacted.
"""

import contextlib
import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.main import create_app
from app.products.materialization import MaterializationStatus
from app.products.model import ReadinessStatus
from app.register.authoring import preflight_request
from app.register.model import ListingShape, RegistrationConflictError
from app.register.preparation import AUTHORING_REVISIONS_UNOWNED, PreflightStage
from tests.conftest import LOCAL
from tests.gate1_support import (
    CATEGORY,
    CLIENT,
    MARKET,
    OPERATOR,
    TAXONOMY,
    record_reviewed_metadata,
    save_policy,
)
from tests.product_support import Collections, product, raw
from tests.register_support import establish

pytestmark = pytest.mark.integration

PREPARATIONS = "/api/v1/register/preparations"
FROZEN_ROWS = (
    "registration_snapshots",
    "registration_item_snapshots",
    "registration_batches",
    "registration_intents",
    "registration_attempts",
)
STAND_INS = ("body-only", "unowned", "default", "sentinel", "placeholder")


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


@pytest.fixture
def draft(api: TestClient, container: Container, config: AppConfig) -> tuple[str, str]:
    """A Draft of one M4 Item under a durable G1-A policy and reviewed G1-B metadata; the Draft
    and the Item."""
    account = establish(container, config, MARKET, "provider-account-1")
    save_policy(api, account)
    record_reviewed_metadata(api)
    run_id, _ = Collections.of(container, config).collect(product(), source_product_id="1234")
    result = container.materializer.materialize_run(run_id)
    assert result.status is MaterializationStatus.MATERIALIZED and result.item_id is not None
    policy = container.registration_preflight.target_policy(MARKET, account)
    assert policy is not None
    assert (policy.category_mapping_revision, policy.detail_composition_revision) == (None, None)
    snapshot = container.pricing.price(result.item_id, policy.pricing_context).snapshot
    assert snapshot is not None
    with container.registrations.transaction() as unit:
        created = unit.create_draft(
            MARKET,
            account,
            ListingShape.SINGLE_LISTING_WITH_OPTIONS,
            created_by=OPERATOR,
            correlation_id="cid-authoring",
        )
        unit.add_draft_item(
            created.draft_id,
            result.item_id,
            snapshot.pricing_snapshot_id,
            added_by=OPERATOR,
            correlation_id="cid-authoring",
        )
    return created.draft_id, result.item_id


def inputs(*, brand: str | None = "합성 브랜드", body: str = "상세 본문") -> dict[str, Any]:
    """What the authoring form sends back: the server's null revisions, exactly."""
    return {
        "category": {
            "category_id": CATEGORY,
            "mapping_revision": None,
            "taxonomy_revision": TAXONOMY,
            "confirmation": "OPERATOR_CONFIRMED",
        },
        "name": {"value": "합성 상품", "provenance": "OPERATOR_CONFIRMED"},
        "tags": [],
        "attributes": {} if brand is None else {"brand": {"value": brand}},
        "notices": {
            "manufacturer": {"value": "합성 제조사"},
            "origin": {"detail_page_reference": True},
        },
        "options": {},
        "detail_composition_revision": None,
        "detail_body": body,
        "detail_sections": ["BODY"],
    }


def author(api: TestClient, draft_id: str, item_id: str, **overrides: Any) -> dict[str, Any]:
    response = api.post(
        PREPARATIONS,
        json={
            "draft_id": draft_id,
            "item_ids": [item_id],
            "actor": OPERATOR,
            "inputs": inputs(**overrides),
        },
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def evaluate(api: TestClient, preparation_id: str) -> dict[str, Any]:
    response = api.post(f"{PREPARATIONS}/{preparation_id}/evaluate", headers=CLIENT)
    assert response.status_code == 200, response.text
    return dict(response.json()["preflight"])


def counts(config: AppConfig) -> dict[str, int]:
    with contextlib.closing(raw(config)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in FROZEN_ROWS
        }


# ---------------------------------------------------------------- authoring


def test_the_authoring_metadata_answers_with_both_revisions_null(
    api: TestClient, draft: tuple[str, str]
) -> None:
    draft_id, _ = draft
    response = api.get(
        f"/api/v1/register/drafts/{draft_id}/authoring-metadata/{CATEGORY}", headers=CLIENT
    )
    assert response.status_code == 200, response.text
    metadata = response.json()
    assert (metadata["mapping_revision"], metadata["detail_composition_revision"]) == (None, None)
    assert metadata["taxonomy_revision"] == TAXONOMY and metadata["metadata_revision"]
    # The reviewed G1-B rules come back in full.
    assert [(f["key"], f["required"]) for f in metadata["attributes"]] == [
        ("brand", True),
        ("color", False),
    ]
    assert [f["key"] for f in metadata["notice_fields"]] == ["manufacturer", "origin"]
    assert metadata["options_supported"] is True


def test_a_body_only_preparation_keeps_both_nulls_through_reload_and_restart(
    config: AppConfig, api: TestClient, container: Container, draft: tuple[str, str]
) -> None:
    draft_id, item_id = draft
    saved = author(api, draft_id, item_id)
    assert saved["inputs"]["category"]["mapping_revision"] is None
    assert saved["inputs"]["detail_composition_revision"] is None
    assert saved["inputs"]["detail_body"] == "상세 본문"
    reread = api.get(f"{PREPARATIONS}/{saved['preparation_id']}", headers=CLIENT).json()
    assert reread == saved
    # Stored as explicit nulls: no stand-in revision anywhere.
    with contextlib.closing(raw(config)) as connection:
        category_json, detail_json = connection.execute(
            "SELECT category_json, detail_json FROM registration_preparation_revisions"
        ).fetchone()
    assert json.loads(category_json)["mapping_revision"] is None
    assert json.loads(detail_json)["composition_revision"] is None
    stored = (category_json + detail_json).lower()
    assert not any(word in stored for word in STAND_INS)
    api.__exit__(None, None, None)
    with TestClient(create_app(config), base_url=LOCAL) as restarted:
        after = restarted.get(f"{PREPARATIONS}/{saved['preparation_id']}", headers=CLIENT).json()
    assert after == saved


def test_the_fingerprint_moves_with_authored_content_and_the_nulls_stay(
    api: TestClient, draft: tuple[str, str]
) -> None:
    draft_id, item_id = draft
    first = author(api, draft_id, item_id)
    response = api.post(
        f"{PREPARATIONS}/{first['preparation_id']}",
        json={"item_ids": [item_id], "actor": OPERATOR, "inputs": inputs(body="다른 상세 본문")},
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["revision_no"] == 2
    assert second["inputs_fingerprint"] != first["inputs_fingerprint"]
    assert second["inputs"]["category"]["mapping_revision"] is None
    assert second["inputs"]["detail_composition_revision"] is None
    # The same content again is the same fingerprint: the null is part of it, explicitly.
    again = api.post(
        f"{PREPARATIONS}/{first['preparation_id']}",
        json={"item_ids": [item_id], "actor": OPERATOR, "inputs": inputs()},
        headers=CLIENT,
    ).json()
    assert again["inputs_fingerprint"] == first["inputs_fingerprint"]


# ---------------------------------------------------------------- the candidate preflight


def test_the_candidate_reports_unowned_revisions_and_evaluates_everything_else(
    api: TestClient, draft: tuple[str, str]
) -> None:
    draft_id, item_id = draft
    saved = author(api, draft_id, item_id, brand=None)
    preflight = evaluate(api, saved["preparation_id"])
    codes = set(preflight["reason_codes"])
    assert AUTHORING_REVISIONS_UNOWNED in codes
    assert not codes & {
        "REGISTER_TARGET_POLICY_MISSING",
        "CATEGORY_METADATA_MISSING",
        "CATEGORY_METADATA_UNREVIEWED",
        "DETAIL_COMPOSITION_MISSING",
    }
    assert preflight["status"] != "READY"
    # The reviewed category rules are still applied: the required brand is missing here...
    assert "ATTRIBUTE_REQUIRED_MISSING" in codes
    # ...and not once it is authored, while the unowned revisions stay reported.
    response = api.post(
        f"{PREPARATIONS}/{saved['preparation_id']}",
        json={"item_ids": [item_id], "actor": OPERATOR, "inputs": inputs()},
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    codes = set(evaluate(api, saved["preparation_id"])["reason_codes"])
    assert "ATTRIBUTE_REQUIRED_MISSING" not in codes
    assert AUTHORING_REVISIONS_UNOWNED in codes
    # The Registration Management read model shows the same server reason.
    (unit,) = api.get(f"/api/v1/register/units/{draft_id}", headers=CLIENT).json()
    assert AUTHORING_REVISIONS_UNOWNED in unit["preflight"]["reason_codes"]


def preparation_rows(config: AppConfig) -> tuple[int, int]:
    with contextlib.closing(raw(config)) as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM registration_preparations").fetchone()[0],
            connection.execute(
                "SELECT COUNT(*) FROM registration_preparation_revisions"
            ).fetchone()[0],
        )


@pytest.mark.parametrize(
    ("mapping", "composition", "body", "refused"),
    [
        ("body-only-v1", None, "상세 본문", ["mapping_revision"]),
        (None, "body-only-v1", "상세 본문", ["detail_composition_revision"]),
        ("unowned", "default", "상세 본문", ["detail_composition_revision", "mapping_revision"]),
        # A composition revision sent without a body would not be stored; it is refused anyway.
        (None, "sentinel", None, ["detail_composition_revision"]),
    ],
)
def test_a_client_supplied_revision_is_refused_and_writes_nothing(
    config: AppConfig,
    api: TestClient,
    draft: tuple[str, str],
    mapping: str | None,
    composition: str | None,
    body: str | None,
    refused: list[str],
) -> None:
    """The revisions are server-owned (5801915996): the client sends back exactly the server's
    value, None today. Anything else is refused whole — on create and on update."""
    draft_id, item_id = draft
    authored = inputs()
    authored["category"]["mapping_revision"] = mapping
    authored["detail_composition_revision"] = composition
    authored["detail_body"] = body
    response = api.post(
        PREPARATIONS,
        json={"draft_id": draft_id, "item_ids": [item_id], "actor": OPERATOR, "inputs": authored},
        headers=CLIENT,
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "REGISTER_AUTHORING_REVISION_NOT_OWNED"
    assert error["details"]["fields"] == refused
    assert preparation_rows(config) == (0, 0)
    # An existing preparation is not revised by it either.
    saved = author(api, draft_id, item_id)
    response = api.post(
        f"{PREPARATIONS}/{saved['preparation_id']}",
        json={"item_ids": [item_id], "actor": OPERATOR, "inputs": authored},
        headers=CLIENT,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "REGISTER_AUTHORING_REVISION_NOT_OWNED"
    assert preparation_rows(config) == (1, 1)
    reread = api.get(f"{PREPARATIONS}/{saved['preparation_id']}", headers=CLIENT).json()
    assert reread == saved


# ---------------------------------------------------------------- freeze stays fail-closed


def test_a_freeze_is_refused_and_writes_nothing(
    config: AppConfig, api: TestClient, container: Container, draft: tuple[str, str]
) -> None:
    draft_id, item_id = draft
    saved = author(api, draft_id, item_id)
    (unit,) = api.get(f"/api/v1/register/units/{draft_id}", headers=CLIENT).json()
    freeze = next(a for a in unit["actions"] if a["action"] == "FREEZE")
    assert (freeze["enabled"], freeze["reason_code"]) == (False, "REGISTER_PREFLIGHT_NOT_READY")
    response = api.post(
        f"{PREPARATIONS}/{saved['preparation_id']}/freeze", json={"actor": OPERATOR}, headers=CLIENT
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "REGISTER_PREFLIGHT_NOT_READY"
    assert counts(config) == dict.fromkeys(FROZEN_ROWS, 0)
    # The final preflight names the same reason.
    preparation = container.registration_preparations.preparation(saved["preparation_id"])
    final = container.registration_preflight.final(
        preflight_request(preparation, preparation.current), ()
    )
    assert AUTHORING_REVISIONS_UNOWNED in final.codes


def test_the_builder_refuses_an_unowned_revision_even_when_handed_ready(
    config: AppConfig, api: TestClient, container: Container, draft: tuple[str, str]
) -> None:
    draft_id, item_id = draft
    saved = author(api, draft_id, item_id)
    preparation = container.registration_preparations.preparation(saved["preparation_id"])
    final = container.registration_preflight.final(
        preflight_request(preparation, preparation.current), ()
    )
    forged = replace(final, stage=PreflightStage.FINAL, status=ReadinessStatus.READY)
    with (
        pytest.raises(RegistrationConflictError) as caught,
        container.registrations.transaction() as unit,
    ):
        container.registration_builder._freeze_fresh(
            unit,
            forged,
            created_by=OPERATOR,
            correlation_id="cid",
            preparation_revision_id=None,
        )
    assert caught.value.code == "REGISTER_AUTHORING_REVISIONS_UNOWNED"
    assert counts(config) == dict.fromkeys(FROZEN_ROWS, 0)


# ---------------------------------------------------------------- G1-A is unchanged


def test_g1a_still_refuses_a_client_supplied_authoring_revision(
    api: TestClient, container: Container, draft: tuple[str, str]
) -> None:
    account = container.accounts.accounts(MARKET)[0].marketplace_account_id
    current = api.get(f"/api/v1/settings/target-policies/{MARKET}/{account}", headers=CLIENT)
    body = current.json()
    for field in ("category_mapping_revision", "detail_composition_revision"):
        payload = dict(body["inputs"])
        payload[field] = "body-only-v1"
        response = api.post(
            f"/api/v1/settings/target-policies/{MARKET}/{account}/revisions",
            json={
                "actor": OPERATOR,
                "expected_current_revision": body["current"]["policy_revision"],
                "inputs": payload,
            },
            headers=CLIENT,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "TARGET_POLICY_AUTHORING_REVISION_UNOWNED"
