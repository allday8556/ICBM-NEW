"""A collection run never outlives its job (Issue #52 ruling 5720586391).

PR #75 measured the defect: a malformed image authority made the parser raise, the job died
`UNHANDLED_EXCEPTION`, and the run it owned stayed `PENDING` for good — an orphan nothing would
ever settle. The fix is a lifecycle contract, not a handler for that one exception: when the job
reaches a state it will never leave, the run it owns is terminal too.

What these tests hold to:

* an unexpected exception anywhere in the collection settles the run, whatever raised it;
* a retryable failure keeps the job alive and the run legitimately `PENDING`;
* an unexpected failure invents nothing — no revision, and no source-truth meaning it has no
  evidence for; the run says only that its job ended without a result, with the job system's own
  classification beside it;
* the same-product read the attempt already consumed stays consumed, so failing is not a way
  around the interval;
* the database, the service and the API say the same thing about the run.

Nothing here can reach a provider: the gateway is the offline fake.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin

import pytest

from app.api.routes.collect import collection_run as run_route
from app.collect.collection import (
    COLLECT_POLICY,
    COLLECT_PRODUCT_JOB,
    UNFINISHED_RUN,
    RegisteredCollection,
    pacing_key,
)
from app.collect.facts import FactsStatus
from app.collect.models import CollectionOutcome
from app.collect.runs import SameProductTooSoon
from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import TransientError
from app.core.ownership import acquire_data_dir
from app.jobs.models import JobState
from app.jobs.registry import TerminalJob
from integrations.suppliers.collection import ImageCandidate, ImageRoleRules
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    DETAIL_URL,
    EXTRACTOR_FINGERPRINT,
    EXTRACTOR_REVISION,
    PRIMARY_BYTES,
    PRIMARY_URL,
    PRODUCT_URL,
    SUPPLIER_KEY,
    FakeGateway,
    StubSessions,
    collection,
    page,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

INTERVAL = collection().profile.limits.same_product_interval_s


def registered(**changes: Any) -> RegisteredCollection:
    shop = collection()
    return RegisteredCollection(
        collection=replace(shop, **changes) if changes else shop,
        extractor_revision=EXTRACTOR_REVISION,
        extractor_fingerprint=EXTRACTOR_FINGERPRINT,
    )


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway(
        documents=[page()], images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES}
    )


def container_for(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, collected: RegisteredCollection
) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=gateway,
            collection_sessions=StubSessions(),
            collections=(collected,),
        )
        try:
            yield built
        finally:
            built.db.dispose()


@pytest.fixture
def collecting(config: AppConfig, clock: FakeClock, gateway: FakeGateway) -> Iterator[Container]:
    yield from container_for(config, clock, gateway, registered())


def submit_and_run(container: Container) -> tuple[str, str]:
    submitted = container.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    assert container.runner.run_next() is not None
    return submitted.collection_run_id, submitted.job_id


def revisions_held(container: Container) -> int:
    with closing(sqlite3.connect(container.config.database_path)) as raw:
        return raw.execute("SELECT COUNT(*) FROM product_facts_revisions").fetchone()[0]


def breaking(where: str) -> Callable[[Container], None]:
    """Make one step of the production collection raise something no one classified."""

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"unexpected bug in {where}")

    def inject(container: Container) -> None:
        service = container.collection
        if where == "parser roles":
            registered_collection = service._collections[SUPPLIER_KEY]
            service._collections[SUPPLIER_KEY] = replace(
                registered_collection,
                collection=replace(
                    registered_collection.collection,
                    roles=ImageRoleRules(identity=EXTRACTOR_REVISION, classify=crash),
                ),
            )
        elif where == "identity rule":
            registered_collection = service._collections[SUPPLIER_KEY]
            service._collections[SUPPLIER_KEY] = replace(
                registered_collection,
                collection=replace(registered_collection.collection, identity=crash),
            )
        elif where == "field parser":
            registered_collection = service._collections[SUPPLIER_KEY]
            service._collections[SUPPLIER_KEY] = replace(
                registered_collection,
                collection=replace(registered_collection.collection, fields=crash),
            )
        elif where == "asset recorder":
            service._recorder.record = crash  # type: ignore[method-assign]
        elif where == "revision append":
            service._revisions.append = crash  # type: ignore[method-assign]
        else:  # pragma: no cover - the table above is the whole list
            raise AssertionError(where)

    return inject


AFTER_THE_READ = (
    "identity rule",
    "field parser",
    "parser roles",
    "asset recorder",
    "revision append",
)


# ---------------------------------------------------------------- the invariant


@pytest.mark.parametrize("where", AFTER_THE_READ)
def test_an_unexpected_failure_anywhere_leaves_no_run_waiting_on_a_dead_job(
    collecting: Container, where: str
) -> None:
    breaking(where)(collecting)
    run_id, job_id = submit_and_run(collecting)

    job = collecting.jobs.get(job_id)
    run = collecting.collection.run(run_id)
    assert (job.state, job.last_error_class, job.last_error_code) == (
        JobState.DEAD,
        "UNKNOWN",
        "UNHANDLED_EXCEPTION",
    ), where
    # The invariant: the job is over, so the run is over.
    assert run.outcome is CollectionOutcome.FAILED, where
    assert run.finished_at is not None
    # G2: it says its job ended without a result, and claims nothing about the source.
    assert run.detail == f"{UNFINISHED_RUN}:UNHANDLED_EXCEPTION"
    assert run.revision_id is None and run.facts_status is None
    assert revisions_held(collecting) == 0, "no revision is invented for a failure"


def test_without_the_lifecycle_owner_the_same_failure_orphans_the_run(
    collecting: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The behaviour PR #75 measured, reproduced: with nothing settling the run when its job ends,
    # the job dies and the run stays PENDING for ever. This is what the fix removes.
    breaking("parser roles")(collecting)
    registry = collecting.runner._registry
    monkeypatch.setitem(
        registry._definitions,
        COLLECT_PRODUCT_JOB,
        replace(registry.get(COLLECT_PRODUCT_JOB), on_terminal=None),
    )
    run_id, job_id = submit_and_run(collecting)

    assert collecting.jobs.get(job_id).state == JobState.DEAD
    assert collecting.collection.run(run_id).outcome is CollectionOutcome.PENDING
    assert collecting.runner.run_next() is None, "nothing will ever settle it"


def test_a_retryable_failure_keeps_the_job_alive_and_the_run_pending(
    config: AppConfig, clock: FakeClock
) -> None:
    # (a) valid non-terminal PENDING: the job may still run, so the run has no answer yet.
    class Flaky(FakeGateway):
        attempts = 0

        def read_document(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            Flaky.attempts += 1
            if Flaky.attempts == 1:
                raise TransientError("COLLECT_TIMEOUT", "the supplier did not answer in time")
            return super().read_document(*args, **kwargs)  # type: ignore[arg-type]

    gateway = Flaky(
        documents=[page()], images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES}
    )
    for container in container_for(config, clock, gateway, registered()):
        run_id, job_id = submit_and_run(container)
        job = container.jobs.get(job_id)
        assert job.state == JobState.RETRY_SCHEDULED and job.next_attempt_at is not None
        run = container.collection.run(run_id)
        assert run.outcome is CollectionOutcome.PENDING and run.finished_at is None
        assert run.detail is None, "a scheduled retry is not a failure"

        # The retry then answers it, so the PENDING above was legitimate, not an orphan.
        clock.advance(COLLECT_POLICY.delay_after(1).total_seconds() + 1)
        assert container.runner.run_next() is not None
        assert container.jobs.get(job_id).state == JobState.SUCCEEDED
        assert container.collection.run(run_id).outcome is CollectionOutcome.RECORDED


def test_a_classified_failure_that_runs_out_of_attempts_keeps_its_own_reason(
    config: AppConfig, clock: FakeClock
) -> None:
    class Down(FakeGateway):
        def read_document(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise TransientError("COLLECT_SERVER_ERROR", "the supplier answered HTTP 503")

    for container in container_for(config, clock, Down(documents=[page()]), registered()):
        run_id, job_id = submit_and_run(container)
        for _ in range(2):
            clock.advance(INTERVAL * 20)
            container.runner.run_next()

        assert container.jobs.get(job_id).state == JobState.DEAD
        run = container.collection.run(run_id)
        assert run.outcome is CollectionOutcome.FAILED
        # The classified path settled it first, so its own reason stands, not the lifecycle one.
        assert run.detail == "COLLECT_SERVER_ERROR"


# ---------------------------------------------------------------- pacing


def test_a_terminal_failure_never_buys_an_earlier_next_read(
    collecting: Container, clock: FakeClock
) -> None:
    breaking("revision append")(collecting)
    run_id, _ = submit_and_run(collecting)
    run = collecting.collection.run(run_id)
    assert run.outcome is CollectionOutcome.FAILED

    # G4: what the attempt consumed stays consumed.
    assert run.product_read_at is not None and run.pacing_key is not None
    key = pacing_key(collection(), PRODUCT_URL)
    remaining = collecting.collection._runs.seconds_until_readable(key, interval_s=INTERVAL)
    assert remaining > 0
    with pytest.raises(SameProductTooSoon):
        collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)

    with closing(sqlite3.connect(collecting.config.database_path)) as raw:
        stored = raw.execute(
            "SELECT product_read_at, pacing_key FROM collection_runs WHERE collection_run_id = ?",
            (run_id,),
        ).fetchone()
    assert stored[0] is not None and stored[1] is not None

    # And the gate opens only when the interval says so, not because the attempt failed.
    clock.advance(INTERVAL + 1)
    assert collecting.collection._runs.seconds_until_readable(key, interval_s=INTERVAL) == 0


# ---------------------------------------------------------------- read-back


def test_the_database_the_service_and_the_api_agree_about_a_settled_run(
    collecting: Container,
) -> None:
    breaking("identity rule")(collecting)
    run_id, _ = submit_and_run(collecting)

    service = collecting.collection.run(run_id)
    api = run_route(run_id, collecting)
    with closing(sqlite3.connect(collecting.config.database_path)) as raw:
        row = raw.execute(
            "SELECT outcome, detail, revision_id, facts_status, finished_at IS NOT NULL "
            "FROM collection_runs WHERE collection_run_id = ?",
            (run_id,),
        ).fetchone()
    assert row == (
        CollectionOutcome.FAILED.value,
        f"{UNFINISHED_RUN}:UNHANDLED_EXCEPTION",
        None,
        None,
        1,
    )
    assert (api.outcome, api.detail, api.revision_id, api.facts_status) == (
        service.outcome,
        service.detail,
        service.revision_id,
        service.facts_status,
    )
    assert api.outcome is CollectionOutcome.FAILED and api.detail == row[1]


# ---------------------------------------------------------------- no regression


def test_a_normal_collection_still_records_its_revision(collecting: Container) -> None:
    run_id, job_id = submit_and_run(collecting)
    run = collecting.collection.run(run_id)
    assert collecting.jobs.get(job_id).state == JobState.SUCCEEDED
    assert run.outcome is CollectionOutcome.RECORDED
    assert run.revision_id is not None and run.facts_status is FactsStatus.CONFIRMED
    assert run.detail is None, "the lifecycle owner never touches a settled run"


@pytest.mark.parametrize(
    ("product_id", "outcome"),
    [("4242", CollectionOutcome.RECORDED), ("", CollectionOutcome.NO_REVISION)],
)
def test_the_lifecycle_owner_never_rewrites_an_answer_a_run_already_had(
    config: AppConfig, clock: FakeClock, product_id: str, outcome: CollectionOutcome
) -> None:
    gateway = FakeGateway(
        documents=[page(product_id=product_id)],
        images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES},
    )
    for container in container_for(config, clock, gateway, registered()):
        run_id, job_id = submit_and_run(container)
        settled = container.collection.run(run_id)
        assert settled.outcome is outcome

        # The job is terminal, so the owner is told — and finds nothing left to settle.
        container.collection._settle_unfinished_run(
            TerminalJob(
                job_id=job_id,
                job_type=COLLECT_PRODUCT_JOB,
                state=JobState.SUCCEEDED.value,
                attempt_no=1,
                correlation_id=settled.correlation_id,
                target_ref=SUPPLIER_KEY,
                error_class=None,
                error_code=None,
            )
        )
        again = container.collection.run(run_id)
        assert (again.outcome, again.detail, again.revision_id, again.finished_at) == (
            settled.outcome,
            settled.detail,
            settled.revision_id,
            settled.finished_at,
        )


def test_an_unresolved_identity_is_still_its_own_answer(
    config: AppConfig, clock: FakeClock
) -> None:
    gateway = FakeGateway(documents=[page(product_id="")])
    for container in container_for(config, clock, gateway, registered()):
        run_id, job_id = submit_and_run(container)
        assert container.jobs.get(job_id).state == JobState.SUCCEEDED
        run = container.collection.run(run_id)
        assert run.outcome is CollectionOutcome.NO_REVISION
        assert run.detail == "the page declares no product number"


def test_a_candidate_the_parser_cannot_resolve_ends_the_run_too(
    config: AppConfig, clock: FakeClock
) -> None:
    # The PR #75 case, now settled: the parser raises while resolving a malformed authority.
    def unresolvable(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
        # Resolving a malformed authority raises in the standard library, exactly as the KM parser
        # was measured doing in PR #75. Nothing here is KM-specific.
        urljoin(product_url, "https://[::1/d/x.png")
        raise AssertionError("unreachable: resolving the reference raises first")

    gateway = FakeGateway(documents=[page()])
    shop = registered(roles=ImageRoleRules(identity=EXTRACTOR_REVISION, classify=unresolvable))
    for container in container_for(config, clock, gateway, shop):
        run_id, job_id = submit_and_run(container)
        assert container.jobs.get(job_id).state == JobState.DEAD
        run = container.collection.run(run_id)
        assert run.outcome is CollectionOutcome.FAILED
        assert run.detail == f"{UNFINISHED_RUN}:UNHANDLED_EXCEPTION"
        assert run.revision_id is None and revisions_held(container) == 0
