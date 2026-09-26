"""M5 PR-E on real owners: the CREATE execution state machine under the M0 job runner.

Everything durable here is real — the registration store and its triggers, the Job/JobAttempt
tables, the runner with its RetryPolicy, and a real frozen Snapshot from the PR-C path. Only the
provider seams are fakes, which is what lets the whole state machine be exercised while the
production CREATE stays NOT_ADOPTED and **no marketplace mutation is reachable** (kickoff §12).

The numbered comments name the kickoff §13 behaviours each test pins.
"""

import contextlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import pytest

from app.config import AppConfig
from app.connect.marketplace.capability import RemoteOutcome
from app.container import Container
from app.core.errors import AppError, ErrorClass
from app.jobs.models import JobState
from app.products.model import ReadinessStatus
from app.register.builder import RegistrationSnapshotBuilder
from app.register.execution import (
    AUTH_RESUMABLE,
    CREATE_JOB_TYPE,
    CREATE_POLICY,
    AttemptFailed,
    ExecutionPolicy,
    ExecutionRefused,
    RegistrationExecutionService,
    create_job_definition,
    encode_send_request,
    enqueue_create,
    target_ref,
)
from app.register.execution import (
    CREATE_ENDPOINT_GROUP as ENDPOINT_GROUP,
)
from app.register.model import (
    OPERATOR_RESUMABLE,
    ExecutionScopeState,
    IntentState,
    Operation,
    ResolutionEvidence,
    ResolvedBy,
    ScopePauseReason,
    VerificationState,
)
from app.register.preparation import (
    FieldValue,
    PreflightRequest,
    PreflightResult,
    PreparedAsset,
)
from app.register.provider import CreateHandoff
from app.register.store import RegistrationStore, RegistrationUnit, ScopeRecord
from integrations.marketplaces.smartstore import readback as smartstore_readback
from integrations.marketplaces.smartstore.execution import (
    CreateNotAdoptedError,
    ReconcileLookupNotAdoptedError,
    SmartStoreCreateSender,
    SmartStoreReconcileLookup,
)
from tests.live_support import AdmittingAuthority
from tests.product_support import Collections, count, raw
from tests.register_support import (
    CID,
    MARKET,
    OPERATOR,
    Preparation,
    draft,
    establish,
    preparation,
    prepared,
    ready_final,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration

PRODUCT_NO = "9900112233"
# A second endpoint group, owned by the scope owner but executed by no owner in M5.
OTHER_GROUP = "product_inquiry"
AT = "2026-09-20 00:00:00"


# ---------------------------------------------------------------- fake provider seams


@dataclass
class FakeSender:
    """A CREATE handoff that proves exactly what the test says, and counts every call."""

    outcome: RemoteOutcome = RemoteOutcome.APPLIED_PROVEN
    product_id: str | None = PRODUCT_NO
    error_class: ErrorClass | None = None
    error_code: str | None = None
    is_available: bool = True
    calls: list[Mapping[str, Any]] = field(default_factory=list)
    raises: Exception | None = None

    def available(self) -> bool:
        return self.is_available

    def send(
        self, *, payload: Mapping[str, Any], idempotency_key: str, listing_identity: str
    ) -> CreateHandoff:
        self.calls.append({"idempotency_key": idempotency_key, "listing": listing_identity})
        if self.raises is not None:
            raise self.raises
        return CreateHandoff(
            remote_outcome=self.outcome,
            sanitized_request={"listing_identity": listing_identity},
            marketplace_product_id=(
                self.product_id if self.outcome is RemoteOutcome.APPLIED_PROVEN else None
            ),
            response_status=200 if self.outcome is RemoteOutcome.APPLIED_PROVEN else 500,
            sanitized_response={"accepted": True},
            error_class=self.error_class,
            error_code=self.error_code,
        )


@dataclass
class FakeReadback:
    """A read-back that returns whatever retained response the test set."""

    retained: Mapping[str, Any] = field(default_factory=dict)
    is_available: bool = True
    calls: int = 0
    raises: Exception | None = None

    def available(self) -> bool:
        return self.is_available

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return dict(self.retained)


@dataclass
class FakeLookup:
    found: Mapping[str, Any] = field(default_factory=dict)
    is_available: bool = False
    calls: int = 0

    def available(self) -> bool:
        return self.is_available

    def find(self, *, marketplace_account_id: str, listing_identity: str) -> Mapping[str, Any]:
        self.calls += 1
        return dict(self.found)


@dataclass
class FakeComparison:
    verdict: Any
    comparison_contract_version: str = "test-comparison/v1"
    normalizer_version: str = "test-normalizer/v1"
    reasons: tuple[str, ...] = ()
    normalized: Mapping[str, Any] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "comparison_contract_version": self.comparison_contract_version,
            "normalizer_version": self.normalizer_version,
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "normalized": dict(self.normalized),
        }


@dataclass
class FakeComparator:
    """Compares like PR-D does, but lets a test choose the verdict and the published state."""

    verdict: Any = smartstore_readback.ReadbackVerdict.MATCH
    published_state: str | None = "SALE"
    reasons: tuple[str, ...] = ()
    seen: list[Mapping[str, Any]] = field(default_factory=list)

    def compare(
        self, snapshot_payload: Mapping[str, Any], retained: Mapping[str, Any]
    ) -> FakeComparison:
        self.seen.append(snapshot_payload)
        normalized: dict[str, Any] = {"retained": dict(retained)}
        if self.published_state is not None:
            normalized["published_state"] = self.published_state
        return FakeComparison(verdict=self.verdict, reasons=self.reasons, normalized=normalized)


@dataclass
class FakeProjection:
    sendable: bool = True
    gaps: tuple[str, ...] = ()


@dataclass
class FakeProjector:
    sendable: bool = True

    def __call__(self, payload: Mapping[str, Any]) -> FakeProjection:
        return FakeProjection(sendable=self.sendable, gaps=() if self.sendable else ("test gap",))


@dataclass
class Execution:
    """One execution service over the real container, with fake provider seams."""

    service: RegistrationExecutionService
    sender: FakeSender
    readback: FakeReadback
    lookup: FakeLookup
    comparator: FakeComparator
    projector: FakeProjector


# ---------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def sources(container: Container, config: AppConfig) -> Collections:
    return Collections.of(container, config)


@pytest.fixture
def account(container: Container, config: AppConfig) -> str:
    return establish(container, config, MARKET, "uid-market-a-1")


@pytest.fixture
def prep(container: Container, account: str) -> Preparation:
    return preparation(container, account)


@pytest.fixture
def store(container: Container) -> RegistrationStore:
    return container.registrations


def execution(container: Container, prep: Preparation, **overrides: Any) -> Execution:
    sender = overrides.get("sender") or FakeSender()
    readback = overrides.get("readback") or FakeReadback(retained={"originProduct": {"name": "x"}})
    lookup = overrides.get("lookup") or FakeLookup()
    comparator = overrides.get("comparator") or FakeComparator()
    projector = overrides.get("projector") or FakeProjector()
    service = RegistrationExecutionService(
        registrations=container.registrations,
        preflight=prep.service,
        sender=sender,
        readback=readback,
        lookup=lookup,
        capability=prep.capability,
        compare=comparator,
        projection=projector,
        clock=container.clock,
        authority=overrides.get("authority") or AdmittingAuthority(),
        policy=overrides.get("policy") or ExecutionPolicy(),
    )
    return Execution(service, sender, readback, lookup, comparator, projector)


@dataclass
class Prepared:
    """One real frozen Snapshot with its Intent and the job payload that sends it."""

    intent_id: str
    snapshot_id: str
    request: PreflightRequest
    assets: tuple[PreparedAsset, ...]
    final: PreflightResult
    draft_id: str = ""


def prepare(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
    source_product_id: str = "1234",
) -> Prepared:
    item = ready_item(container, sources, source_product_id)
    draft_id = draft(store, account, [item])
    req = request(store, draft_id, account, [item])
    req, final = ready_final(prep, req)
    builder = RegistrationSnapshotBuilder(preflight=prep.service, registrations=store)
    snapshot = builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    with store.transaction() as unit:
        batch = unit.create_batch(
            MARKET, snapshot.marketplace_account_id, created_by=OPERATOR, correlation_id=CID
        )
        intent = unit.create_intent(
            batch, snapshot.registration_snapshot_id, created_by=OPERATOR, correlation_id=CID
        )
    return Prepared(
        intent.intent_id,
        snapshot.registration_snapshot_id,
        req,
        tuple(prepared(final)),
        final,
        draft_id,
    )


def prepare_without_assets(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> Prepared:
    """One frozen Snapshot of a target that needs no provider-issued asset (PR-C's asset-free
    path): nothing binds the candidate, so the fingerprint alone guards a drift."""
    from tests.register_support import no_match

    item = ready_item(container, sources, "1234")
    draft_id = draft(store, account, [item])
    req = request(store, draft_id, account, [item])
    req = replace(req, duplicate_evidence=no_match(prep.service.candidate(req)))
    final = prep.service.final(req, ())
    assert final.status is ReadinessStatus.READY, final.reasons
    builder = RegistrationSnapshotBuilder(preflight=prep.service, registrations=store)
    snapshot = builder.freeze(final, created_by=OPERATOR, correlation_id=CID)
    with store.transaction() as unit:
        batch = unit.create_batch(
            MARKET, snapshot.marketplace_account_id, created_by=OPERATOR, correlation_id=CID
        )
        intent = unit.create_intent(
            batch, snapshot.registration_snapshot_id, created_by=OPERATOR, correlation_id=CID
        )
    return Prepared(intent.intent_id, snapshot.registration_snapshot_id, req, (), final, draft_id)


def failing(
    container: Container,
    prep: Preparation,
    error_class: ErrorClass,
    error_code: str,
    **overrides: Any,
) -> Execution:
    """An execution whose provider proves a failure of this class, and nothing else."""
    return execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=error_class,
            error_code=error_code,
        ),
        **overrides,
    )


def scope_of(store: RegistrationStore, account: str, group: str = ENDPOINT_GROUP) -> ScopeRecord:
    return store.execution_scope(MARKET, account, group)


def _pause(store: RegistrationStore, account: str, group: str) -> None:
    """Engage one scope's brake through the owner itself: M5 executes only the CREATE group."""
    with store.transaction() as unit:
        unit.pause_scope(
            MARKET,
            account,
            group,
            reason=ScopePauseReason.POLICY,
            policy_version=ExecutionPolicy().version,
            error_class=ErrorClass.POLICY_BLOCKED,
            actor=OPERATOR,
            correlation_id=CID,
        )


def context(ready: Prepared, *, attempt_no: int = 1) -> Any:
    from app.jobs.registry import JobContext

    return JobContext(
        job_id="job-test-1",
        job_type=CREATE_JOB_TYPE,
        attempt_no=attempt_no,
        max_attempts=CREATE_POLICY.max_attempts,
        correlation_id=CID,
        target_ref=target_ref(ready.intent_id),
        payload=encode_send_request(
            ready.intent_id,
            ready.request,
            ready.assets,
            listing_identity=ready.final.resolved.listing_identity,
            identity_generation=ready.final.resolved.identity_generation,
        ),
    )


# ---------------------------------------------------------------- the happy path (5, 6, 24, 25)


def test_an_applied_create_is_confirmed_only_by_its_read_back(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    result = run.service.run(context(ready))
    # 5 + 6: the CREATE proved an identity, and only the read-back confirmed the Intent.
    assert len(run.sender.calls) == 1 and run.readback.calls == 1
    assert (result.action, result.intent_state) == ("CONFIRMED", IntentState.CONFIRMED)
    intent = store.intent(ready.intent_id)
    assert intent is not None
    assert (intent.state, intent.remote_outcome, intent.verification_state) == (
        IntentState.CONFIRMED,
        RemoteOutcome.APPLIED_PROVEN,
        VerificationState.PASS,
    )
    assert intent.marketplace_product_id == PRODUCT_NO
    assert count(container.config, "marketplace_registrations") == 1
    # The comparison expectation was the immutable Snapshot payload, never a Draft.
    (compared,) = run.comparator.seen
    assert compared["listing_identity"] and compared["items"]
    # 24: nothing here promotes the capability.
    assert container.marketplace_capability.capability("smartstore").write.status.value == (
        "UNVERIFIED"
    )


def test_a_confirmed_intent_replays_as_a_no_op(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    run.service.run(context(ready))
    # 23: a second run of the same job neither sends nor reads again.
    again = run.service.run(context(ready, attempt_no=2))
    assert (again.action, again.intent_state) == ("NO_OP_CONFIRMED", IntentState.CONFIRMED)
    assert len(run.sender.calls) == 1 and run.readback.calls == 1


# ---------------------------------------------------------------- the send gate (3, 4)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"sender": FakeSender(is_available=False)}, "REGISTER_CREATE_NOT_ADOPTED"),
        ({"projector": FakeProjector(sendable=False)}, "REGISTER_WIRE_NOT_SENDABLE"),
    ],
)
def test_the_gate_blocks_before_any_attempt_or_provider_call(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
    overrides: dict[str, Any],
    code: str,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep, **overrides)
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready))
    assert refused.value.code == code
    # 3 + 4: no Attempt was opened, the Intent never moved, and nothing was sent.
    assert store.attempts(ready.intent_id) == ()
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.PREPARED
    assert run.sender.calls == []


def test_a_dependency_that_moved_blocks_the_send_on_the_fingerprint(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # A target that needs no provider-issued asset has no prepared asset binding the candidate,
    # so a drift that stays READY is caught by the fingerprint alone — the same guard PR-C's
    # builder relies on.
    from app.register.policy import AssetPolicy
    from tests.register_support import target

    prep.policies.put(
        target(
            account,
            asset_policy=AssetPolicy(
                profile="asset-profile-test-1", provider_asset_identity_required=False
            ),
        )
    )
    ready = prepare_without_assets(container, sources, store, account, prep)
    run = execution(container, prep)
    renamed = replace(
        ready.request,
        listing=replace(ready.request.listing, name=FieldValue("완전히 다른 이름")),
    )
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(replace(ready, request=renamed)))
    assert refused.value.code == "REGISTER_SEND_FINGERPRINT_DRIFT"
    assert store.attempts(ready.intent_id) == ()
    assert run.sender.calls == []


def test_a_preflight_that_is_no_longer_ready_blocks_the_send(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    # The account's registration policy moved after the Snapshot was frozen, so the assets the
    # Snapshot named no longer bind to the current candidate: current truth is not READY.
    from tests.register_support import target

    prep.policies.put(target(account, policy_revision="policy-test-2"))
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready))
    assert refused.value.code == "REGISTER_SEND_PREFLIGHT_NOT_READY"
    assert store.attempts(ready.intent_id) == ()
    assert run.sender.calls == []


def test_a_prepared_asset_that_is_not_the_snapshots_blocks_the_send(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    foreign = tuple(
        replace(asset, provider_asset_ref="provider-asset-elsewhere") for asset in ready.assets
    )
    job = context(replace(ready, assets=foreign))
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(job)
    # Checked directly against the durable Snapshot, before anything is re-derived.
    assert refused.value.code == "REGISTER_SEND_ASSET_DRIFT"
    assert store.attempts(ready.intent_id) == () and run.sender.calls == []


def test_a_send_request_naming_another_unit_blocks_the_send(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    job = context(ready)
    payload = dict(job.payload)
    payload["unit_identity"] = {"listing_identity": "icbm-" + "f" * 32, "generation": 0}
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(replace(job, payload=payload))
    assert refused.value.code == "REGISTER_SEND_SCOPE_MISMATCH"
    assert store.attempts(ready.intent_id) == () and run.sender.calls == []


# ---------------------------------------------------------------- outcomes (7, 9, 10, 18)


def test_a_read_back_mismatch_never_confirms_and_never_creates_again(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        comparator=FakeComparator(
            verdict=smartstore_readback.ReadbackVerdict.MISMATCH, reasons=("OPTION_UNITS_MISSING",)
        ),
    )
    with pytest.raises(AttemptFailed) as failed:
        run.service.run(context(ready))
    # 7 + 8: not confirmed, and the cause is never automatically retryable.
    assert failed.value.code == "REGISTER_READBACK_MISMATCH"
    assert failed.value.error_class is ErrorClass.REVIEW_REQUIRED
    intent = store.intent(ready.intent_id)
    assert intent is not None
    assert (intent.state, intent.verification_state) == (
        IntentState.SENT,
        VerificationState.MISMATCH,
    )
    assert count(container.config, "marketplace_registrations") == 0
    # 18: the next run reads back only; the CREATE count stays one.
    run.comparator.verdict = smartstore_readback.ReadbackVerdict.MATCH
    run.comparator.reasons = ()
    run.service.run(context(ready, attempt_no=2))
    assert len(run.sender.calls) == 1 and run.readback.calls == 2


def test_an_unreadable_read_back_is_never_a_confirmation(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        comparator=FakeComparator(verdict=smartstore_readback.ReadbackVerdict.UNREADABLE),
    )
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    assert count(container.config, "marketplace_registrations") == 0


def test_a_match_without_a_proven_published_state_is_not_confirmed(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep, comparator=FakeComparator(published_state=None))
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready))
    # ADR-0014 §11 compares the published state exactly; an unproven one is never invented.
    assert refused.value.code == "REGISTER_PUBLISHED_STATE_UNPROVEN"
    assert count(container.config, "marketplace_registrations") == 0


def test_a_proven_not_applied_failure_is_retryable_with_the_same_intent(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.TRANSIENT,
            error_code="PROVIDER_TIMEOUT",
        ),
    )
    with pytest.raises(AttemptFailed) as failed:
        run.service.run(context(ready))
    # 9: the provider's class may decide a retry only now that the outcome is proven.
    assert failed.value.error_class is ErrorClass.TRANSIENT
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.FAILED
    # The retry is another Attempt of the same Intent and Snapshot, never a new one.
    run.sender.outcome = RemoteOutcome.APPLIED_PROVEN
    run.sender.product_id = PRODUCT_NO
    run.sender.error_class = None
    run.service.run(context(ready, attempt_no=2))
    attempts = store.attempts(ready.intent_id)
    assert [a.attempt_no for a in attempts] == [1, 2]
    assert store.intent(ready.intent_id).registration_snapshot_id == ready.snapshot_id  # type: ignore[union-attr]


def test_an_unknown_outcome_is_never_retried_and_never_resent(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.UNKNOWN,
            product_id=None,
            error_class=ErrorClass.TRANSIENT,
            error_code="PROVIDER_TIMEOUT",
        ),
    )
    with pytest.raises(AttemptFailed) as failed:
        run.service.run(context(ready))
    # 10: the same transient cause, but the outcome is unproven, so the job is not retryable.
    assert failed.value.code == "REGISTER_OUTCOME_UNKNOWN"
    assert failed.value.error_class is ErrorClass.REVIEW_REQUIRED
    assert not CREATE_POLICY.allows_retry(failed.value.error_class, 1)
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.UNKNOWN
    # A further run refuses before anything, and the CREATE count stays one.
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(ready, attempt_no=2))
    assert refused.value.code == "REGISTER_UNKNOWN_REQUIRES_RECONCILE"
    assert len(run.sender.calls) == 1


# ---------------------------------------------------------------- reconcile (13, 14)


def _unknown(
    container: Container, store: RegistrationStore, prep: Preparation, ready: Prepared
) -> Execution:
    run = execution(
        container,
        prep,
        sender=FakeSender(outcome=RemoteOutcome.UNKNOWN, product_id=None),
    )
    with contextlib.suppress(AttemptFailed):
        run.service.run(context(ready))
    return run


@pytest.mark.parametrize(
    "found",
    [
        {"marketplace_product_id": PRODUCT_NO, "evidence": "sanitized"},
        {"absence_proven": True, "evidence": "sanitized"},
        {"searched": True},
    ],
    ids=["a-match", "a-claimed-absence", "nothing"],
)
def test_no_lookup_result_ever_resolves_an_unknown(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
    found: Mapping[str, Any],
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = _unknown(container, store, prep, ready)
    # ADR-0014 §17.2, §28: even an available lookup is never consulted. A match is not a presence
    # proof (§28.2), and no lookup ever proves absence, so the Intent stays UNKNOWN.
    run.lookup.is_available = True
    run.lookup.found = found
    with pytest.raises(ExecutionRefused) as refused:
        run.service.reconcile(ready.intent_id, correlation_id=CID)
    assert refused.value.code == "REGISTER_RECONCILE_UNAVAILABLE"
    assert run.lookup.calls == 0
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.UNKNOWN
    assert intent.marketplace_product_id is None
    # §28.3: nothing freed it, so no CREATE can be queued for it.
    with pytest.raises(ExecutionRefused) as queued:
        enqueue_create(
            container.jobs,
            container.registrations,
            intent_id=ready.intent_id,
            request=ready.request,
            frozen=ready.final,
        )
    assert queued.value.code == "REGISTER_INTENT_NOT_SENDABLE"


def test_without_an_adopted_lookup_the_unknown_stays_unresolved(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = _unknown(container, store, prep, ready)
    with pytest.raises(ExecutionRefused) as refused:
        run.service.reconcile(ready.intent_id, correlation_id=CID)
    # 14 + PR-D: nothing is fabricated, and the conflict scope is not freed.
    assert refused.value.code == "REGISTER_RECONCILE_UNAVAILABLE"
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.UNKNOWN


def test_only_an_unknown_intent_is_reconciled(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    with pytest.raises(ExecutionRefused) as refused:
        run.service.reconcile(ready.intent_id, correlation_id=CID)
    assert refused.value.code == "REGISTER_NOT_UNKNOWN"


def test_a_read_back_needs_the_provider_identity(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    with pytest.raises(ExecutionRefused) as refused:
        run.service.verify(ready.intent_id, correlation_id=CID)
    # §11: without the provider product identity there is nothing to read back.
    assert refused.value.code == "REGISTER_NO_PROVIDER_IDENTITY"
    assert run.readback.calls == 0


def test_an_operator_assertion_alone_cannot_resolve_an_unknown(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    _unknown(container, store, prep, ready)
    # 14: the resolution vocabulary has no operator-assertion evidence, and USER is only an
    # actor. The only way in is a machine/provider proof, which this account has none of.
    assert "ASSERTION" not in {e.value for e in ResolutionEvidence}
    assert ResolvedBy.USER.value == "USER"
    with pytest.raises(ExecutionRefused):
        execution(container, prep).service.reconcile(ready.intent_id, correlation_id=CID)
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.UNKNOWN


# ------------------------------------------------- the job owner (1, 2, 15-17, 20-22)


def _jobs(container: Container, run: Execution) -> None:
    """Point the container's registered CREATE job type at this test's execution service.

    The container already registers the real definition (with the production, unavailable
    SmartStore seams); a test swaps in one bound to its fakes, exactly as the COLLECT lifecycle
    tests do, so the runner, the retry policy and the terminal hooks stay the real ones.
    """
    container.job_registry._definitions[CREATE_JOB_TYPE] = create_job_definition(
        run.service, retry_policy=CREATE_POLICY
    )


def test_the_same_snapshot_keeps_one_intent_and_one_idempotency_key(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    intent = store.intent(ready.intent_id)
    assert intent is not None
    # 1: the identity is deterministic from the marketplace, account, operation and the exact
    # Snapshot, so asking again — after a restart, or concurrently — returns the same Intent
    # rather than opening a second one.
    with store.transaction() as unit:
        batch = unit.create_batch(MARKET, account, created_by=OPERATOR, correlation_id=CID)
        again = unit.create_intent(
            batch, ready.snapshot_id, created_by=OPERATOR, correlation_id=CID
        )
    assert (again.intent_id, again.idempotency_key) == (intent.intent_id, intent.idempotency_key)
    assert count(container.config, "registration_intents") == 1


def test_a_second_dispatch_cannot_open_a_competing_attempt(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    with store.transaction() as unit:
        unit.start_attempt(
            ready.intent_id,
            sanitized_request={"request": "sanitized"},
            sanitizer_profile_version="sanitizer-test-1",
            correlation_id=CID,
        )
    # 2: the Intent is SENT with one open attempt; a concurrent run neither sends nor opens a
    # second attempt — it can only read back, and this one has no provider identity yet.
    run = execution(container, prep)
    with pytest.raises(AppError):
        run.service.run(context(ready))
    assert len(store.attempts(ready.intent_id)) == 1
    assert run.sender.calls == []


def test_a_second_enqueue_never_bypasses_the_first_job_backoff(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.TRANSIENT,
            error_code="PROVIDER_TIMEOUT",
        ),
    )
    _jobs(container, run)
    first = enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    # A double enqueue is the same queued work, never a second job that could run at once.
    assert (
        enqueue_create(
            container.jobs,
            container.registrations,
            intent_id=ready.intent_id,
            request=ready.request,
            frozen=ready.final,
        )
        == first
    )
    assert container.jobs.count(job_type_prefix=CREATE_JOB_TYPE) == 1
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.RETRY_SCHEDULED
    scheduled = container.jobs.get(first).next_attempt_at
    assert scheduled is not None
    assert (scheduled - container.clock.now()).total_seconds() == pytest.approx(60.0)
    # Enqueuing again while the first job waits out its backoff yields that same job, and the
    # runner has nothing due, so no CREATE happens before the policy says so.
    assert (
        enqueue_create(
            container.jobs,
            container.registrations,
            intent_id=ready.intent_id,
            request=ready.request,
            frozen=ready.final,
        )
        == first
    )
    assert container.jobs.count(job_type_prefix=CREATE_JOB_TYPE) == 1
    assert container.runner.run_next() is None
    assert len(run.sender.calls) == 1
    assert len(store.attempts(ready.intent_id)) == 1
    # Once the backoff has elapsed the same job runs attempt 2 — the retry schedule is the job
    # system's, and nothing bypassed it.
    container.clock.advance(60)
    assert container.runner.run_next() is not None
    assert len(run.sender.calls) == 2
    assert [a.attempt_no for a in store.attempts(ready.intent_id)] == [1, 2]


def test_two_concurrent_enqueues_leave_exactly_one_live_job(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.TRANSIENT,
            error_code="PROVIDER_TIMEOUT",
        ),
    )
    _jobs(container, run)

    # Force the interleaving the guard exists for: the first caller does not leave its check
    # until the second has had its chance to check too. When the check and the insert share one
    # transaction the second caller cannot even reach its check until the first commits, so this
    # wait times out harmlessly; when they do not, both callers see "no live job" and both queue.
    first_checked = threading.Event()
    second_checked = threading.Event()
    original = RegistrationUnit.active_job

    def interleaved(
        self: RegistrationUnit, job_type: str, intent_id: str, states: Sequence[str]
    ) -> str | None:
        answer = original(self, job_type, intent_id, states)
        if not first_checked.is_set():
            first_checked.set()
            second_checked.wait(timeout=0.5)
        else:
            second_checked.set()
        return answer

    monkeypatch.setattr(RegistrationUnit, "active_job", interleaved)

    started = threading.Barrier(2)
    queued: list[str] = []
    failures: list[BaseException] = []

    def enqueue() -> None:
        started.wait(timeout=5)
        try:
            queued.append(
                enqueue_create(
                    container.jobs,
                    container.registrations,
                    intent_id=ready.intent_id,
                    request=ready.request,
                    frozen=ready.final,
                )
            )
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=enqueue) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    # The check and the insert are one unit of work, so the second caller sees the first's job.
    assert failures == []
    assert len(set(queued)) == 1
    assert container.jobs.count(job_type_prefix=CREATE_JOB_TYPE) == 1
    # The first attempt fails retryably, so the one job waits out its backoff...
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.RETRY_SCHEDULED
    # ...and a caller arriving now cannot create a due job that would run the CREATE early.
    assert (
        enqueue_create(
            container.jobs,
            container.registrations,
            intent_id=ready.intent_id,
            request=ready.request,
            frozen=ready.final,
        )
        == queued[0]
    )
    assert container.jobs.count(job_type_prefix=CREATE_JOB_TYPE) == 1
    assert container.runner.run_next() is None
    assert len(run.sender.calls) == 1
    assert len(store.attempts(ready.intent_id)) == 1


def test_an_unknown_intent_can_never_be_queued_until_evidence_frees_it(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = _unknown(container, store, prep, ready)
    with pytest.raises(ExecutionRefused) as refused:
        enqueue_create(
            container.jobs,
            container.registrations,
            intent_id=ready.intent_id,
            request=ready.request,
            frozen=ready.final,
        )
    assert refused.value.code == "REGISTER_INTENT_NOT_SENDABLE"
    assert container.jobs.count(job_type_prefix=CREATE_JOB_TYPE) == 0
    # A lookup never frees it (§17.2, §28): even an available lookup claiming absence is refused.
    run.lookup.is_available = True
    run.lookup.found = {"absence_proven": True, "evidence": "sanitized"}
    with pytest.raises(ExecutionRefused):
        run.service.reconcile(ready.intent_id, correlation_id=CID)
    assert container.jobs.count(job_type_prefix=CREATE_JOB_TYPE) == 0
    # Only machine proof of non-application moves the Intent to FAILED (§28.3, §10's table), and
    # only then may a job be queued again.
    with store.transaction() as unit:
        unit.resolve_unknown(
            ready.intent_id,
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            resolved_by=ResolvedBy.USER,
            evidence_kind=ResolutionEvidence.TRANSMISSION_PRECLUDED,
            sanitized_evidence={"phase": "before transmission"},
            correlation_id=CID,
            actor="operator",
        )
    job_id = enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    assert container.jobs.get(job_id).state == JobState.QUEUED


def test_a_crash_after_start_attempt_converges_on_unknown(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    _jobs(container, run)
    job_id = enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    # The worker dies mid-attempt: the job stays RUNNING and the attempt stays open.
    claimed = container.runner._claim()
    assert claimed is not None and claimed.job_id == job_id
    with store.transaction() as unit:
        unit.start_attempt(
            ready.intent_id,
            sanitized_request={"request": "sanitized"},
            sanitizer_profile_version="sanitizer-test-1",
            correlation_id=CID,
        )
    # While the job is still RUNNING the owner is not waiting on a *terminal* job, so the sweep
    # has nothing to settle: the terminal filter belongs in the query, before any bound.
    from app.jobs.models import TERMINAL_STATE_NAMES

    assert store.jobs_with_open_attempts(CREATE_JOB_TYPE, TERMINAL_STATE_NAMES) == ()
    assert container.runner.reconcile_terminal_owners() == 0
    # 15 + 16: recovery dead-letters the non-idempotent job as UNKNOWN and never re-runs CREATE.
    assert container.runner.recover_interrupted() == 1
    job = container.jobs.get(job_id)
    assert (job.state, job.last_error_code) == (JobState.DEAD, "INTERRUPTED_OUTCOME_UNKNOWN")
    attempt = store.attempts(ready.intent_id)[-1]
    assert attempt.finished and attempt.outcome is RemoteOutcome.UNKNOWN
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.UNKNOWN
    assert run.sender.calls == []


def test_a_missed_settlement_converges_on_the_next_reconcile_sweep(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    _jobs(container, run)
    job_id = enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    claimed = container.runner._claim()
    assert claimed is not None
    with store.transaction() as unit:
        unit.start_attempt(
            ready.intent_id,
            sanitized_request={"request": "sanitized"},
            sanitizer_profile_version="sanitizer-test-1",
            correlation_id=CID,
        )
    # The job reaches its terminal state while the owner hook never runs (a process that stopped
    # between the two commits): the open attempt is still discoverable from durable rows alone.
    from dataclasses import replace as dataclass_replace

    definition = container.job_registry.get(CREATE_JOB_TYPE)
    container.job_registry._definitions[CREATE_JOB_TYPE] = dataclass_replace(
        definition, on_terminal=lambda terminal: None
    )
    container.runner.recover_interrupted()
    assert store.attempts(ready.intent_id)[-1].finished is False
    # 17: the sweep finds the inconsistency and settles it, without any replay.
    container.job_registry._definitions[CREATE_JOB_TYPE] = definition
    assert container.runner.reconcile_terminal_owners() == 1
    assert store.attempts(ready.intent_id)[-1].outcome is RemoteOutcome.UNKNOWN
    assert container.jobs.get(job_id).state == JobState.DEAD
    assert run.sender.calls == []
    # Repeating the sweep finds nothing: the owner is waiting on nothing any more.
    assert container.runner.reconcile_terminal_owners() == 0
    assert len(store.attempts(ready.intent_id)) == 1
    # Settling the same terminal job twice settles nothing new and raises nothing: the hook is
    # safe to call again, which is what makes the sweep safe to repeat.
    from app.jobs.registry import TerminalJob

    terminal = TerminalJob(
        job_id=job_id,
        job_type=CREATE_JOB_TYPE,
        state=JobState.DEAD.value,
        attempt_no=1,
        correlation_id=CID,
        target_ref=target_ref(ready.intent_id),
        error_class=ErrorClass.UNKNOWN.value,
        error_code="INTERRUPTED_OUTCOME_UNKNOWN",
    )
    run.service.settle_terminal(terminal)
    assert len(store.attempts(ready.intent_id)) == 1
    assert store.attempts(ready.intent_id)[-1].outcome is RemoteOutcome.UNKNOWN


def test_a_transient_not_applied_failure_schedules_a_retry_through_the_job_policy(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.TRANSIENT,
            error_code="PROVIDER_TIMEOUT",
        ),
    )
    _jobs(container, run)
    enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    result = container.runner.run_next()
    assert result is not None
    # 9 + 21: the shared RetryPolicy schedules it, with its own deterministic backoff.
    assert result.state is JobState.RETRY_SCHEDULED
    assert result.next_attempt_at is not None
    delay = (result.next_attempt_at - container.clock.now()).total_seconds()
    assert delay == pytest.approx(CREATE_POLICY.delay_after(1).total_seconds())


def test_an_unknown_outcome_dead_letters_the_job_instead_of_retrying(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.UNKNOWN,
            product_id=None,
            error_class=ErrorClass.RATE_LIMITED,
            error_code="PROVIDER_RATE_LIMITED",
        ),
    )
    _jobs(container, run)
    enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=ready.intent_id,
        request=ready.request,
        frozen=ready.final,
    )
    result = container.runner.run_next()
    assert result is not None
    # 10: a retryable *cause* with an unproven *outcome* still ends the job; UNKNOWN stays.
    assert result.state is JobState.DEAD
    intent = store.intent(ready.intent_id)
    assert intent is not None and intent.state is IntentState.UNKNOWN
    assert len(run.sender.calls) == 1


def test_an_auth_failure_pauses_the_scope_instead_of_spinning_through_items(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
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
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    # 22: an AUTH cause pauses the marketplace/account/endpoint-group scope.
    budget = run.service.budget(MARKET, account)
    assert budget.paused_by is ScopePauseReason.AUTH and not budget.sends_allowed
    # §26 test 1: the brake is one row, keyed exactly by that scope — and by nothing wider.
    paused = store.execution_scope(MARKET, account, ENDPOINT_GROUP)
    assert (paused.state, paused.pause_reason, paused.pause_error_class) == (
        ExecutionScopeState.PAUSED,
        ScopePauseReason.AUTH,
        ErrorClass.AUTH,
    )
    assert paused.paused_at is not None and paused.resume_generation == 0
    assert paused.pause_policy_version == ExecutionPolicy().version
    assert not store.execution_scope(MARKET, account, "another_group").paused
    # 20: a further send in that scope refuses before any attempt, for a different unit too.
    other = prepare(container, sources, store, account, prep, source_product_id="5678")
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(other))
    assert refused.value.code == "REGISTER_SCOPE_PAUSED"
    assert store.attempts(other.intent_id) == ()
    assert len(run.sender.calls) == 1


def test_an_auth_pause_is_released_only_by_a_newer_authentication_proof(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.AUTH, "PROVIDER_AUTH")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    paused = run.service.budget(MARKET, account)
    assert paused.paused_by is ScopePauseReason.AUTH and not paused.sends_allowed
    second = prepare(container, sources, store, account, prep, source_product_id="5678")
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(second))
    assert refused.value.code == "REGISTER_SCOPE_PAUSED"
    # §26 test 3: an unrelated capability change is not a recovery — a reviewed contract-freshness
    # recording or a permission refresh moves the capability row and releases nothing. Nor does an
    # authentication proof *older* than the pause it would answer.
    container.clock.advance(120)
    prep.capability.updated_at = container.clock.now()
    prep.capability.freshness_recorded_at = container.clock.now()
    prep.capability.auth_verified_at = scope_of(store, account).paused_at
    assert run.service.refresh_scope(MARKET, account, correlation_id=CID).paused
    still_paused = run.service.budget(MARKET, account)
    assert still_paused.paused_by is ScopePauseReason.AUTH and not still_paused.sends_allowed
    with pytest.raises(ExecutionRefused) as again:
        run.service.run(context(second))
    assert again.value.code == "REGISTER_SCOPE_PAUSED"
    assert scope_of(store, account).resume_generation == 0
    # §26 test 4: a proof newer than the pause releases it, and REGISTER records that release in
    # its own owner — at the proof's own time, as its next generation.
    container.clock.advance(60)
    proof = container.clock.now()
    prep.capability.auth_verified_at = proof
    assert not run.service.refresh_scope(MARKET, account, correlation_id=CID).paused
    resumed = run.service.budget(MARKET, account)
    assert resumed.paused_by is None and resumed.sends_allowed
    assert (resumed.reset_at, resumed.resume_generation) == (proof, 1)
    row = scope_of(store, account)
    assert (row.state, row.resumed_at, row.resumed_by, row.resume_reason) == (
        ExecutionScopeState.ACTIVE,
        proof,
        "system",
        "FRESH_AUTH_PROOF",
    )
    # §26 test 11: the failed attempt is untouched — a release moves a window, it never rewrites
    # or deletes one recorded attempt.
    attempts = store.attempts(ready.intent_id)
    assert len(attempts) == 1
    assert (attempts[0].outcome, attempts[0].error_class) == (
        RemoteOutcome.NOT_APPLIED_PROVEN,
        ErrorClass.AUTH,
    )
    # And a send in the scope is possible again: the gate is reached, not the pause.
    run.sender.outcome = RemoteOutcome.APPLIED_PROVEN
    run.sender.product_id = PRODUCT_NO
    run.sender.error_class = None
    assert run.service.run(context(second)).intent_state is IntentState.CONFIRMED


def test_an_auth_pause_is_not_an_operators_to_release(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # M5-26 / B1: an AUTH pause ends when the account authenticates again. An operator action is
    # not that proof, so the explicit resume refuses it and the brake does not move at all.
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.AUTH, "PROVIDER_AUTH")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    paused = scope_of(store, account)
    assert paused.pause_reason is ScopePauseReason.AUTH
    with pytest.raises(AppError) as refused:
        run.service.resume_scope(
            MARKET, account, actor=OPERATOR, reason="OPERATOR-DECIDED", correlation_id=CID
        )
    assert refused.value.code == "REGISTER_SCOPE_RESUME_NOT_PERMITTED"
    # The owner refuses it for every caller, not only through this service.
    with pytest.raises(AppError) as direct, store.transaction() as unit:
        unit.resume_scope(
            MARKET,
            account,
            ENDPOINT_GROUP,
            actor=OPERATOR,
            reason="OPERATOR-DECIDED",
            correlation_id=CID,
            allowed_reasons=OPERATOR_RESUMABLE,
        )
    assert direct.value.code == "REGISTER_SCOPE_RESUME_NOT_PERMITTED"
    assert scope_of(store, account) == paused  # nothing moved: state, boundary, generation
    # A stale proof, and a proof exactly as old as the pause, leave it paused.
    for proof in (paused.paused_at - timedelta(seconds=1), paused.paused_at):
        prep.capability.auth_verified_at = proof
        assert run.service.refresh_scope(MARKET, account, correlation_id=CID).paused
        assert scope_of(store, account).resume_generation == 0
    # Only a strictly newer authentication proof releases it.
    container.clock.advance(60)
    prep.capability.auth_verified_at = container.clock.now()
    released = run.service.refresh_scope(MARKET, account, correlation_id=CID)
    assert (released.state, released.resume_generation) == (ExecutionScopeState.ACTIVE, 1)
    assert released.resumed_by == "system" and released.resume_reason == "FRESH_AUTH_PROOF"


def test_a_spent_budget_becomes_a_durable_brake_before_the_send_is_refused(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # §26 / B2: the budget is counted under the *current* policy, so a lowered threshold, a policy
    # revision or a restart can exhaust a scope that was never paused. The evaluation that refuses
    # the send records what it found, so the scope an operator must resume actually exists.
    ready = prepare(container, sources, store, account, prep)
    lenient = failing(
        container,
        prep,
        ErrorClass.TRANSIENT,
        "PROVIDER_TIMEOUT",
        policy=ExecutionPolicy(max_proven_failures=3),
    )
    for _ in range(2):
        with pytest.raises(AttemptFailed):
            lenient.service.run(context(ready))
    assert lenient.service.budget(MARKET, account).sends_allowed
    assert not scope_of(store, account).paused  # still ACTIVE: two of three spent
    before = store.attempts(ready.intent_id)
    # A policy revision and a restart: the same history, a lower threshold.
    strict = failing(
        container,
        prep,
        ErrorClass.TRANSIENT,
        "PROVIDER_TIMEOUT",
        policy=ExecutionPolicy(max_proven_failures=2),
    )
    second = prepare(container, sources, store, account, prep, source_product_id="5678")
    with pytest.raises(ExecutionRefused) as refused:
        strict.service.run(context(second))
    assert refused.value.code == "REGISTER_FAILURE_BUDGET_EXHAUSTED"
    paused = scope_of(store, account)
    assert (paused.state, paused.pause_reason) == (
        ExecutionScopeState.PAUSED,
        ScopePauseReason.FAILURE_BUDGET,
    )
    # No single provider verdict caused it, so none is recorded; the policy that judged it is.
    assert paused.pause_error_class is None
    assert paused.pause_policy_version == ExecutionPolicy().version
    assert store.attempts(second.intent_id) == ()  # refused before any attempt
    # And the operator can now resume it — which is what the missing row made impossible.
    container.clock.advance(60)
    released = strict.service.resume_scope(
        MARKET, account, actor=OPERATOR, reason="INVESTIGATED", correlation_id=CID
    )
    assert (released.state, released.resume_generation) == (ExecutionScopeState.ACTIVE, 1)
    assert strict.service.budget(MARKET, account).sends_allowed
    # §26 test 11: the recorded attempts are exactly what they were.
    assert store.attempts(ready.intent_id) == before


def test_a_recorded_brake_keeps_the_cause_that_engaged_it(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # §26: a scope already stopped by AUTH is not re-labelled `FAILURE_BUDGET` because its
    # history is also spent. The recorded cause is what the operator and the automatic release
    # both answer, so a later evaluation never overwrites it.
    ready = prepare(container, sources, store, account, prep)
    run = failing(
        container,
        prep,
        ErrorClass.AUTH,
        "PROVIDER_AUTH",
        policy=ExecutionPolicy(max_proven_failures=1),
    )
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    paused = scope_of(store, account)
    assert (paused.pause_reason, paused.pause_error_class) == (
        ScopePauseReason.AUTH,
        ErrorClass.AUTH,
    )
    assert run.service.budget(MARKET, account).exhausted  # the history is spent as well
    second = prepare(container, sources, store, account, prep, source_product_id="5678")
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(second))
    assert refused.value.code == "REGISTER_SCOPE_PAUSED"
    assert scope_of(store, account) == paused  # cause, time, policy version and generation


def test_a_brake_never_records_a_class_that_did_not_cause_it(
    container: Container,
    config: AppConfig,
    store: RegistrationStore,
    account: str,
) -> None:
    # §26 hardening: a reason and its measured class belong together. The owner refuses the pair,
    # and the schema refuses it again, so no write path can store `AUTH` + `POLICY_BLOCKED`.
    for reason, cause in (
        (ScopePauseReason.AUTH, ErrorClass.POLICY_BLOCKED),
        (ScopePauseReason.AUTH, None),
        (ScopePauseReason.POLICY, ErrorClass.AUTH),
        (ScopePauseReason.FAILURE_BUDGET, ErrorClass.AUTH),
        (ScopePauseReason.FAILURE_BUDGET, ErrorClass.POLICY_BLOCKED),
    ):
        with pytest.raises(AppError) as refused, store.transaction() as unit:
            unit.pause_scope(
                MARKET,
                account,
                ENDPOINT_GROUP,
                reason=reason,
                policy_version=ExecutionPolicy().version,
                error_class=cause,
                actor=OPERATOR,
                correlation_id=CID,
            )
        assert refused.value.code == "REGISTER_SCOPE_CAUSE_MISMATCH"
    assert not scope_of(store, account).paused
    with (
        contextlib.closing(raw(config)) as connection,
        pytest.raises(sqlite3.IntegrityError, match="pause_class_is_its_cause"),
    ):
        connection.execute(
            "INSERT INTO registration_execution_scopes (marketplace_key,"
            " marketplace_account_id, endpoint_group, state, pause_reason, pause_error_class,"
            " paused_at, pause_policy_version, resume_generation, created_at, updated_at)"
            " VALUES (?, ?, ?, 'PAUSED', 'AUTH', 'POLICY_BLOCKED', ?, 'p/v1', 0, ?, ?)",
            (MARKET, account, ENDPOINT_GROUP, AT, AT, AT),
        )
        connection.commit()


def test_a_policy_pause_is_never_released_by_authentication(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.POLICY_BLOCKED, "PROVIDER_POLICY")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    paused = scope_of(store, account)
    assert (paused.state, paused.pause_reason) == (
        ExecutionScopeState.PAUSED,
        ScopePauseReason.POLICY,
    )
    second = prepare(container, sources, store, account, prep, source_product_id="5678")
    # §26 test 5: re-authenticating proves nothing about a marketplace policy refusal.
    container.clock.advance(600)
    prep.capability.auth_verified_at = container.clock.now()
    prep.capability.updated_at = container.clock.now()
    still = run.service.budget(MARKET, account)
    assert still.paused_by is ScopePauseReason.POLICY and not still.sends_allowed
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(second))
    assert refused.value.code == "REGISTER_SCOPE_PAUSED"
    assert scope_of(store, account).resume_generation == 0
    # The automatic path refuses it at the owner too, not only by this service's own check.
    with pytest.raises(AppError) as mismatch, store.transaction() as unit:
        unit.resume_scope(
            MARKET,
            account,
            ENDPOINT_GROUP,
            actor="system",
            reason="FRESH_AUTH_PROOF",
            correlation_id=CID,
            allowed_reasons=AUTH_RESUMABLE,
        )
    assert mismatch.value.code == "REGISTER_SCOPE_RESUME_NOT_PERMITTED"
    # §26 test 6: only an explicit audited REGISTER resume releases it, and the next send runs the
    # whole gate again — the resume claims nothing about the provider.
    released = run.service.resume_scope(
        MARKET, account, actor=OPERATOR, reason="POLICY-REVIEWED", correlation_id=CID
    )
    assert (released.state, released.resume_generation) == (ExecutionScopeState.ACTIVE, 1)
    assert run.service.budget(MARKET, account).sends_allowed
    run.sender.outcome = RemoteOutcome.APPLIED_PROVEN
    run.sender.product_id = PRODUCT_NO
    run.sender.error_class = None
    assert run.service.run(context(second)).intent_state is IntentState.CONFIRMED
    assert len(store.attempts(ready.intent_id)) == 1


def test_a_budget_breach_is_released_only_by_an_explicit_resume(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    run = failing(
        container,
        prep,
        ErrorClass.TRANSIENT,
        "PROVIDER_TIMEOUT",
        policy=ExecutionPolicy(max_proven_failures=2),
    )
    ready = prepare(container, sources, store, account, prep)
    for _ in range(2):
        with pytest.raises(AttemptFailed):
            run.service.run(context(ready))
    breached = run.service.budget(MARKET, account)
    assert breached.exhausted and breached.paused_by is ScopePauseReason.FAILURE_BUDGET
    assert scope_of(store, account).pause_error_class is ErrorClass.TRANSIENT
    # §26 test 7: an ordinary authentication refresh never clears a spent budget.
    container.clock.advance(300)
    prep.capability.updated_at = container.clock.now()
    prep.capability.auth_verified_at = container.clock.now()
    assert not run.service.budget(MARKET, account).sends_allowed
    assert scope_of(store, account).resume_generation == 0
    # §26 test 8: the explicit resume releases it and resets only this scope's counting window.
    released = run.service.resume_scope(
        MARKET, account, actor=OPERATOR, reason="INVESTIGATED", correlation_id=CID
    )
    after = run.service.budget(MARKET, account)
    assert (after.sends_allowed, after.consecutive_failures, after.exhausted) == (True, 0, False)
    assert after.reset_at == released.resumed_at
    # §26 test 11: both attempts are still there, unchanged — only the window moved.
    attempts = store.attempts(ready.intent_id)
    assert [(a.attempt_no, a.outcome, a.error_class) for a in attempts] == [
        (1, RemoteOutcome.NOT_APPLIED_PROVEN, ErrorClass.TRANSIENT),
        (2, RemoteOutcome.NOT_APPLIED_PROVEN, ErrorClass.TRANSIENT),
    ]
    # §26 test 14: a re-failure after the resume pauses the scope again, at a later boundary.
    container.clock.advance(60)
    third = prepare(container, sources, store, account, prep, source_product_id="5678")
    for _ in range(2):
        with pytest.raises(AttemptFailed):
            run.service.run(context(third))
    again = scope_of(store, account)
    assert (again.state, again.pause_reason) == (
        ExecutionScopeState.PAUSED,
        ScopePauseReason.FAILURE_BUDGET,
    )
    assert again.paused_at is not None and again.paused_at > released.resumed_at
    assert again.resume_generation == 1
    # §26 test 13: a second resume of a scope that is no longer paused is refused by contract.
    run.service.resume_scope(
        MARKET, account, actor=OPERATOR, reason="INVESTIGATED", correlation_id=CID
    )
    with pytest.raises(AppError) as repeated:
        run.service.resume_scope(
            MARKET, account, actor=OPERATOR, reason="INVESTIGATED", correlation_id=CID
        )
    assert repeated.value.code == "REGISTER_SCOPE_NOT_PAUSED"
    assert scope_of(store, account).resume_generation == 2


def test_a_resume_releases_that_scope_and_no_other(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.POLICY_BLOCKED, "PROVIDER_POLICY")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    # The same account, a second endpoint group, and a second canonical account: each is its own
    # brake in the owner. Their execution owner is the PR that adopts them — this one sends only
    # CREATE — so they are engaged here through the scope owner itself.
    _pause(store, account, OTHER_GROUP)
    other_account = establish(container, config, MARKET, "uid-market-a-2")
    _pause(store, other_account, ENDPOINT_GROUP)
    assert store.execution_scope(MARKET, account, OTHER_GROUP).paused
    assert store.execution_scope(MARKET, other_account, ENDPOINT_GROUP).paused
    # §26 tests 9 and 10: releasing one scope releases exactly that scope.
    run.service.resume_scope(
        MARKET, account, actor=OPERATOR, reason="POLICY-REVIEWED", correlation_id=CID
    )
    assert not scope_of(store, account).paused
    assert store.execution_scope(MARKET, account, OTHER_GROUP).paused
    assert store.execution_scope(MARKET, other_account, ENDPOINT_GROUP).paused
    # And a release in one group never moves another group's boundary.
    with store.transaction() as unit:
        unit.resume_scope(
            MARKET,
            account,
            OTHER_GROUP,
            actor=OPERATOR,
            reason="POLICY-REVIEWED",
            correlation_id=CID,
            allowed_reasons=OPERATOR_RESUMABLE,
        )
    assert store.execution_scope(MARKET, account, OTHER_GROUP).resume_generation == 1
    assert store.execution_scope(MARKET, other_account, ENDPOINT_GROUP).resume_generation == 0
    assert scope_of(store, account).resume_generation == 1


def test_this_owner_executes_the_create_endpoint_group_only(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # §26 / B3: the budget of a scope is counted from that scope's own attempt history. M5 sends
    # one operation, CREATE, so this owner refuses any other endpoint group outright rather than
    # counting a history that is not its own.
    with pytest.raises(AppError) as refused:
        ExecutionPolicy(endpoint_group=OTHER_GROUP)
    assert refused.value.code == "REGISTER_ENDPOINT_GROUP_UNSUPPORTED"
    # The history it counts is exactly the CREATE attempts of that marketplace and account, and
    # CREATE is the whole operation vocabulary M5 has — the schema refuses any other, so no other
    # operation's attempts exist to mix in. The store filters on it all the same, so the claim
    # stays true of the code and not only of today's vocabulary.
    assert set(Operation) == {Operation.CREATE}
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.TRANSIENT, "PROVIDER_TIMEOUT")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    counted = store.scope_attempts(MARKET, account)
    assert [a.attempt_no for a in counted] == [1]
    assert store.scope_attempts(MARKET, account, operation=Operation.CREATE) == counted


@pytest.mark.parametrize(
    "cause", [ErrorClass.UNKNOWN, ErrorClass.TRANSIENT, ErrorClass.AUTH, ErrorClass.POLICY_BLOCKED]
)
def test_an_unknown_outcome_never_engages_the_brake(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
    cause: ErrorClass,
) -> None:
    # §26 test 15: an unproven outcome is reconciled, never budgeted. Its Intent already blocks
    # its own conflict scope, and the scope brake stays exactly where it was — whatever the cause
    # was, because the cause alone never proves that the mutation did not happen.
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.UNKNOWN,
            product_id=None,
            error_class=cause,
            error_code="PROVIDER_AMBIGUOUS",
        ),
        policy=ExecutionPolicy(max_proven_failures=1),
    )
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    scope = scope_of(store, account)
    assert (scope.state, scope.pause_reason, scope.paused_at) == (
        ExecutionScopeState.ACTIVE,
        None,
        None,
    )
    budget = run.service.budget(MARKET, account)
    assert budget.sends_allowed and budget.consecutive_failures == 0


def test_a_release_older_than_the_pause_it_answers_is_refused(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    # §26: `resumed_at` is the accepted release time, so it can never predate the brake it lifts —
    # otherwise a stale proof would open a window that ends before the failures it forgives.
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.AUTH, "PROVIDER_AUTH")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    paused_at = scope_of(store, account).paused_at
    assert paused_at is not None
    with pytest.raises(AppError) as refused, store.transaction() as unit:
        unit.resume_scope(
            MARKET,
            account,
            ENDPOINT_GROUP,
            actor="system",
            reason="FRESH_AUTH_PROOF",
            correlation_id=CID,
            allowed_reasons=AUTH_RESUMABLE,
            at=paused_at - timedelta(seconds=1),
        )
    assert refused.value.code == "REGISTER_SCOPE_RELEASE_NOT_NEWER"
    assert scope_of(store, account).paused


def test_every_brake_transition_is_audited_with_safe_fields_only(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.POLICY_BLOCKED, "PROVIDER_POLICY")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    run.service.resume_scope(
        MARKET, account, actor=OPERATOR, reason="POLICY-REVIEWED", correlation_id=CID
    )
    rows = _scope_audit(config)
    assert [kind for kind, _target, _details in rows] == [
        "REGISTRATION_EXECUTION_SCOPE_PAUSED",
        "REGISTRATION_EXECUTION_SCOPE_RESUMED",
    ]
    paused, resumed = (json.loads(details) for _k, _t, details in rows)
    assert paused["pause_reason"] == "POLICY" and paused["state"] == "PAUSED"
    assert paused["endpoint_group"] == ENDPOINT_GROUP and paused["resume_generation"] == 0
    assert resumed["state"] == "ACTIVE" and resumed["resume_generation"] == 1
    assert resumed["resume_reason"] == "POLICY-REVIEWED" and resumed["pause_reason"] is None
    # §26: identities, enums, versions and the generation — nothing else, and no provider identity.
    text = " ".join(details for _k, _t, details in rows)
    assert "uid-market-a-1" not in text and PRODUCT_NO not in text
    assert set(paused) == {
        "marketplace_key",
        "marketplace_account_id",
        "endpoint_group",
        "state",
        "pause_reason",
        "pause_error_class",
        "pause_policy_version",
        "resume_generation",
        "resume_reason",
    }


@pytest.mark.parametrize(
    "actor,reason",
    [
        ("operator-1", "https://provider.example/why"),
        ("operator-1", "bearer abcdefgh12345678"),
        ("", "REVIEWED"),
        ("operator-1", "reviewed: the seller called about it"),
    ],
)
def test_a_brake_transition_records_no_prose_url_or_secret(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
    actor: str,
    reason: str,
) -> None:
    # §26: `resumed_by` and `resume_reason` are plain labels. A URL, a secret-shaped value, an
    # empty actor or operator prose never reaches the durable row (§15).
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.POLICY_BLOCKED, "PROVIDER_POLICY")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    with pytest.raises(AppError) as refused:
        run.service.resume_scope(MARKET, account, actor=actor, reason=reason, correlation_id=CID)
    assert refused.value.code == "REGISTER_SCOPE_LABEL_UNSAFE"
    assert scope_of(store, account).paused


def _scope_audit(config: AppConfig) -> list[tuple[str, str, str]]:
    with contextlib.closing(raw(config)) as connection:
        return [
            (kind, target, details)
            for kind, target, details in connection.execute(
                "SELECT event_type, target_ref, details_json FROM audit_events"
                " WHERE event_type LIKE 'REGISTRATION_EXECUTION_SCOPE_%' ORDER BY seq"
            ).fetchall()
        ]


def test_a_paused_scope_survives_a_restart_with_its_boundary(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = failing(container, prep, ErrorClass.POLICY_BLOCKED, "PROVIDER_POLICY")
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    paused = scope_of(store, account)
    # §26 test 12: a fresh owner over the same database — what a restart leaves — reads the same
    # brake and the same boundary. Nothing about it lived in the process.
    restarted = execution(container, prep).service
    assert restarted.scope(MARKET, account) == paused
    assert not restarted.budget(MARKET, account).sends_allowed
    restarted.resume_scope(
        MARKET, account, actor=OPERATOR, reason="POLICY-REVIEWED", correlation_id=CID
    )
    reread = execution(container, prep).service.scope(MARKET, account)
    assert (reread.state, reread.resume_generation) == (ExecutionScopeState.ACTIVE, 1)
    assert reread.resumed_at is not None


def test_a_budget_of_one_account_never_stops_another(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.AUTH,
            error_code="PROVIDER_AUTH",
        ),
        policy=ExecutionPolicy(max_proven_failures=1),
    )
    with pytest.raises(AttemptFailed):
        run.service.run(context(ready))
    # The budget is scoped to marketplace x canonical account x endpoint group: another
    # account's history is not this one's, in either direction.
    other_account = establish(container, config, MARKET, "uid-market-a-2")
    assert not run.service.budget(MARKET, account).sends_allowed
    assert run.service.budget(MARKET, other_account).sends_allowed


def test_a_failure_budget_breach_stops_further_sends_in_the_scope(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    run = execution(
        container,
        prep,
        sender=FakeSender(
            outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
            product_id=None,
            error_class=ErrorClass.TRANSIENT,
            error_code="PROVIDER_TIMEOUT",
        ),
        policy=ExecutionPolicy(max_proven_failures=2),
    )
    first = prepare(container, sources, store, account, prep)
    for _ in range(2):
        with pytest.raises(AttemptFailed):
            run.service.run(context(first))
    # 20: two proven failures exhaust this scope's budget, derived from the attempts themselves.
    budget = run.service.budget(MARKET, account)
    assert (budget.exhausted, budget.consecutive_failures) == (True, 2)
    second = prepare(container, sources, store, account, prep, source_product_id="5678")
    with pytest.raises(ExecutionRefused) as refused:
        run.service.run(context(second))
    assert refused.value.code == "REGISTER_FAILURE_BUDGET_EXHAUSTED"
    assert store.attempts(second.intent_id) == ()


def test_no_durable_hash_carries_forbidden_material(
    container: Container,
    config: AppConfig,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    import contextlib as _contextlib
    import json

    from tests.product_support import raw

    ready = prepare(container, sources, store, account, prep)
    run = execution(container, prep)
    run.service.run(context(ready))
    # 19: the durable job payload and every registration row hold only sanitized values.
    with _contextlib.closing(raw(config)) as connection:
        payloads = [row[0] for row in connection.execute("SELECT payload_json FROM jobs")]
        attempts = list(
            connection.execute(
                "SELECT request_payload_hash, response_digest FROM registration_attempts"
            )
        )
    text = json.dumps(payloads) + json.dumps([list(row) for row in attempts])
    for forbidden in ("Bearer", "authorization", "cookie", "access_token", "?sig=", "secret"):
        assert forbidden.lower() not in text.lower()
    assert all(len(row[0]) == 64 for row in attempts)


def test_the_production_wiring_cannot_reach_a_marketplace_mutation(
    container: Container,
) -> None:
    # 4 + 25: the container's own CREATE seam is the SmartStore one, and it is unavailable; the
    # reconcile lookup likewise. Neither can be made to send by any caller.
    sender = SmartStoreCreateSender()
    assert not sender.available()
    with pytest.raises(CreateNotAdoptedError):
        sender.send(payload={}, idempotency_key="k", listing_identity="icbm-x")
    lookup = SmartStoreReconcileLookup()
    assert not lookup.available()
    with pytest.raises(ReconcileLookupNotAdoptedError):
        lookup.find(marketplace_account_id="mpa-1", listing_identity="icbm-x")
    assert CREATE_JOB_TYPE in container.job_registry.job_types()
    definition = container.job_registry.get(CREATE_JOB_TYPE)
    # 15: the CREATE job is non-idempotent, so recovery never replays a possible handoff.
    assert definition.idempotent is False
    assert definition.on_terminal is not None and definition.unsettled_owned_jobs is not None


def test_an_unknown_blocks_a_new_overlapping_create_intent_but_not_another_group(
    container: Container,
    sources: Collections,
    store: RegistrationStore,
    account: str,
    prep: Preparation,
) -> None:
    ready = prepare(container, sources, store, account, prep)
    _unknown(container, store, prep, ready)
    # 11: a new Snapshot of the same group cannot escape the unresolved UNKNOWN (R2), so the
    # preflight of that group is BLOCKED and no second Intent can even be prepared.
    item = ready_item(container, sources, "1234")
    blocked_draft = draft(store, account, [item])
    blocked = prep.service.candidate(request(store, blocked_draft, account, [item]))
    assert blocked.status is ReadinessStatus.BLOCKED
    assert "UNRESOLVED_CREATE_CONFLICT" in set(blocked.codes)
    # 12: a different product group in the same marketplace and account is unaffected.
    other = prepare(container, sources, store, account, prep, source_product_id="5678")
    intent = store.intent(other.intent_id)
    assert intent is not None and intent.state is IntentState.PREPARED
