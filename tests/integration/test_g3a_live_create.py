"""Gate 3 area 1: the CREATE path behind the real send-time safety stack (ADR-0018 §4.3, G3-27).

The REGISTER execution owner is the one of PR-E, with its fake provider seams; only the authority
changes. Production wiring must refuse at this main; a permitted test stack must spend exactly the
grant that names this Snapshot, Intent, idempotency key and attempt number — and a retry after a
proven non-application needs a new grant.
"""

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from app.config import AppConfig
from app.connect.marketplace.capability import RemoteOutcome
from app.container import Container
from app.core.errors import AppError, ErrorClass, InputValidationError
from app.live import model as live_model
from app.live.authority import LiveAuthorityService
from app.live.model import GrantState, MutationStage
from app.live.stack import SafetyStack
from app.live.store import ArtifactRef, LiveAuthorityStore
from app.products.model import ReadinessStatus
from app.register.authoring import AuthoredInputs, decode_inputs, encode_inputs
from app.register.execution import ExecutionRefused
from app.register.model import IntentState
from app.register.store import RegistrationStore
from tests.integration.test_m5_register_execution import (
    FakeSender,
    _pause,
    context,
    execution,
    prepare,
)
from tests.live_support import PermittedMode, ProvenProofs
from tests.product_support import Collections, count
from tests.register_support import (
    MARKET,
    Preparation,
    draft,
    establish,
    no_match,
    preparation,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration

APPROVAL = "5826077470"
CID = "corr-g3a-create"


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def prep(container: Container, account: str) -> Preparation:
    return preparation(container, account)


@pytest.fixture
def store(container: Container) -> RegistrationStore:
    return container.registrations


def permitted(container: Container, **proofs: frozenset[str]) -> SafetyStack:
    return SafetyStack(
        store=LiveAuthorityStore(container.db, container.clock, container.audit),
        mode=PermittedMode(),
        proofs=ProvenProofs(**proofs),
        clock=container.clock,
    )


def create_grant(container: Container, intent_id: str) -> str:
    now = container.clock.now()
    return container.live_authority.issue_create_grant(
        intent_id=intent_id,
        not_before=now,
        expires_at=now + timedelta(hours=1),
        approved_by="operator",
        authorization_ref=APPROVAL,
        correlation_id=CID,
    ).grant_id


def release(container: Container) -> None:
    container.live_authority.release_brake(
        actor="operator",
        reason_code="CANARY_WINDOW",
        authorization_ref=APPROVAL,
        correlation_id=CID,
    )


def test_the_production_stack_refuses_every_create_at_this_main(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    grant_id = create_grant(container, ready.intent_id)
    release(container)
    run = execution(container, prep, authority=container.safety_stack)
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready))
    assert refused.value.code == live_model.MODE_NOT_LIVE
    reasons = {layer["reason"] for layer in refused.value.details["layers"]}
    assert live_model.RESTORE_PROOF_ABSENT in reasons
    # The unit rolled back: no Attempt, the Intent unmoved, nothing sent, nothing spent.
    assert store.attempts(ready.intent_id) == ()
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.PREPARED
    assert run.sender.calls == []
    grant = container.live_authority.grant_record(grant_id)
    assert grant is not None and (grant.budget_used, grant.state) == (0, GrantState.ACTIVE)
    events = [e.event_type for e in container.audit.list_events(limit=200)]
    assert "LIVE_MUTATION_REFUSED" in events


def test_a_permitted_stack_spends_exactly_the_grant_of_this_attempt(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    release(container)
    stack = permitted(container)
    run = execution(container, prep, authority=stack)
    # No grant: refused before any attempt, whatever else is proven.
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready))
    assert refused.value.code == live_model.GRANT_MISSING
    assert store.attempts(ready.intent_id) == () and run.sender.calls == []
    grant_id = create_grant(container, ready.intent_id)
    result = run.service.run(context(ready))
    assert result.intent_state is IntentState.CONFIRMED and len(run.sender.calls) == 1
    grant = container.live_authority.grant_record(grant_id)
    assert grant is not None and (grant.budget_used, grant.state) == (1, GrantState.EXHAUSTED)
    assert grant.create_attempt_no == 1 and grant.stage is MutationStage.CREATE


def test_a_retry_after_a_proven_non_application_needs_a_new_grant(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    release(container)
    sender = FakeSender(
        outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
        product_id=None,
        error_class=ErrorClass.TRANSIENT,
        error_code="SMARTSTORE_5XX",
    )
    run = execution(container, prep, authority=permitted(container), sender=sender)
    create_grant(container, ready.intent_id)
    with pytest.raises(AppError):
        run.service.run(context(ready))
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.FAILED
    # The first grant named attempt 1 and is spent: the retry is refused before any attempt.
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready, attempt_no=2))
    assert refused.value.code == live_model.GRANT_MISSING
    assert len(store.attempts(ready.intent_id)) == 1 and len(sender.calls) == 1
    # A new grant names attempt 2, and only then may it be sent.
    second = create_grant(container, ready.intent_id)
    sender.outcome, sender.product_id = RemoteOutcome.APPLIED_PROVEN, "9900112233"
    run.service.run(context(ready, attempt_no=2))
    stored = container.live_authority.grant_record(second)
    assert stored is not None and stored.create_attempt_no == 2 and stored.budget_used == 1


def test_the_create_only_scope_brake_still_refuses_first_and_spends_nothing(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    release(container)
    grant_id = create_grant(container, ready.intent_id)
    from app.register.execution import CREATE_ENDPOINT_GROUP

    _pause(store, account, CREATE_ENDPOINT_GROUP)
    run = execution(container, prep, authority=permitted(container))
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready))
    assert refused.value.code == "REGISTER_SCOPE_PAUSED"
    grant = container.live_authority.grant_record(grant_id)
    assert grant is not None and grant.budget_used == 0
    assert count(container.config, "asset_upload_attempts") == 0


def test_a_create_grant_names_a_sendable_intent_and_its_next_attempt(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep, authority=permitted(container))
    release(container)
    create_grant(container, ready.intent_id)
    run.service.run(context(ready))
    # A CONFIRMED Intent is no longer sendable: no grant can name it.
    with pytest.raises(InputValidationError) as refused:
        create_grant(container, ready.intent_id)
    assert refused.value.code == "LIVE_GRANT_INTENT_NOT_SENDABLE"


def test_an_unspent_grant_for_an_earlier_attempt_never_admits_the_retry(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # Two grants both name attempt 1. The first is spent on it and it is proven not applied; the
    # second is still ACTIVE with budget, yet it names attempt 1, so the retry is refused (G3-27).
    ready = prepare(container, sources, store, account, prep)
    release(container)
    sender = FakeSender(
        outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
        product_id=None,
        error_class=ErrorClass.TRANSIENT,
        error_code="SMARTSTORE_5XX",
    )
    run = execution(container, prep, authority=permitted(container), sender=sender)
    both = (create_grant(container, ready.intent_id), create_grant(container, ready.intent_id))
    with pytest.raises(AppError):
        run.service.run(context(ready))
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready, attempt_no=2))
    assert refused.value.code == live_model.GRANT_MISSING
    states = sorted(
        (g.state.value, g.budget_used)
        for g in (container.live_authority.grant_record(i) for i in both)
        if g is not None
    )
    # One was spent on attempt 1; the other stays unspent and still cannot admit attempt 2.
    assert states == [("ACTIVE", 0), ("EXHAUSTED", 1)]
    assert len(sender.calls) == 1


@pytest.mark.parametrize("end", ["expired", "revoked"])
def test_an_expired_or_revoked_create_grant_never_matches(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
    end: str,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    release(container)
    grant_id = create_grant(container, ready.intent_id)
    if end == "expired":
        container.clock.advance(2 * 3600)  # type: ignore[attr-defined]
    else:
        container.live_authority.revoke(
            grant_id, actor="operator", reason_code="OPERATOR_REVOKED", correlation_id=CID
        )
    run = execution(container, prep, authority=permitted(container))
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready))
    assert refused.value.code == live_model.GRANT_MISSING
    assert store.attempts(ready.intent_id) == () and run.sender.calls == []


# ---------------------------------------------------------------- the ASSET grant unit (B1)


class Evaluator:
    """The preparation owner's candidate evaluation, answered by the real preflight owner."""

    def __init__(self, result: Any) -> None:
        self.result = result

    def evaluate(self, preparation_id: str) -> Any:
        return self.result


def ready_preparation(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> tuple[str, Any, Any]:
    """A durable preparation of one real M4 Item, and the real READY candidate of its unit."""
    item = ready_item(container, sources, "1234")
    draft_id = draft(store, account, [item])
    req = request(store, draft_id, account, [item])
    first = prep.service.candidate(req)
    req = replace(req, duplicate_evidence=no_match(first))
    candidate = prep.service.candidate(req)
    assert candidate.status is ReadinessStatus.READY and candidate.upload_permitted
    with store.transaction() as unit:
        record = unit.create_preparation(
            draft_id,
            item_ids=list(candidate.resolved.unit_item_ids),
            inputs=encode_inputs(AuthoredInputs(req.category, req.listing, req.detail)),
            created_by="operator",
            correlation_id=CID,
        )
    return record.current.preparation_revision_id, candidate, first


def asset_grant(
    authority: LiveAuthorityService, container: Container, account: str, **values: Any
) -> Any:
    now = container.clock.now()
    return authority.issue_asset_grant(
        **(
            {
                "marketplace_key": MARKET,
                "marketplace_account_id": account,
                "budget": 2,
                "not_before": now,
                "expires_at": now + timedelta(hours=1),
                "approved_by": "operator",
                "authorization_ref": APPROVAL,
                "correlation_id": CID,
            }
            | values
        )
    )


def test_an_asset_grant_is_derived_from_the_owners_never_from_the_caller(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    revision, candidate, not_ready = ready_preparation(container, sources, store, account, prep)
    authority = LiveAuthorityService(
        store=LiveAuthorityStore(container.db, container.clock, container.audit),
        registrations=store,
        preparations=Evaluator(candidate),
    )
    selected = sorted(
        {
            ArtifactRef(image.asset_kind, image.sha256, image.derivation_id)
            for item in candidate.resolved.items
            for image in item.images
        },
        key=lambda a: a.sha256,
    )
    assert selected
    profile = candidate.resolved.target.asset_policy.profile
    exact = {
        "preparation_revision_id": revision,
        "candidate_fingerprint": candidate.candidate_fingerprint,
        "artifacts": selected,
        "asset_profile": profile,
    }
    other = selected[0]
    wrong = [
        ({"candidate_fingerprint": "c" * 64}, "LIVE_GRANT_CANDIDATE_MISMATCH"),
        ({"artifacts": [*selected, ArtifactRef(other.asset_kind, "d" * 64, None)]},
         "LIVE_GRANT_ARTIFACT_SET_MISMATCH"),  # an extra artifact
        ({"artifacts": selected[1:]}, "LIVE_GRANT_ARTIFACT_SET_MISMATCH"),  # a missing one
        ({"artifacts": [ArtifactRef(other.asset_kind, other.sha256, "substituted-derivation"),
                        *selected[1:]]}, "LIVE_GRANT_ARTIFACT_SET_MISMATCH"),  # substituted
        ({"artifacts": [*selected, selected[0]]}, "LIVE_GRANT_ARTIFACT_SET_MISMATCH"),
        ({"asset_profile": "caller-chosen-profile"}, "LIVE_GRANT_PROFILE_MISMATCH"),
    ]  # fmt: skip
    for override, code in wrong:
        with pytest.raises(InputValidationError) as refused:
            asset_grant(authority, container, account, **(exact | override))
        assert refused.value.code == code, override
    # A candidate that is not READY, or that permits no upload, grants nothing.
    blocked = LiveAuthorityService(
        store=LiveAuthorityStore(container.db, container.clock, container.audit),
        registrations=store,
        preparations=Evaluator(not_ready),
    )
    with pytest.raises(InputValidationError) as refused:
        asset_grant(blocked, container, account, **exact)
    assert refused.value.code == "LIVE_GRANT_CANDIDATE_NOT_READY"
    assert count(container.config, "live_grants") == 0
    # The exact unit is granted, bound to what the owners derived.
    granted = asset_grant(authority, container, account, **exact)
    assert granted.candidate_fingerprint == candidate.candidate_fingerprint
    assert sorted(granted.artifacts, key=lambda a: a.sha256) == selected
    assert granted.asset_profile == profile


def test_an_asset_grant_binds_only_the_current_preparation_revision(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    revision, candidate, _ = ready_preparation(container, sources, store, account, prep)
    preparation = store.preparation_of_revision(revision)
    assert preparation is not None
    with store.transaction() as unit:
        unit.revise_preparation(
            preparation.preparation_id,
            item_ids=list(preparation.current.item_ids),
            inputs=encode_inputs(decode_inputs(preparation.current)),
            authored_by="operator",
            correlation_id=CID,
        )
    authority = LiveAuthorityService(
        store=LiveAuthorityStore(container.db, container.clock, container.audit),
        registrations=store,
        preparations=Evaluator(candidate),
    )
    with pytest.raises(InputValidationError) as refused:
        asset_grant(
            authority,
            container,
            account,
            preparation_revision_id=revision,
            candidate_fingerprint=candidate.candidate_fingerprint,
            artifacts=[
                ArtifactRef(image.asset_kind, image.sha256, image.derivation_id)
                for item in candidate.resolved.items
                for image in item.images
            ],
            asset_profile=candidate.resolved.target.asset_policy.profile,
        )
    assert refused.value.code == "LIVE_GRANT_PREPARATION_NOT_CURRENT"
    assert count(container.config, "live_grants") == 0
