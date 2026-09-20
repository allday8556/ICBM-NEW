"""The Registration Management surface over the real application (M5 PR-F §B).

Every state and every action verdict here comes from the server. The tests prove three things the
kickoff requires: the screen reads server-owned M5 truth, the server decides what may be done, and
a reload reconstructs the same view from durable rows rather than from anything held in a page.

No provider is reached: CREATE stays NOT_ADOPTED, the execution seams refuse locally, and the
canary readiness is a derived read that authorizes nothing.
"""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.connect.marketplace.capability import RemoteOutcome
from app.container import Container
from app.core.clock import Clock
from app.core.errors import AppError, ErrorClass
from app.main import create_app
from app.register.builder import RegistrationSnapshotBuilder
from app.register.contracts import RegisterAction
from app.register.execution import CREATE_ENDPOINT_GROUP, enqueue_create
from app.register.model import IntentState, ListingShape, ScopePauseReason
from app.register.preparation import UnitRequest
from app.register.service import RegisterService
from app.register.store import RegistrationStore
from integrations.marketplaces.smartstore.adoption import SmartStoreAdoption
from tests.conftest import LOCAL
from tests.integration.test_m5_register_execution import (
    FakeSender,
    context,
    execution,
    prepare,
)
from tests.product_support import Collections, product
from tests.register_support import (
    CATEGORY,
    CID,
    MARKET,
    OPERATOR,
    Preparation,
    ReadyItem,
    draft,
    establish,
    preparation,
    ready_final,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration

CLIENT = {"X-ICBM-Client": "pytest"}
OVERVIEW = "/api/v1/register/overview"
CANARY = "/api/v1/register/canary"


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    """The real application. One process owns the data directory (ADR-0006), so every owner this
    test drives is the application's own: the container under test *is* the served one."""
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


@pytest.fixture
def prep(container: Container, account: str) -> Preparation:
    # The served application re-evaluates through its **own** preflight owner, so this test
    # configures that owner's sources rather than only its own.
    return preparation(container, account, served=True)


@dataclass(frozen=True)
class Frozen:
    """One provider-listing unit this test froze, and the Intent it opened for it (if any)."""

    item: ReadyItem
    snapshot_id: str
    listing_identity: str
    item_key: str
    intent_id: str | None


def _separate_listings(
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
    *,
    intents: int,
) -> tuple[str, list[Frozen]]:
    """One `SEPARATE_LISTINGS` Draft holding two Items: **two** provider-listing units (§2, R3).

    Each is frozen as its own Snapshot with its own `registration_item_key`; ``intents`` says how
    many of them also open an Intent, so a Snapshot without one can be shown as well.
    """
    store = container.registrations
    builder = RegistrationSnapshotBuilder(preflight=prep.service, registrations=store)
    items = [
        ready_item(container, sources, "1234"),
        ready_item(container, sources, "5678"),
    ]
    draft_id = draft(store, account, items, ListingShape.SEPARATE_LISTINGS)
    current = store.draft(draft_id)
    assert current is not None
    frozen: list[Frozen] = []
    for position, item in enumerate(items):
        req = request(
            store,
            draft_id,
            account,
            [item],
            unit=UnitRequest(draft_id, current.draft_revision, (item.item_id,)),
        )
        req, final = ready_final(prep, req)
        snapshot = builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
        intent_id = None
        if position < intents:
            with store.transaction() as work:
                batch = work.create_batch(MARKET, account, created_by=OPERATOR, correlation_id=CID)
                intent = work.create_intent(
                    batch,
                    snapshot.registration_snapshot_id,
                    created_by=OPERATOR,
                    correlation_id=CID,
                )
            intent_id = intent.intent_id
        frozen.append(
            Frozen(
                item,
                snapshot.registration_snapshot_id,
                snapshot.listing_identity,
                snapshot.items[0].registration_item_key,
                intent_id,
            )
        )
    return draft_id, frozen


def _by_ref(units: list[dict]) -> dict[str, dict]:
    return {str(unit["unit_ref"]): unit for unit in units}


def _get(api: TestClient, path: str) -> dict:
    response = api.get(path, headers=CLIENT)
    assert response.status_code == 200, response.text
    return dict(response.json())


def _unit(api: TestClient) -> dict:
    units = _get(api, OVERVIEW)["units"]
    assert units, "the overview shows the prepared unit"
    return dict(units[0])


def _action(unit: dict, action: RegisterAction) -> dict:
    found = next(a for a in unit["actions"] if a["action"] == action.value)
    return dict(found)


def test_the_screen_reads_server_owned_registration_truth(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    unit = _unit(api)
    assert unit["marketplace_account_id"] == account
    assert unit["account_binding"] == "BOUND"
    assert unit["preparation"] == "INTENT_OPEN"
    assert unit["intent"]["intent_id"] == ready.intent_id
    assert unit["intent"]["state"] == IntentState.PREPARED.value
    # The price and its basis are the M4 pin the Draft froze, read and never recalculated.
    item = unit["items"][0]
    assert item["sale_price_krw"] > 0 and item["price_basis"]
    assert item["registration_item_key"].startswith("rik1-")
    assert unit["snapshot"]["registration_snapshot_id"] == ready.snapshot_id
    # A marketplace identity exists only once it is proven.
    assert unit["intent"]["marketplace_product_id"] is None
    screen = _get(api, "/api/v1/screens/register")
    assert screen["registration_candidates_total"] == 1
    assert screen["registrations_total"] == 0


def test_the_server_decides_which_actions_are_available(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    unit = _unit(api)
    # Nothing has been queued, so the frozen send request does not exist yet.
    create = _action(unit, RegisterAction.CREATE_ENQUEUE)
    assert create == {
        "action": "CREATE_ENQUEUE",
        "enabled": False,
        "reason_code": "REGISTER_SEND_REQUEST_ABSENT",
    }
    assert _action(unit, RegisterAction.RECONCILE)["reason_code"] == "REGISTER_NOT_UNKNOWN"
    assert _action(unit, RegisterAction.VERIFY)["reason_code"] == "REGISTER_NOT_APPLIED"
    assert _action(unit, RegisterAction.RESUME_SCOPE)["reason_code"] == "REGISTER_SCOPE_NOT_PAUSED"
    # The route refuses exactly what the view refused, so ignoring the verdict changes nothing.
    refused = api.post(
        f"/api/v1/register/intents/{ready.intent_id}/create", json={}, headers=CLIENT
    )
    assert refused.status_code >= 400
    assert container.jobs.count(job_type_prefix="register.create") == 0
    # Once a job carries the frozen request, the send request exists and the action is offered on
    # the job system's terms — a live job is the queued work, never a second one.
    enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    queued = _action(_unit(api), RegisterAction.CREATE_ENQUEUE)
    assert queued["reason_code"] in (None, "REGISTER_JOB_ALREADY_QUEUED")
    accepted = api.post(
        f"/api/v1/register/intents/{ready.intent_id}/create", json={}, headers=CLIENT
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["job_id"]
    # Whatever the worker did with the job meanwhile, no CREATE reached a provider: the send gate
    # refuses before an Attempt exists, because the endpoint is NOT_ADOPTED.
    assert container.registrations.attempts(ready.intent_id) == ()


def test_an_unknown_outcome_offers_reconcile_and_never_a_create_retry(
    api: TestClient,
    container: Container,
    clock: Clock,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(outcome=RemoteOutcome.UNKNOWN, product_id=None),
    )
    with pytest.raises(AppError):
        run.service.run(context(ready))
    unit = _unit(api)
    assert unit["intent"]["state"] == IntentState.UNKNOWN.value
    assert _action(unit, RegisterAction.RECONCILE)["enabled"] is True
    create = _action(unit, RegisterAction.CREATE_ENQUEUE)
    assert create["enabled"] is False and create["reason_code"] == "REGISTER_INTENT_NOT_SENDABLE"
    # The attempt is shown as unproven, never as a failure.
    attempt = unit["intent"]["attempts"][0]
    assert attempt["outcome"] == RemoteOutcome.UNKNOWN.value
    assert attempt["ambiguous_result"] is True
    # The route refuses a CREATE for an UNKNOWN Intent even when asked directly.
    refused = api.post(
        f"/api/v1/register/intents/{ready.intent_id}/create", json={}, headers=CLIENT
    )
    assert refused.status_code >= 400
    assert container.jobs.count(job_type_prefix="register.create") == 0
    # With no adopted lookup, reconcile resolves nothing and says so.
    unresolved = api.post(
        f"/api/v1/register/intents/{ready.intent_id}/reconcile", json={}, headers=CLIENT
    )
    assert unresolved.status_code >= 400
    assert container.registrations.intent(ready.intent_id).state is IntentState.UNKNOWN


def test_an_auth_brake_is_never_offered_an_operator_resume(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.AUTH,
            error_code="PROVIDER_AUTH",
        ),
    )
    with pytest.raises(AppError):
        run.service.run(context(ready))
    unit = _unit(api)
    assert unit["scope"]["pause_reason"] == ScopePauseReason.AUTH.value
    assert unit["scope"]["operator_resumable"] is False
    resume = _action(unit, RegisterAction.RESUME_SCOPE)
    assert resume["enabled"] is False
    assert resume["reason_code"] == "REGISTER_SCOPE_RESUME_NOT_PERMITTED"
    refused = api.post(
        "/api/v1/register/scopes/resume",
        json={
            "marketplace_key": MARKET,
            "marketplace_account_id": account,
            "actor": OPERATOR,
            "reason": "OPERATOR-DECIDED",
        },
        headers=CLIENT,
    )
    assert refused.status_code >= 400
    assert (
        container.registrations.execution_scope(
            MARKET, account, CREATE_ENDPOINT_GROUP
        ).resume_generation
        == 0
    )


def test_a_policy_brake_is_resumed_by_the_operator_through_the_server(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.POLICY_BLOCKED,
            error_code="PROVIDER_POLICY",
        ),
    )
    with pytest.raises(AppError):
        run.service.run(context(ready))
    unit = _unit(api)
    assert unit["scope"]["operator_resumable"] is True
    assert _action(unit, RegisterAction.RESUME_SCOPE)["enabled"] is True
    accepted = api.post(
        "/api/v1/register/scopes/resume",
        json={
            "marketplace_key": MARKET,
            "marketplace_account_id": account,
            "actor": OPERATOR,
            "reason": "POLICY-REVIEWED",
        },
        headers=CLIENT,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["scope"]["resume_generation"] == 1
    assert _unit(api)["scope"]["state"] == "ACTIVE"


def test_each_provider_listing_unit_of_one_draft_is_its_own_row(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    # ADR-0014 §2 (R3): a SEPARATE_LISTINGS Draft with two Items is two provider-listing units.
    _draft_id, frozen = _separate_listings(container, sources, account, prep, intents=2)
    units = _by_ref(_get(api, OVERVIEW)["units"])
    assert set(units) == {unit.listing_identity for unit in frozen}
    for unit in frozen:
        view = units[unit.listing_identity]
        # Each unit carries its own Snapshot, Intent and Item — and only its own.
        assert view["snapshot"]["registration_snapshot_id"] == unit.snapshot_id
        assert view["intent"]["intent_id"] == unit.intent_id
        assert [item["item_id"] for item in view["items"]] == [unit.item.item_id]
        assert view["items"][0]["registration_item_key"] == unit.item_key
        assert view["preparation"] == "INTENT_OPEN"
    # No sibling leaks into the other panel: neither the Item nor the key it was frozen under.
    first, second = (units[unit.listing_identity] for unit in frozen)
    keys = [item["registration_item_key"] for view in (first, second) for item in view["items"]]
    assert len(set(keys)) == 2
    assert first["intent"]["intent_id"] != second["intent"]["intent_id"]


def test_a_multi_unit_draft_never_passes_the_single_canary_unit_gate(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    draft_id, _frozen = _separate_listings(container, sources, account, prep, intents=2)
    canary = _get(api, f"{CANARY}?draft_id={draft_id}")
    assert canary["verdict"] == "BLOCKED"
    # The Draft holds two provider-listing units, so it is not one canary unit — the gate counts
    # units, never Draft panels.
    assert "SINGLE_UNIT" in canary["missing"]
    missing = {item["requirement"]: item for item in canary["requirements"]}
    assert missing["SINGLE_UNIT"]["reason_code"] == "MORE_THAN_ONE_UNIT_SELECTED"


def test_a_frozen_unit_without_an_intent_is_shown_and_survives_a_reload(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    _draft_id, frozen = _separate_listings(container, sources, account, prep, intents=1)
    units = _by_ref(_get(api, OVERVIEW)["units"])
    waiting = units[frozen[1].listing_identity]
    assert frozen[1].intent_id is None
    # A Snapshot an Intent does not yet name is still a unit, and it says exactly that.
    assert waiting["preparation"] == "SNAPSHOT_FROZEN"
    assert waiting["intent"] is None
    assert waiting["snapshot"]["registration_snapshot_id"] == frozen[1].snapshot_id
    assert [item["item_id"] for item in waiting["items"]] == [frozen[1].item.item_id]
    assert units[frozen[0].listing_identity]["preparation"] == "INTENT_OPEN"
    # A reload rebuilds it from the durable rows, with nothing held in a page or this process.
    again = RegisterService(
        registrations=RegistrationStore(container.db, container.clock, container.audit),
        execution=container.registration_execution,
        preflight=container.registration_preflight,
        accounts=container.accounts,
        jobs=container.jobs,
        capability=container.marketplace_capability,
        adoption=SmartStoreAdoption(),
    ).overview()
    reloaded = {unit.unit_ref: unit for unit in again.units}
    assert reloaded[frozen[1].listing_identity].preparation.value == "SNAPSHOT_FROZEN"


def test_the_screen_shows_the_servers_own_preflight_category_price_and_qa(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    # The preflight is recomputed from the operator's frozen inputs, and the only durable copy of
    # them is the send request a CREATE job carries.
    enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    unit = _unit(api)
    preflight = unit["preflight"]
    assert preflight["status"] == "READY" and preflight["reason_codes"] == []
    assert preflight["fingerprint_matches_snapshot"] is True
    assert unit["preflight_unavailable_reason"] is None
    # The category and what its reviewed metadata requires, with what the Snapshot actually sent.
    category = unit["category"]
    assert category["category_id"] == CATEGORY and category["reviewed"] is True
    fields = {field["key"]: field for field in category["attributes"]}
    assert fields["brand"] == {
        "key": "brand",
        "required": True,
        "provided": True,
        "detail_page_reference_allowed": False,
    }
    assert fields["color"]["required"] is False and fields["color"]["provided"] is False
    notice = {field["key"]: field for field in category["notice_fields"]}
    assert notice["manufacturer"]["provided"] is True and notice["origin"]["provided"] is True
    # What the category's own policy says about options, and the option fields this unit froze.
    assert category["options_supported"] is True and category["max_options"] == 5
    assert unit["items"][0]["option_keys"] == []
    # The pinned price, the current M4 price and the server's own comparison of the two.
    item = unit["items"][0]
    assert item["sale_price_krw"] == item["current_sale_price_krw"] > 0
    assert item["price_basis"] == item["current_price_basis"]
    assert item["price_pin_current"] is True
    assert item["base_status"] == "READY" and item["pricing_status"] == "READY"
    # The selected publication assets are identities with their M4 QA, never a count.
    asset = item["publication_assets"][0]
    assert len(asset["sha256"]) == 64 and asset["qa_verdict"] == "PASS"
    assert asset["provider_asset_prepared"] is True


def test_a_reprice_shows_a_stale_pin_and_the_preflight_reason_codes(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, container.registrations, account, prep)
    enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    before = _unit(api)["items"][0]
    # The same source product collected again at another price: M4 moves, the Draft pin does not.
    run_id, _revision = sources.collect(product(price=24500), source_product_id="1234")
    result = container.materializer.materialize_run(run_id)
    assert result.item_id == before["item_id"]
    repriced = container.pricing.price(
        result.item_id, prep.policies.target(MARKET, account).pricing_context
    )
    assert repriced.snapshot is not None
    item = _unit(api)["items"][0]
    # The screen shows both prices and the server's verdict that the pin is no longer current.
    assert item["sale_price_krw"] == before["sale_price_krw"]
    assert item["current_pricing_snapshot_id"] == repriced.snapshot.pricing_snapshot_id
    assert item["current_sale_price_krw"] != item["sale_price_krw"]
    assert item["price_pin_current"] is False
    # Every reason the owners returned is carried: the M4 layers and the preflight that refuses.
    unit = _unit(api)
    assert unit["preflight"]["status"] != "READY"
    assert unit["preflight"]["reason_codes"]
    assert unit["preflight"]["fingerprint_matches_snapshot"] is False
    assert item["base_reason_codes"] or item["pricing_reason_codes"]


def test_the_canary_plan_is_blocked_by_the_contracts_that_are_not_adopted(
    api: TestClient,
    container: Container,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    prepare(container, sources, container.registrations, account, prep)
    canary = _get(api, CANARY)
    assert canary["verdict"] == "BLOCKED"
    assert canary["execution_mode"] == "DRY_RUN"
    assert canary["write_status"] == "UNVERIFIED"
    missing = {
        item["requirement"]: item for item in canary["requirements"] if not item["satisfied"]
    }
    # The unadopted contracts are named as unadopted, never as absent or unnecessary.
    assert missing["CREATE_ADOPTED"]["reason_code"] == "ENDPOINT_NOT_ADOPTED"
    assert missing["CREATE_ADOPTED"]["endpoint_id"] == "SMARTSTORE_PRODUCT_CREATE_V2"
    assert missing["RECONCILE_PATH_ADOPTED"]["endpoint_id"] == "SMARTSTORE_PRODUCT_SEARCH"
    # A running application cannot prove its own checkout, so it says so rather than assuming.
    assert missing["CLEAN_RUNTIME"]["reason_code"] == "PROOF_NOT_AVAILABLE_IN_PROCESS"
    assert "CREATE_ADOPTED" in canary["missing"]


def test_a_reload_reconstructs_the_same_view_from_durable_rows(
    api: TestClient,
    container: Container,
    config: AppConfig,
    sources: Collections,
    account: str,
    prep: Preparation,
) -> None:
    prepare(container, sources, container.registrations, account, prep)
    before = _get(api, OVERVIEW)
    # A second application over the same data directory — what a reload or a restart gives — sees
    # exactly the same server-owned state, because none of it lived in a page or a process.
    # A second read model over the same database — what a reload gives — sees exactly the same
    # server-owned state, because none of it lived in a page or in this process.
    again = RegisterService(
        registrations=RegistrationStore(container.db, container.clock, container.audit),
        execution=container.registration_execution,
        preflight=container.registration_preflight,
        accounts=container.accounts,
        jobs=container.jobs,
        capability=container.marketplace_capability,
        adoption=SmartStoreAdoption(),
    ).overview()
    assert again.model_dump(mode="json") == before
