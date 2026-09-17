"""The production one-product COLLECT path, offline (M3 Stage-B2, Issue #52 ruling 5706133893).

Every test drives the real services — the durable job, the orchestrator, the asset recorder, the
revision store and the read-back — against a synthetic supplier and a scripted transport. Nothing
here reimplements parsing, storage or persistence, and nothing here can reach a provider.
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.api.routes.collect import collection_run
from app.api.routes.collect import revision as revision_route
from app.collect.collection import (
    COLLECT_PRODUCT_JOB,
    ProductCollectionService,
    RegisteredCollection,
)
from app.collect.facts import FactsStatus, FieldStatus, ImageIssue, ImageRole
from app.collect.models import CollectionOutcome
from app.collect.runs import CollectionRunStore
from app.config import AppConfig
from app.container import Container, build_container
from app.core.errors import InputValidationError, TransientError
from app.core.ownership import acquire_data_dir
from app.jobs.models import JobState
from integrations.suppliers.collection import ImageResponse
from scripts.m3collect.fake_shop import (
    DETAIL_BYTES,
    DETAIL_URL,
    EXTRACTOR_FINGERPRINT,
    EXTRACTOR_REVISION,
    OTHER_BYTES,
    PRIMARY_BYTES,
    PRIMARY_URL,
    PRODUCT_URL,
    SUPPLIER_KEY,
    FakeGateway,
    StubSessions,
    collection,
    page,
    refusal,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration


def registered(**kwargs: object) -> RegisteredCollection:
    return RegisteredCollection(
        collection=collection(**kwargs),  # type: ignore[arg-type]
        extractor_revision=EXTRACTOR_REVISION,
        extractor_fingerprint=EXTRACTOR_FINGERPRINT,
    )


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway(
        documents=[page()],
        images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES},
    )


@pytest.fixture
def sessions() -> StubSessions:
    return StubSessions()


@pytest.fixture
def collecting(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, sessions: StubSessions
) -> Iterator[Container]:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=gateway,
            collection_sessions=sessions,
            collections=(registered(),),
        )
        try:
            yield built
        finally:
            built.db.dispose()


def run_once(container: Container) -> None:
    record = container.runner.run_next()
    assert record is not None, "the collection job did not run"


# ---------------------------------------------------------------- the whole path


def test_one_url_becomes_a_durable_job_a_revision_and_a_read_back(
    collecting: Container, gateway: FakeGateway, sessions: StubSessions
) -> None:
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    pending = collecting.collection.run(submitted.collection_run_id)
    assert pending.outcome is CollectionOutcome.PENDING
    assert pending.revision_id is None and pending.finished_at is None

    job = collecting.jobs.get(submitted.job_id)
    assert job.job_type == COLLECT_PRODUCT_JOB and job.state == JobState.QUEUED
    assert job.correlation_id == submitted.correlation_id

    run_once(collecting)
    assert collecting.jobs.get(submitted.job_id).state == JobState.SUCCEEDED

    finished = collecting.collection.run(submitted.collection_run_id)
    assert finished.outcome is CollectionOutcome.RECORDED
    assert finished.revision_id is not None and finished.finished_at is not None
    assert finished.facts_status is FactsStatus.CONFIRMED

    # The page was read once, with the session CONNECT owns, and nothing else was requested.
    assert gateway.document_reads == 1
    assert sessions.asked == [SUPPLIER_KEY]
    assert gateway.image_reads == [PRIMARY_URL, DETAIL_URL], "a layout asset is never fetched"

    stored = collecting.source_truth.revision(finished.revision_id)
    assert stored.supplier_key == SUPPLIER_KEY
    assert stored.source_product_id == "4242"
    assert stored.source_url == PRODUCT_URL
    assert stored.collection_run_id == submitted.collection_run_id
    assert stored.extractor_revision == EXTRACTOR_REVISION
    assert stored.extractor_fingerprint == EXTRACTOR_FINGERPRINT
    assert stored.sequence == 1
    assert stored.fingerprints_intact


def test_the_stored_bytes_are_the_ones_that_arrived(collecting: Container) -> None:
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    revision_id = collecting.collection.run(submitted.collection_run_id).revision_id
    assert revision_id is not None
    stored = collecting.source_truth.revision(revision_id)

    representative = [i for i in stored.images if i.role is ImageRole.REPRESENTATIVE]
    detail = [i for i in stored.images if i.role is ImageRole.DETAIL]
    assert len(representative) == 1 and len(detail) == 1
    primary = representative[0]
    assert primary.status is FieldStatus.CONFIRMED and primary.issue is None
    assert primary.ordinal == 0 and detail[0].ordinal == 1, "the page's own order is kept"
    assert primary.provenance == "fake.primary"
    assert primary.locator == PRIMARY_URL
    assert primary.http_etag == f'"etag-{len(PRIMARY_BYTES)}"'

    assert primary.asset is not None
    assert primary.asset.sha256 == hashlib.sha256(PRIMARY_BYTES).hexdigest()
    assert primary.asset.byte_size == len(PRIMARY_BYTES)
    assert (primary.asset.width, primary.asset.height) == (800, 600)
    assert primary.asset.mime_type == "image/png"
    assert collecting.source_assets.read(primary.asset.sha256) == PRIMARY_BYTES


def test_the_database_row_and_the_api_agree(collecting: Container) -> None:
    # The API adds nothing: what it answers is the row, field for field.
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    row = collecting.collection.run(submitted.collection_run_id)
    assert row.revision_id is not None

    answered = collection_run(row.collection_run_id, collecting)
    assert answered.outcome is row.outcome
    assert answered.revision_id == row.revision_id
    assert answered.facts_status is row.facts_status
    assert answered.job_id == row.job_id and answered.correlation_id == row.correlation_id
    assert answered.source_url == row.source_url
    assert answered.requested_at == row.requested_at
    assert answered.finished_at == row.finished_at

    stored = collecting.source_truth.revision(row.revision_id)
    assert revision_route(row.revision_id, collecting).model_dump() == stored.model_dump()
    assert stored.facts_status is row.facts_status
    assert [i.asset.sha256 for i in stored.images if i.asset] == [
        i.asset.sha256
        for i in revision_route(row.revision_id, collecting).images
        if i.asset is not None
    ]


# ---------------------------------------------------------------- content identity


def test_the_same_bytes_are_stored_once_and_each_run_appends_its_own_revision(
    collecting: Container, gateway: FakeGateway
) -> None:
    first = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    second = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)

    one = collecting.collection.run(first.collection_run_id)
    two = collecting.collection.run(second.collection_run_id)
    assert one.revision_id != two.revision_id
    history = collecting.source_truth.history(SUPPLIER_KEY, "4242")
    assert [r.sequence for r in history.revisions] == [1, 2]

    digests = {
        image.asset.sha256
        for revision in history.revisions
        for image in revision.images
        if image.asset is not None
    }
    assert len(digests) == 2, "two distinct images, stored once each"
    for digest in digests:
        assert len(list(Path(collecting.config.source_assets_dir).rglob(f"*{digest}*"))) <= 1


def test_the_same_url_serving_different_bytes_is_a_different_asset(
    collecting: Container, gateway: FakeGateway
) -> None:
    first = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    gateway.images[PRIMARY_URL] = OTHER_BYTES  # same URL, the provider now serves other content
    second = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)

    def primary(run_id: str) -> str:
        revision_id = collecting.collection.run(run_id).revision_id
        assert revision_id is not None
        stored = collecting.source_truth.revision(revision_id)
        asset = next(i.asset for i in stored.images if i.role is ImageRole.REPRESENTATIVE)
        assert asset is not None
        return asset.sha256

    assert primary(first.collection_run_id) == hashlib.sha256(PRIMARY_BYTES).hexdigest()
    assert primary(second.collection_run_id) == hashlib.sha256(OTHER_BYTES).hexdigest()


def test_a_validated_reuse_keeps_the_exact_content_and_still_appends_a_reference(
    collecting: Container, gateway: FakeGateway
) -> None:
    first = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    before = collecting.collection.run(first.collection_run_id).revision_id
    assert before is not None
    stored_digest = next(
        i.asset.sha256
        for i in collecting.source_truth.revision(before).images
        if i.role is ImageRole.REPRESENTATIVE and i.asset is not None
    )

    gateway.images[PRIMARY_URL] = ImageResponse(304, None, f'"etag-{len(PRIMARY_BYTES)}"', None)
    second = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    after = collecting.collection.run(second.collection_run_id).revision_id
    assert after is not None and after != before

    revision = collecting.source_truth.revision(after)
    reused = next(i for i in revision.images if i.role is ImageRole.REPRESENTATIVE)
    assert reused.status is FieldStatus.CONFIRMED and reused.issue is None
    assert reused.asset is not None and reused.asset.sha256 == stored_digest
    assert reused.ordinal == 0 and reused.locator == PRIMARY_URL
    assert collecting.source_assets.read(stored_digest) == PRIMARY_BYTES


def test_an_image_the_provider_refuses_is_recorded_as_review_not_as_a_failure(
    collecting: Container, gateway: FakeGateway
) -> None:
    gateway.images[DETAIL_URL] = refusal()
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    run = collecting.collection.run(submitted.collection_run_id)
    assert run.outcome is CollectionOutcome.RECORDED and run.revision_id is not None

    stored = collecting.source_truth.revision(run.revision_id)
    detail = next(i for i in stored.images if i.role is ImageRole.DETAIL)
    assert detail.status is FieldStatus.REVIEW_REQUIRED
    assert detail.issue is ImageIssue.BAD_CONTENT_TYPE
    assert detail.asset is None
    assert stored.facts_status is FactsStatus.REVIEW_REQUIRED


# ---------------------------------------------------------------- the result contract


def test_an_unresolved_identity_records_no_revision_and_is_not_a_failure(
    collecting: Container, gateway: FakeGateway
) -> None:
    gateway.documents = [page(product_id="")]
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)

    job = collecting.jobs.get(submitted.job_id)
    assert job.state == JobState.SUCCEEDED, "the run answered; there is nothing to retry"
    run = collecting.collection.run(submitted.collection_run_id)
    assert run.outcome is CollectionOutcome.NO_REVISION
    assert run.revision_id is None and run.facts_status is None
    assert run.detail == "the page declares no product number"
    assert run.finished_at is not None
    assert collecting.source_truth.history(SUPPLIER_KEY, "4242").revisions == ()
    assert gateway.image_reads == [], "no identity, no image requests"


def test_a_core_field_under_review_can_never_be_a_confirmed_revision(
    collecting: Container, gateway: FakeGateway
) -> None:
    gateway.documents = [page(stock_unclear=True)]
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    run = collecting.collection.run(submitted.collection_run_id)
    assert run.revision_id is not None

    stored = collecting.source_truth.revision(run.revision_id)
    reviewed = [f.key for f in stored.fields if f.status is FieldStatus.REVIEW_REQUIRED]
    assert reviewed == ["stock"], "the page does not say plainly, so the parser did not either"
    assert stored.facts_status is FactsStatus.REVIEW_REQUIRED
    assert run.facts_status is FactsStatus.REVIEW_REQUIRED
    # The revision exists and keeps everything else it did read: review is recorded, not discarded.
    assert any(f.status is FieldStatus.CONFIRMED for f in stored.fields)


def test_a_transient_failure_retries_and_review_ambiguity_never_does(
    collecting: Container, gateway: FakeGateway, clock: FakeClock
) -> None:
    gateway.images[PRIMARY_URL] = TransientError("PROVIDER_BUSY", "try again")
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)

    # An image refusal is not a failed collection, so nothing retried: the reference is REVIEW.
    assert collecting.jobs.get(submitted.job_id).state == JobState.SUCCEEDED
    run = collecting.collection.run(submitted.collection_run_id)
    assert run.outcome is CollectionOutcome.RECORDED
    assert gateway.document_reads == 1, "a field under review never triggers another read"

    # A transient failure of the product read itself is the only thing that retries.
    class Busy(FakeGateway):
        def read_document(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            self.document_reads += 1
            raise TransientError("PROVIDER_BUSY", "try again")

    busy = Busy()
    service = ProductCollectionService(
        db=collecting.db,
        clock=clock,
        jobs=collecting.jobs,
        runs=collecting.collection._runs,  # the same durable store, driven directly
        revisions=collecting.revisions,
        recorder=collecting.source_asset_recorder,
        sessions=StubSessions(),
        gateway=busy,
        collections=(registered(),),
    )
    with pytest.raises(TransientError):
        service.collect(SUPPLIER_KEY, PRODUCT_URL, run_id="run-transient")
    assert busy.document_reads == 1, "the service itself never loops; the job policy decides"


def test_a_transient_product_read_leaves_the_run_open_for_its_retry(
    config: AppConfig, clock: FakeClock
) -> None:
    # The run's answer is not written while the job still has attempts left: the attempt history
    # carries the failure, and the next attempt records the real outcome.
    class Flaky(FakeGateway):
        failures: int = 1

        def read_document(self, *args: object, **kwargs: object) -> object:
            if self.failures:
                self.failures -= 1
                self.document_reads += 1
                raise TransientError("PROVIDER_BUSY", "try again")
            return super().read_document(*args, **kwargs)  # type: ignore[arg-type]

    gateway = Flaky(
        documents=[page()], images={PRIMARY_URL: PRIMARY_BYTES, DETAIL_URL: DETAIL_BYTES}
    )
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=gateway,
            collection_sessions=StubSessions(),
            collections=(registered(),),
        )
        try:
            submitted = built.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
            first = built.runner.run_next()
            assert first is not None and first.state is JobState.RETRY_SCHEDULED
            open_run = built.collection.run(submitted.collection_run_id)
            assert open_run.outcome is CollectionOutcome.PENDING
            assert open_run.detail is None and open_run.finished_at is None

            clock.advance(5.0)
            second = built.runner.run_next()
            assert second is not None and second.state is JobState.SUCCEEDED
            settled = built.collection.run(submitted.collection_run_id)
            assert settled.outcome is CollectionOutcome.RECORDED
            assert gateway.document_reads == 2
        finally:
            built.db.dispose()


def test_a_settled_run_keeps_the_answer_it_first_recorded(collecting: Container) -> None:
    # A run reaches one outcome. A second attempt of the same job, or any later caller, never
    # rewrites what the first answer said.
    submitted = collecting.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
    run_once(collecting)
    settled = collecting.collection.run(submitted.collection_run_id)
    assert settled.outcome is CollectionOutcome.RECORDED

    runs = CollectionRunStore(collecting.db, collecting.clock)
    runs.no_revision(submitted.collection_run_id, reason="a later caller disagrees")
    runs.failed(submitted.collection_run_id, detail="so does this one")

    unchanged = collecting.collection.run(submitted.collection_run_id)
    assert unchanged.outcome is CollectionOutcome.RECORDED
    assert unchanged.revision_id == settled.revision_id
    assert unchanged.detail is None
    assert unchanged.finished_at == settled.finished_at


# ---------------------------------------------------------------- scope and egress


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.collect.invalid/product/sample/",  # not a product path
        "https://shop.collect.invalid/product/list/4242/page/2/",  # pagination
        "https://other.collect.invalid/product/sample/4242/",  # another host
        "http://shop.collect.invalid/product/sample/4242/",  # not https
        "https://user:pw@shop.collect.invalid/product/sample/4242/",  # credentials
        "https://shop.collect.invalid/category/23/",  # a listing
    ],
)
def test_only_one_product_page_can_ever_be_submitted(collecting: Container, url: str) -> None:
    with pytest.raises(InputValidationError):
        collecting.collection.submit(SUPPLIER_KEY, url)
    assert collecting.jobs.count(job_type_prefix="collect.") == 0


def test_a_run_may_read_exactly_one_document(collecting: Container, gateway: FakeGateway) -> None:
    from app.collect.collection import RunBudget
    from integrations.suppliers.collection import ReadKind
    from integrations.suppliers.transport.collection import CollectionBudgetRefused

    budget = RunBudget(max_product_reads=1, max_image_requests=2)
    budget.reserve(ReadKind.PRODUCT_READ, PRODUCT_URL)
    with pytest.raises(CollectionBudgetRefused):
        budget.reserve(ReadKind.PRODUCT_READ, PRODUCT_URL)
    with pytest.raises(CollectionBudgetRefused):
        budget.reserve(ReadKind.POLICY_READ, "/robots.txt")


def test_a_run_cannot_spend_more_image_requests_than_its_profile_allows(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            collection_gateway=gateway,
            collection_sessions=StubSessions(),
            collections=(registered(max_image_requests=1),),
        )
        try:
            submitted = built.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
            assert built.runner.run_next() is not None
            run = built.collection.run(submitted.collection_run_id)
            assert run.revision_id is not None
            stored = built.source_truth.revision(run.revision_id)
            detail = next(i for i in stored.images if i.role is ImageRole.DETAIL)
            assert detail.status is FieldStatus.REVIEW_REQUIRED
            assert detail.issue is ImageIssue.BUDGET_EXHAUSTED
            assert len(gateway.image_reads) == 1
        finally:
            built.db.dispose()


def test_under_a_test_run_no_collection_transport_can_exist(collecting: Container) -> None:
    from integrations.suppliers.transport.collection import (
        DeferredCollectionGateway,
        LiveTransportRefused,
        PolicedCollectionGateway,
    )

    with pytest.raises(LiveTransportRefused):
        PolicedCollectionGateway()
    deferred = DeferredCollectionGateway()
    with pytest.raises(LiveTransportRefused):
        deferred.read_image(
            collection().profile,
            PRIMARY_URL,
            budget=RunBudgetStub(),  # type: ignore[arg-type]
        )


class RunBudgetStub:
    def reserve(self, kind: object, subject: str) -> None:
        raise AssertionError("nothing may be reserved: the transport must refuse first")
