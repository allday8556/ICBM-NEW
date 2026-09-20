"""The Registration Management surface over the real application (M5 PR-F §B).

Every state and every action verdict here comes from the server. The tests prove three things the
kickoff requires: the screen reads server-owned M5 truth, the server decides what may be done, and
a reload reconstructs the same view from durable rows rather than from anything held in a page.

No provider is reached: CREATE stays NOT_ADOPTED, the execution seams refuse locally, and the
canary readiness is a derived read that authorizes nothing.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.connect.marketplace.capability import RemoteOutcome
from app.container import Container
from app.core.clock import Clock
from app.core.errors import AppError, ErrorClass
from app.main import create_app
from app.register.contracts import RegisterAction
from app.register.execution import CREATE_ENDPOINT_GROUP, enqueue_create
from app.register.model import IntentState, ScopePauseReason
from app.register.service import RegisterService
from app.register.store import RegistrationStore
from integrations.marketplaces.smartstore.execution import SmartStoreAdoption
from tests.conftest import LOCAL
from tests.integration.test_m5_register_execution import (
    FakeSender,
    context,
    execution,
    prepare,
)
from tests.product_support import Collections
from tests.register_support import MARKET, OPERATOR, Preparation, establish, preparation

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
    return preparation(container, account)


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
        accounts=container.accounts,
        jobs=container.jobs,
        capability=container.marketplace_capability,
        adoption=SmartStoreAdoption(),
    ).overview()
    assert again.model_dump(mode="json") == before
