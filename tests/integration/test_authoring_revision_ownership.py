"""Authoring revisions are owner-held only by exact equality (Issue #89 decision 5801915996).

No real owner of the category-mapping or detail-composition revision exists yet, so this uses a
**controlled non-null target fixture** (``tests.register_support.target``: ``mapping-test-1`` /
``detail-test-1``) to prove what the null policy cannot show:
- a preparation carrying exactly the target's revisions is accepted, and the ownership rule lets
  its unit reach READY;
- a preparation carrying any other non-null revision is refused at the write boundary and writes
  nothing;
- a request carrying another non-null revision (however it got there) is never READY, and the
  Snapshot builder refuses it even from a forged READY result — no Snapshot is written.

No provider is contacted.
"""

import contextlib
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.container import Container
from app.core.errors import InputValidationError
from app.main import create_app
from app.products.model import ReadinessStatus
from app.register.builder import RegistrationSnapshotBuilder
from app.register.model import RegistrationConflictError
from app.register.preparation import (
    AUTHORING_REVISIONS_UNOWNED,
    CategoryConfirmation,
    CategorySelection,
    DetailComposition,
    PreflightRequest,
)
from tests.conftest import LOCAL
from tests.product_support import Collections, raw
from tests.register_support import (
    CATEGORY,
    CID,
    MARKET,
    OPERATOR,
    TAXONOMY,
    Preparation,
    ReadyItem,
    draft,
    establish,
    preparation,
    prepared,
    ready_final,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
PREPARATIONS = "/api/v1/register/preparations"
OWNED = ("mapping-test-1", "detail-test-1")


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


@pytest.fixture
def prep(container: Container, account: str) -> Preparation:
    return preparation(container, account, served=True)


@pytest.fixture
def item(container: Container, config: AppConfig) -> ReadyItem:
    return ready_item(container, Collections.of(container, config), "1234")


@pytest.fixture
def unit(container: Container, account: str, item: ReadyItem) -> tuple[str, str]:
    return draft(container.registrations, account, [item]), item.item_id


def owned_request(
    container: Container, account: str, draft_id: str, item: ReadyItem
) -> PreflightRequest:
    """The unit's request carrying exactly the target's revisions."""
    req = request(container.registrations, draft_id, account, [item])
    assert req.category is not None and req.detail is not None
    assert (req.category.mapping_revision, req.detail.composition_revision) == OWNED
    return req


def inputs(mapping: str | None, composition: str | None) -> dict[str, Any]:
    return {
        "category": {
            "category_id": CATEGORY,
            "mapping_revision": mapping,
            "taxonomy_revision": TAXONOMY,
            "confirmation": "OPERATOR_CONFIRMED",
        },
        "name": {"value": "authored listing name", "provenance": "OPERATOR_CONFIRMED"},
        "tags": [],
        "attributes": {"brand": {"value": "authored brand"}},
        "notices": {
            "manufacturer": {"value": "authored maker"},
            "origin": {"detail_page_reference": True},
        },
        "options": {},
        "detail_composition_revision": composition,
        "detail_body": "authored body text",
        "detail_sections": ["BODY"],
    }


def snapshot_count(config: AppConfig) -> int:
    with contextlib.closing(raw(config)) as connection:
        count: int = connection.execute("SELECT COUNT(*) FROM registration_snapshots").fetchone()[0]
    return count


def mismatched(req: PreflightRequest, field: str) -> PreflightRequest:
    if field == "mapping":
        return replace(
            req,
            category=CategorySelection(
                CATEGORY, "mapping-other", TAXONOMY, CategoryConfirmation.OPERATOR_CONFIRMED
            ),
        )
    return replace(req, detail=DetailComposition("detail-other", "invented body text"))


# ---------------------------------------------------------------- the write boundary


def test_exactly_the_targets_revisions_are_authored(
    config: AppConfig, api: TestClient, prep: Preparation, unit: tuple[str, str]
) -> None:
    draft_id, item_id = unit
    response = api.post(
        PREPARATIONS,
        json={
            "draft_id": draft_id,
            "item_ids": [item_id],
            "actor": OPERATOR,
            "inputs": inputs(*OWNED),
        },
        headers=CLIENT,
    )
    assert response.status_code == 200, response.text
    saved = response.json()["inputs"]
    assert (saved["category"]["mapping_revision"], saved["detail_composition_revision"]) == OWNED
    evaluated = api.post(
        f"{PREPARATIONS}/{response.json()['preparation_id']}/evaluate", headers=CLIENT
    ).json()["preflight"]
    assert AUTHORING_REVISIONS_UNOWNED not in evaluated["reason_codes"]


@pytest.mark.parametrize(
    ("mapping", "composition", "refused"),
    [
        ("mapping-other", OWNED[1], ["mapping_revision"]),
        (OWNED[0], "detail-other", ["detail_composition_revision"]),
        (None, OWNED[1], ["mapping_revision"]),
        (OWNED[0], None, ["detail_composition_revision"]),
    ],
)
def test_any_other_revision_is_refused_under_an_owned_target(
    config: AppConfig,
    api: TestClient,
    prep: Preparation,
    unit: tuple[str, str],
    mapping: str | None,
    composition: str | None,
    refused: list[str],
) -> None:
    draft_id, item_id = unit
    response = api.post(
        PREPARATIONS,
        json={
            "draft_id": draft_id,
            "item_ids": [item_id],
            "actor": OPERATOR,
            "inputs": inputs(mapping, composition),
        },
        headers=CLIENT,
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert (error["code"], error["details"]["fields"]) == (
        "REGISTER_AUTHORING_REVISION_NOT_OWNED",
        refused,
    )
    with contextlib.closing(raw(config)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM registration_preparations").fetchone() == (
            0,
        )


# ---------------------------------------------------------------- preflight and builder


def test_matching_revisions_pass_the_ownership_rule(
    container: Container, account: str, prep: Preparation, unit: tuple[str, str], item: ReadyItem
) -> None:
    _, final = ready_final(prep, owned_request(container, account, unit[0], item))
    assert final.status is ReadinessStatus.READY
    assert AUTHORING_REVISIONS_UNOWNED not in final.codes


@pytest.mark.parametrize("field", ["mapping", "detail"])
def test_a_mismatched_revision_is_never_ready_and_never_frozen(
    config: AppConfig,
    container: Container,
    account: str,
    prep: Preparation,
    unit: tuple[str, str],
    item: ReadyItem,
    field: str,
) -> None:
    matching, ready = ready_final(prep, owned_request(container, account, unit[0], item))
    req = mismatched(matching, field)
    candidate = prep.service.candidate(req)
    assert candidate.status is not ReadinessStatus.READY
    assert AUTHORING_REVISIONS_UNOWNED in candidate.codes
    final = prep.service.final(req, prepared(candidate))
    assert final.status is not ReadinessStatus.READY
    assert AUTHORING_REVISIONS_UNOWNED in final.codes
    builder = RegistrationSnapshotBuilder(
        preflight=prep.service, registrations=container.registrations
    )
    with pytest.raises(InputValidationError, match="final READY"):
        builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    # A forged READY carrying the mismatched request: the fresh evaluation refuses it, and the
    # write path refuses it on the revisions alone.
    forged = replace(ready, request=req)
    with pytest.raises(RegistrationConflictError) as stale:
        builder.freeze(forged, created_by=OPERATOR, correlation_id=CID)
    assert stale.value.code == "REGISTER_PREFLIGHT_STALE"
    with (
        pytest.raises(RegistrationConflictError) as unowned,
        container.registrations.transaction() as registrations,
    ):
        builder._freeze_fresh(
            registrations,
            forged,
            created_by=OPERATOR,
            correlation_id=CID,
            preparation_revision_id=None,
        )
    assert unowned.value.code == "REGISTER_AUTHORING_REVISIONS_UNOWNED"
    assert snapshot_count(config) == 0
