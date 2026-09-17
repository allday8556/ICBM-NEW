"""A collection run never outlives its job (Issue #52 rulings 5720586391 and 5721367502).

PR #75 measured the defect: a malformed image authority made the parser raise, the job died
`UNHANDLED_EXCEPTION`, and the run it owned stayed `PENDING` for good — an orphan nothing would
ever settle. The fix is a lifecycle contract, not a handler for that one exception: when the job
reaches a state it will never leave, the run it owns is terminal too.

The prompt call that settles it is not where correctness lives, because it happens after the job's
state is committed and can fail, or never happen at all. What a settlement did not do stays
written down — a terminal job beside a run with no outcome — and the reconciliation sweep finds it
there and finishes it.

What these tests hold to:

* an unexpected exception anywhere in the collection settles the run, whatever raised it;
* a settlement that fails, and a process that ends a job and tells no one, both leave that run
  discoverable in the database and both converge on a later sweep;
* a retryable failure keeps the job alive and the run legitimately `PENDING`, and is never swept;
* an unexpected failure invents nothing — no revision, and no source-truth meaning it has no
  evidence for; the run says only that its job ended without a result, with the job system's own
  classification beside it;
* the same-product read the attempt already consumed stays consumed through a failed settlement
  and the sweep, so failing is not a way around the interval;
* an answer a run already had is never rewritten, and the image diagnostics of PR #74/#75 are
  untouched;
* the database, the service and the API say the same thing about the run.

Nothing here can reach a provider: the gateway is the offline fake, and the sweep sends nothing.
"""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from uuid import uuid4

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
from app.collect.models import CollectionOutcome, CollectionRun
from app.collect.runs import SameProductTooSoon
from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import TransientError
from app.core.ownership import acquire_data_dir
from app.jobs.models import TERMINAL_STATES, AttemptOutcome, Job, JobAttempt, JobState
from app.jobs.registry import TerminalHook, TerminalJob
from integrations.suppliers.collection import ImageCandidate, ImageRoleRules
from integrations.suppliers.collection import ImageRole as SourceRole
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    DETAIL_URL,
    EXTRACTOR_FINGERPRINT,
    EXTRACTOR_REVISION,
    IMAGE_HOST,
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


def orphaned(container: Container) -> list[tuple[str, str]]:
    """The inconsistency the ruling names (S1), asked of the database and nothing else: a job that
    can no longer run, beside a run of its own that is still waiting for an answer."""
    with closing(sqlite3.connect(container.config.database_path)) as raw:
        return raw.execute(
            "SELECT j.job_id, r.collection_run_id FROM jobs j"
            " JOIN collection_runs r ON r.job_id = j.job_id"
            " WHERE j.state IN ('SUCCEEDED', 'DEAD') AND r.outcome = 'PENDING'"
        ).fetchall()


def stored_pacing(container: Container, run_id: str) -> tuple[Any, Any]:
    with closing(sqlite3.connect(container.config.database_path)) as raw:
        return raw.execute(
            "SELECT product_read_at, pacing_key FROM collection_runs WHERE collection_run_id = ?",
            (run_id,),
        ).fetchone()


def image_refs(container: Container) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(container.config.database_path)) as raw:
        return raw.execute(
            "SELECT ordinal, role, status, issue, locator, sha256, source_form, source_trimmed,"
            " target_refusal FROM product_facts_image_refs ORDER BY ordinal"
        ).fetchall()


class SettlesAfter:
    """An owner whose first settlements fail — a locked database, a process that stops there.

    The job is already terminal by then, so each failure is exactly the case the sweep exists for.
    After ``failures`` of them it does what the registered owner was always going to do.
    """

    def __init__(self, real: TerminalHook, failures: int) -> None:
        self._real = real
        self._failures = failures
        self.calls = 0

    def __call__(self, terminal: TerminalJob) -> None:
        self.calls += 1
        if self.calls <= self._failures:
            raise RuntimeError("settling the run failed")
        self._real(terminal)


def owner_is(
    container: Container, hook: TerminalHook | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace the collect job's terminal hook, leaving the rest of its definition registered."""
    registry = container.runner._registry
    monkeypatch.setitem(
        registry._definitions,
        COLLECT_PRODUCT_JOB,
        replace(registry.get(COLLECT_PRODUCT_JOB), on_terminal=hook),
    )


@dataclass
class FlakyOnce(FakeGateway):
    """The first read fails the way the shared job policy is willing to retry; the next works."""

    attempts: int = 0

    def read_document(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        self.attempts += 1
        if self.attempts == 1:
            raise TransientError("COLLECT_TIMEOUT", "the supplier did not answer in time")
        return super().read_document(*args, **kwargs)  # type: ignore[arg-type]


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


# ---------------------------------------------------------------- reconciliation


def test_a_settlement_that_fails_leaves_a_run_the_sweep_can_still_find(
    collecting: Container, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaking("revision append")(collecting)
    owner = SettlesAfter(collecting.collection._settle_unfinished_run, failures=1)
    owner_is(collecting, owner, monkeypatch)
    run_id, job_id = submit_and_run(collecting)

    # S1: the job is over, its run is still waiting, and neither half of that is in memory.
    assert owner.calls == 1
    assert collecting.jobs.get(job_id).state == JobState.DEAD
    assert collecting.collection.run(run_id).outcome is CollectionOutcome.PENDING
    assert orphaned(collecting) == [(job_id, run_id)]
    spent = (gateway.document_reads, list(gateway.image_reads))

    # S2: the sweep reads the same two rows and finishes what the callback did not.
    assert collecting.runner.reconcile_terminal_owners() == 1
    run = collecting.collection.run(run_id)
    assert run.outcome is CollectionOutcome.FAILED and run.finished_at is not None
    assert run.detail == f"{UNFINISHED_RUN}:UNHANDLED_EXCEPTION"
    assert run.revision_id is None and run.facts_status is None
    assert revisions_held(collecting) == 0, "a sweep invents no revision"
    # S3: nothing is left behind, and asking again changes nothing.
    assert orphaned(collecting) == []
    assert collecting.runner.reconcile_terminal_owners() == 0
    assert (gateway.document_reads, list(gateway.image_reads)) == spent, "and it sends nothing"


def test_a_reconciliation_that_fails_erases_nothing_and_the_next_one_converges(
    collecting: Container, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    breaking("identity rule")(collecting)
    owner = SettlesAfter(collecting.collection._settle_unfinished_run, failures=2)
    owner_is(collecting, owner, monkeypatch)
    run_id, job_id = submit_and_run(collecting)
    assert orphaned(collecting) == [(job_id, run_id)], "the prompt call failed"

    # G7: the first sweep fails too. It says what it found, and marks nothing as reconciled.
    assert collecting.runner.reconcile_terminal_owners() == 1
    assert orphaned(collecting) == [(job_id, run_id)]
    assert collecting.collection.run(run_id).outcome is CollectionOutcome.PENDING
    assert [r for r in caplog.records if "owner_left_unsettled" in r.getMessage()]
    assert [r for r in caplog.records if "terminal_owner_failed" in r.getMessage()]

    assert collecting.runner.reconcile_terminal_owners() == 1
    assert collecting.collection.run(run_id).outcome is CollectionOutcome.FAILED
    assert orphaned(collecting) == [] and collecting.runner.reconcile_terminal_owners() == 0


def test_a_process_that_ends_a_job_and_tells_no_one_converges_on_the_next_sweep(
    collecting: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    # S4: not a hook that failed — a process that committed the job's terminal state and stopped
    # before anything of its owner's was called.
    breaking("asset recorder")(collecting)
    owner_is(collecting, None, monkeypatch)
    run_id, job_id = submit_and_run(collecting)
    assert collecting.jobs.get(job_id).state == JobState.DEAD
    assert orphaned(collecting) == [(job_id, run_id)]

    monkeypatch.undo()  # the next process starts with the owner the job type always declared
    assert collecting.runner.reconcile_terminal_owners() == 1
    run = collecting.collection.run(run_id)
    assert run.outcome is CollectionOutcome.FAILED
    assert run.detail == f"{UNFINISHED_RUN}:UNHANDLED_EXCEPTION"
    assert run.revision_id is None and revisions_held(collecting) == 0
    assert orphaned(collecting) == []


def test_a_run_whose_job_can_still_run_is_never_reconciled(
    config: AppConfig, clock: FakeClock
) -> None:
    gateway = FlakyOnce(
        documents=[page()], images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES}
    )
    for container in container_for(config, clock, gateway, registered()):
        run_id, job_id = submit_and_run(container)
        assert container.jobs.get(job_id).state == JobState.RETRY_SCHEDULED
        assert orphaned(container) == [], "a job that can still run is no inconsistency"

        assert container.runner.reconcile_terminal_owners() == 0
        run = container.collection.run(run_id)
        assert run.outcome is CollectionOutcome.PENDING and run.detail is None

        # And the retry then gives the run its real answer, which the sweep did not pre-empt.
        clock.advance(COLLECT_POLICY.delay_after(1).total_seconds() + 1)
        assert container.runner.run_next() is not None
        assert container.collection.run(run_id).outcome is CollectionOutcome.RECORDED
        assert container.runner.reconcile_terminal_owners() == 0


@pytest.mark.parametrize(
    ("product_id", "outcome"),
    [("4242", CollectionOutcome.RECORDED), ("", CollectionOutcome.NO_REVISION)],
)
def test_a_sweep_never_rewrites_an_answer_a_run_already_had(
    config: AppConfig, clock: FakeClock, product_id: str, outcome: CollectionOutcome
) -> None:
    gateway = FakeGateway(
        documents=[page(product_id=product_id)],
        images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES},
    )
    for container in container_for(config, clock, gateway, registered()):
        run_id, _ = submit_and_run(container)
        settled = container.collection.run(run_id)
        assert settled.outcome is outcome
        assert orphaned(container) == [], "an answered run is not waiting on anything"

        assert container.runner.reconcile_terminal_owners() == 0
        again = container.collection.run(run_id)
        assert (again.outcome, again.detail, again.revision_id, again.facts_status) == (
            settled.outcome,
            settled.detail,
            settled.revision_id,
            settled.facts_status,
        )
        assert again.finished_at == settled.finished_at


def test_a_sweep_leaves_a_classified_failure_with_its_own_reason(
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

        assert container.runner.reconcile_terminal_owners() == 0
        assert container.collection.run(run_id).detail == "COLLECT_SERVER_ERROR"


def test_pacing_survives_a_failed_settlement_and_the_sweep(
    collecting: Container, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaking("field parser")(collecting)
    owner = SettlesAfter(collecting.collection._settle_unfinished_run, failures=1)
    owner_is(collecting, owner, monkeypatch)
    run_id, _ = submit_and_run(collecting)

    # G8: what the attempt reserved is already stored, and the sweep is not allowed to give it back.
    reserved = stored_pacing(collecting, run_id)
    assert reserved[0] is not None and reserved[1] is not None
    assert collecting.runner.reconcile_terminal_owners() == 1
    assert stored_pacing(collecting, run_id) == reserved

    key = pacing_key(collection(), PRODUCT_URL)
    assert collecting.collection._runs.seconds_until_readable(key, interval_s=INTERVAL) > 0
    with pytest.raises(SameProductTooSoon):
        collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    clock.advance(INTERVAL + 1)
    assert collecting.collection._runs.seconds_until_readable(key, interval_s=INTERVAL) == 0


def test_the_database_the_service_and_the_api_agree_after_a_sweep_converges(
    collecting: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    breaking("parser roles")(collecting)
    owner = SettlesAfter(collecting.collection._settle_unfinished_run, failures=1)
    owner_is(collecting, owner, monkeypatch)
    run_id, _ = submit_and_run(collecting)
    assert collecting.runner.reconcile_terminal_owners() == 1

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
    assert api.detail == row[1]


def test_a_sweep_leaves_the_image_diagnostics_of_a_recorded_revision_alone(
    config: AppConfig, clock: FakeClock
) -> None:
    # PR #74/#75: one reference the target check allows, one it refuses before anything is sent.
    refused = f"http://{IMAGE_HOST}/p/detail.png"

    def written(body: str, product_url: str) -> tuple[ImageCandidate, ...]:
        return (
            ImageCandidate(PRIMARY_URL, SourceRole.PRIMARY, 0, "fake.primary", source=PRIMARY_URL),
            ImageCandidate(refused, SourceRole.DETAIL, 1, "fake.detail", source=refused),
        )

    gateway = FakeGateway(documents=[page()], images={PRIMARY_URL: PRIMARY_BYTES})
    shop = registered(roles=ImageRoleRules(identity=EXTRACTOR_REVISION, classify=written))
    for container in container_for(config, clock, gateway, shop):
        run_id, _ = submit_and_run(container)
        assert container.collection.run(run_id).outcome is CollectionOutcome.RECORDED
        assert gateway.image_reads == [PRIMARY_URL], "a refused target is never requested"
        before = image_refs(container)
        assert [row[8] for row in before] == [None, "NON_HTTPS"]
        assert before[1][4] is None and before[1][5] is None, "and it resolved to nothing"

        assert container.runner.reconcile_terminal_owners() == 0
        assert image_refs(container) == before
        assert gateway.image_reads == [PRIMARY_URL]


# ---------------------------------------------------------------- the bound, and what it hides


def waiting_runs(container: Container, count: int, *, state: JobState, from_when: int) -> list[str]:
    """``count`` runs with no outcome, each owned by a job in ``state``, requested before now.

    They stand for an ordinary backlog: a page of work the sweep has to look past. Writing them
    directly is the point — the question here is what the discovery query returns when there are
    more of them than any one sweep may answer with, not how they were made.
    """
    run_ids: list[str] = []
    with container.db.write() as session:
        for index in range(count):
            at = container.clock.now() - timedelta(seconds=from_when - index)
            job_id, run_id = str(uuid4()), str(uuid4())
            session.add(
                Job(
                    job_id=job_id,
                    job_type=COLLECT_PRODUCT_JOB,
                    target_ref=SUPPLIER_KEY,
                    payload_json="{}",
                    state=state,
                    attempt_count=1 if state in TERMINAL_STATES else 0,
                    max_attempts=3,
                    next_attempt_at=None if state in TERMINAL_STATES else at + timedelta(days=1),
                    correlation_id=f"backlog-{index}",
                    last_error_class="UNKNOWN" if state is JobState.DEAD else None,
                    last_error_code="UNHANDLED_EXCEPTION" if state is JobState.DEAD else None,
                    created_at=at,
                    started_at=at if state in TERMINAL_STATES else None,
                    finished_at=at if state in TERMINAL_STATES else None,
                    updated_at=at,
                )
            )
            if state in TERMINAL_STATES:
                session.add(
                    JobAttempt(
                        job_id=job_id,
                        attempt_no=1,
                        scheduled_for=at,
                        started_at=at,
                        finished_at=at,
                        outcome=AttemptOutcome.FAILED,
                        error_class="UNKNOWN",
                        error_code="UNHANDLED_EXCEPTION",
                        error_message="unexpected bug",
                    )
                )
            session.add(
                CollectionRun(
                    collection_run_id=run_id,
                    job_id=job_id,
                    correlation_id=f"backlog-{index}",
                    supplier_key=SUPPLIER_KEY,
                    source_url=f"https://shop.collect.invalid/product/sample/{index}/",
                    outcome=CollectionOutcome.PENDING,
                    revision_id=None,
                    facts_status=None,
                    detail=None,
                    requested_at=at,
                    product_read_at=None,
                    pacing_key=None,
                    source_product_id=None,
                    finished_at=None,
                )
            )
            run_ids.append(run_id)
    return run_ids


def test_a_page_of_jobs_that_can_still_run_never_hides_an_orphan_behind_it(
    collecting: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 501 runs waiting on jobs that are alive and due tomorrow — more than the discovery bound —
    # and all of them requested before the orphan, so any bound taken before the join would return
    # nothing but these, sweep after sweep (ruling 5721796080 §1).
    alive = waiting_runs(collecting, 501, state=JobState.QUEUED, from_when=10_000)
    assert len(collecting.collection._runs.unsettled_job_ids(("SUCCEEDED", "DEAD"))) == 0

    breaking("revision append")(collecting)
    owner_is(collecting, None, monkeypatch)
    run_id, job_id = submit_and_run(collecting)  # the orphan, requested last
    monkeypatch.undo()
    assert orphaned(collecting) == [(job_id, run_id)]

    assert collecting.runner.reconcile_terminal_owners() == 1, "the bound is spent on orphans only"
    settled = collecting.collection.run(run_id)
    assert settled.outcome is CollectionOutcome.FAILED
    assert settled.detail == f"{UNFINISHED_RUN}:UNHANDLED_EXCEPTION"

    # And the backlog is exactly as it was: a job that can still run is nobody's inconsistency.
    for other in alive:
        run = collecting.collection.run(other)
        assert run.outcome is CollectionOutcome.PENDING
        assert run.detail is None and run.finished_at is None
    assert collecting.runner.reconcile_terminal_owners() == 0


def test_bounded_sweeps_keep_reaching_further_until_none_is_left(
    collecting: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    # More orphans than one sweep may answer with, behind a backlog of jobs that are still alive.
    waiting_runs(collecting, 20, state=JobState.QUEUED, from_when=10_000)
    orphans = waiting_runs(collecting, 7, state=JobState.DEAD, from_when=9_000)
    store = collecting.collection._runs
    registry = collecting.runner._registry
    monkeypatch.setitem(
        registry._definitions,
        COLLECT_PRODUCT_JOB,
        replace(
            registry.get(COLLECT_PRODUCT_JOB),
            # The same query, with a bound small enough to need several sweeps.
            unsettled_owned_jobs=lambda states: store.unsettled_job_ids(states, limit=3),
        ),
    )

    remaining = [len(orphaned(collecting))]
    found = []
    for _ in range(4):
        found.append(collecting.runner.reconcile_terminal_owners())
        remaining.append(len(orphaned(collecting)))

    assert remaining[0] == 7, "every orphan is discoverable from the start"
    assert found == [3, 3, 1, 0], "each sweep settles what it is allowed to, and no more"
    assert remaining == [7, 4, 1, 0, 0], "strictly fewer every time, then none"
    for run_id in orphans:
        run = collecting.collection.run(run_id)
        assert run.outcome is CollectionOutcome.FAILED
        assert run.detail == f"{UNFINISHED_RUN}:UNHANDLED_EXCEPTION"
    assert revisions_held(collecting) == 0


def test_the_owner_asks_with_the_states_it_is_given_and_defines_none(
    collecting: Container,
) -> None:
    store = collecting.collection._runs
    waiting = waiting_runs(collecting, 2, state=JobState.QUEUED, from_when=100)
    dead = waiting_runs(collecting, 1, state=JobState.DEAD, from_when=50)

    # It applies the predicate it is handed, whatever that is — including states that are not
    # terminal at all, which it has no opinion about.
    by_job = {run_id: collecting.collection.run(run_id).job_id for run_id in (*waiting, *dead)}
    assert set(store.unsettled_job_ids(("QUEUED",))) == {by_job[run] for run in waiting}
    assert set(store.unsettled_job_ids(("DEAD",))) == {by_job[run] for run in dead}
    assert set(store.unsettled_job_ids(())) == set(), "and it invents none of its own"

    source = (Path(__file__).resolve().parents[2] / "app" / "collect" / "runs.py").read_text(
        encoding="utf-8"
    )
    assert "SUCCEEDED" not in source and "DEAD" not in source


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
