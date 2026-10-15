"""ADR-0042 S3b at send time: the store snapshot's freshness is checked again before a CREATE.

ADR-0042 §5.2 stays strict — an ICBM CREATE of the store makes the snapshot stale — and a bulk run
must not fail for it. Proven here on the real job runner, bulk owner and registration store, with
fake provider seams and a fake store catalog (no provider is reached):

- a bulk run of three units: the first CREATE is sent; each later one finds the snapshot stale
  only because the previous CREATE started after it, waits (a TRANSIENT refusal the job's retry
  schedule repeats — no attempt, nothing sent) while the automatic re-sync runs, then proceeds on
  the fresh snapshot with the frozen fingerprint; every unit is sent exactly once;
- a wait past the cap after that mutation, a snapshot stale by age, or a missing one is refused
  as it is: the job ends, nothing is sent, and the Intent stays sendable;
- a store listing that appeared since the freeze changes the evidence, so the gate refuses it.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import pytest

from app.capabilities.jobs.models import JobState
from app.container import Container
from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.operate.catalog.coverage import CatalogCoverage
from app.stages.operate.catalog.duplicates import CatalogDuplicateSource
from app.stages.operate.catalog.service import CONNECT, StoreCatalogService
from app.stages.products.model import ReadinessStatus
from app.stages.register.builder import RegistrationSnapshotBuilder
from app.stages.register.bulk import BulkCreatePlan
from app.stages.register.execution import (
    CREATE_JOB_TYPE,
    CREATE_POLICY,
    DUPLICATE_RESYNC_WAIT,
    DUPLICATE_REVALIDATION_NOT_WIRED,
    RegistrationExecutionService,
    create_job_definition,
    encode_send_request,
    enqueue_create,
)
from app.stages.register.model import IntentState
from app.stages.register.preparation import DUPLICATE_EVIDENCE_STALE, PreflightResult
from app.stages.register.provider import CreateHandoff, DuplicateCheck, duplicate_unit
from app.stages.register.store import RegistrationStore
from integrations.marketplaces.smartstore.product import seller_management_code
from tests.integration.register.test_m5_register_execution import (  # noqa: F401 - fixtures
    FakeComparator,
    FakeLookup,
    FakeProjector,
    FakeReadback,
    Prepared,
    account,
    prep,
    sources,
    store,
)
from tests.integration.register.test_s3b_catalog_duplicate_source import FakeCatalogSource
from tests.support.live_safety_support import AdmittingAuthority
from tests.support.product_support import Collections
from tests.support.register_support import (
    CID,
    MARKET,
    OPERATOR,
    Preparation,
    draft,
    prepared,
    ready_item,
    request,
)

pytestmark = pytest.mark.integration


@dataclass
class CountingSender:
    """An applied CREATE per call, each with its own provider product number."""

    calls: list[str] = field(default_factory=list)

    def available(self) -> bool:
        return True

    def send(self, *, payload: Any, idempotency_key: str, listing_identity: str) -> CreateHandoff:
        self.calls.append(listing_identity)
        return CreateHandoff(
            remote_outcome=RemoteOutcome.APPLIED_PROVEN,
            sanitized_request={"listing_identity": listing_identity},
            marketplace_product_id=str(9000 + len(self.calls)),
            response_status=200,
            sanitized_response={"accepted": True},
        )


def _service(
    container: Container,
    prep: Preparation,  # noqa: F811
    sender: CountingSender,
    check: Callable[[PreflightResult], DuplicateCheck | None],
) -> RegistrationExecutionService:
    service = RegistrationExecutionService(
        registrations=container.registrations,
        preflight=prep.service,
        sender=sender,
        readback=FakeReadback(retained={"originProduct": {"name": "x"}}),
        lookup=FakeLookup(),
        capability=prep.capability,
        compare=FakeComparator(),
        projection=FakeProjector(),
        clock=container.clock,
        authority=AdmittingAuthority(),
        duplicate_check=check,
    )
    container.job_registry._definitions[CREATE_JOB_TYPE] = create_job_definition(
        service,
        retry_policy=CREATE_POLICY,
        after_terminal=container.bulk_registration.settle_terminal,
        additional_unsettled=container.bulk_registration.unsettled_jobs,
    )
    return service


def _catalog(container: Container, account: str, source: FakeCatalogSource) -> Any:  # noqa: F811
    catalog = StoreCatalogService(
        db=container.db,
        clock=container.clock,
        source=source,
        account_of=lambda key: account if key == MARKET else None,
        interval_s=1800,
        pause_s=0.0,
    )
    resyncs: list[int] = []
    coverage = CatalogCoverage(catalog=catalog, audit=container.audit)
    duplicates = CatalogDuplicateSource(
        marketplace_key=MARKET,
        contract_version="catalog-duplicate-test/v1",
        catalog=catalog,
        seller_code=seller_management_code,
        convention_codes=lambda items: (),
        mutation_started_at=container.registrations.last_mutation_started,
        refresh=lambda: resyncs.append(1) is None,
        coverage=coverage,
    )
    return _Confirming(catalog, coverage), duplicates, resyncs


class _Confirming:
    """The catalog owner, with the seller confirming the coverage after each COMPLETE sync."""

    def __init__(self, catalog: StoreCatalogService, coverage: CatalogCoverage) -> None:
        self._catalog = catalog
        self._coverage = coverage

    def sync(self, **kwargs: Any) -> Any:
        run = self._catalog.sync(**kwargs)
        if run.outcome == "COMPLETE" and self._coverage.view().state != "CONFIRMED":
            self._coverage.confirm(run_id=run.run_id, actor=OPERATOR, correlation_id=CID)
        return run


def _frozen(
    container: Container,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
    duplicates: CatalogDuplicateSource,
    source_product_id: str,
) -> Prepared:
    """One unit frozen with the store snapshot's own evidence."""
    item = ready_item(container, sources, source_product_id)
    draft_id = draft(store, account, [item])
    req = request(store, draft_id, account, [item])
    check = duplicates.check(duplicate_unit(prep.service.candidate(req)))
    assert check.evidence is not None, check
    req = replace(req, duplicate_evidence=check.evidence)
    candidate = prep.service.candidate(req)
    assert candidate.status is ReadinessStatus.READY, candidate.reasons
    final = prep.service.final(req, prepared(candidate))
    assert final.status is ReadinessStatus.READY, final.reasons
    snapshot = RegistrationSnapshotBuilder(preflight=prep.service, registrations=store).freeze(
        final, created_by=OPERATOR, correlation_id=CID
    )
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


def _plan(unit: Prepared) -> BulkCreatePlan:
    return BulkCreatePlan(
        unit.intent_id,
        encode_send_request(
            unit.intent_id,
            unit.request,
            unit.assets,
            listing_identity=unit.final.resolved.listing_identity,
            identity_generation=unit.final.resolved.identity_generation,
        ),
    )


def test_a_bulk_run_waits_for_each_resync_and_sends_every_unit_once(
    container: Container,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    source = FakeCatalogSource()
    catalog, duplicates, resyncs = _catalog(container, account, source)
    assert catalog.sync(trigger=CONNECT, correlation_id=CID).outcome == "COMPLETE"
    units = [
        _frozen(container, sources, store, account, prep, duplicates, spid)
        for spid in ("s3b-bulk-1", "s3b-bulk-2", "s3b-bulk-3")
    ]
    sender = CountingSender()
    _service(
        container,
        prep,
        sender,
        lambda candidate: duplicates.check(duplicate_unit(candidate)),
    )
    run = container.bulk_registration.start([_plan(u) for u in units], correlation_id=CID)
    assert run.state == "RUNNING"

    # The first CREATE: the snapshot is fresh, and it is sent.
    first = container.runner.run_next()
    assert first is not None and first.state is JobState.SUCCEEDED, first
    assert sender.calls == [units[0].final.resolved.listing_identity]
    for position, unit in enumerate(units[1:], start=1):
        # The previous CREATE started after the sync: stale by ICBM's own change. The next unit
        # waits — nothing is sent, no attempt opens — and the re-sync is asked for.
        asked = len(resyncs)
        waited = container.runner.run_next()
        assert waited is not None and waited.state is JobState.RETRY_SCHEDULED, waited
        assert waited.error_code == DUPLICATE_RESYNC_WAIT
        assert len(sender.calls) == position
        assert store.attempts(unit.intent_id) == ()
        assert len(resyncs) > asked
        # The re-sync runs: the store now lists the previous unit's new listing (its own code).
        previous = units[position - 1].final.resolved.listing_identity
        source.put(f"900{position}", f"listed product {position}", seller_management_code(previous))
        container.clock.advance(5)
        assert catalog.sync(trigger="PRE_REGISTRATION", correlation_id=CID).outcome == "COMPLETE"
        # Its retry is due: the CREATE proceeds on the fresh snapshot, with the frozen fingerprint.
        container.clock.advance(CREATE_POLICY.delay_after(1).total_seconds())
        sent = container.runner.run_next()
        assert sent is not None and sent.state is JobState.SUCCEEDED, sent
        assert sender.calls[-1] == unit.final.resolved.listing_identity
    assert sender.calls == [u.final.resolved.listing_identity for u in units]
    assert all(len(store.attempts(u.intent_id)) == 1 for u in units)
    done = container.bulk_registration.status(run.bulk_run_id)
    assert (done.state, done.succeeded, done.failed) == ("COMPLETED", 3, 0)


@pytest.mark.parametrize(
    ("check", "code"),
    [
        # Stale because of ICBM's own change, but longer ago than the cap: refused as it is.
        (
            lambda now: DuplicateCheck(
                None, DUPLICATE_EVIDENCE_STALE, stale_since_mutation=now - timedelta(minutes=11)
            ),
            "REGISTER_SEND_DUPLICATE_EVIDENCE_STALE",
        ),
        # Stale by age: never a wait.
        (
            lambda now: DuplicateCheck(None, DUPLICATE_EVIDENCE_STALE, resyncing=True),
            "REGISTER_SEND_DUPLICATE_EVIDENCE_STALE",
        ),
        (
            lambda now: DuplicateCheck(None, "DUPLICATE_EVIDENCE_UNAVAILABLE"),
            "REGISTER_SEND_DUPLICATE_EVIDENCE_UNAVAILABLE",
        ),
    ],
)
def test_without_current_evidence_nothing_is_sent(
    container: Container,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
    check: Callable[[Any], DuplicateCheck],
    code: str,
) -> None:
    source = FakeCatalogSource()
    catalog, duplicates, _ = _catalog(container, account, source)
    catalog.sync(trigger=CONNECT, correlation_id=CID)
    unit = _frozen(container, sources, store, account, prep, duplicates, "s3b-refused")
    sender = CountingSender()
    _service(container, prep, sender, lambda candidate: check(container.clock.now()))
    enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=unit.intent_id,
        request=unit.request,
        frozen=unit.final,
    )
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.DEAD, result
    assert result.error_code == code
    assert sender.calls == [] and store.attempts(unit.intent_id) == ()
    intent = store.intent(unit.intent_id)
    assert intent is not None and intent.state is IntentState.PREPARED


def test_a_listing_that_appeared_since_the_freeze_is_never_sent_over(
    container: Container,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    source = FakeCatalogSource()
    catalog, duplicates, _ = _catalog(container, account, source)
    catalog.sync(trigger=CONNECT, correlation_id=CID)
    unit = _frozen(container, sources, store, account, prep, duplicates, "s3b-appeared")
    # Someone listed it by hand in the seller center; the next sync sees it.
    code = seller_management_code(unit.final.resolved.listing_identity)
    source.put("8001", "listed by hand", code)
    container.clock.advance(5)
    catalog.sync(trigger="AUTO", correlation_id=CID)
    sender = CountingSender()
    _service(container, prep, sender, lambda candidate: duplicates.check(duplicate_unit(candidate)))
    enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=unit.intent_id,
        request=unit.request,
        frozen=unit.final,
    )
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.DEAD, result
    assert result.error_code == "REGISTER_SEND_PREFLIGHT_NOT_READY"
    assert sender.calls == []


def test_a_create_without_send_time_revalidation_is_never_sent(
    container: Container,
    sources: Collections,  # noqa: F811
    store: RegistrationStore,  # noqa: F811
    account: str,  # noqa: F811
    prep: Preparation,  # noqa: F811
) -> None:
    """ADR-0042 §6: the send-time duplicate revalidation is mandatory. A service built without it
    refuses every CREATE before any attempt opens; nothing is sent."""
    source = FakeCatalogSource()
    catalog, duplicates, _ = _catalog(container, account, source)
    catalog.sync(trigger=CONNECT, correlation_id=CID)
    unit = _frozen(container, sources, store, account, prep, duplicates, "s3b-unwired")
    sender = CountingSender()
    service = RegistrationExecutionService(
        registrations=container.registrations,
        preflight=prep.service,
        sender=sender,
        readback=FakeReadback(retained={"originProduct": {"name": "x"}}),
        lookup=FakeLookup(),
        capability=prep.capability,
        compare=FakeComparator(),
        projection=FakeProjector(),
        clock=container.clock,
        authority=AdmittingAuthority(),
    )
    container.job_registry._definitions[CREATE_JOB_TYPE] = create_job_definition(
        service, retry_policy=CREATE_POLICY
    )
    enqueue_create(
        container.jobs,
        container.registrations,
        intent_id=unit.intent_id,
        request=unit.request,
        frozen=unit.final,
    )
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.DEAD, result
    assert result.error_code == DUPLICATE_REVALIDATION_NOT_WIRED
    assert sender.calls == [] and store.attempts(unit.intent_id) == ()
