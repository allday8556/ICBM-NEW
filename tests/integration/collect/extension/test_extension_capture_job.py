"""The extension capture job: its in-process buffer (ruling 5906712259 N-1) and, from E2, the
revision it records through the canonical collection pipeline (ADR-0019 §2, §10).

These tests run the owners directly, with a clock they control and no worker: each job attempt
happens exactly when the test runs it, so a restart and an interruption can be staged. The
collection gateway is a script that sends nothing.
"""

import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.capabilities.jobs.models import JobState
from app.config import AppConfig
from app.container import Container, build_container
from app.platform.core.errors import AppError
from app.platform.core.ownership import acquire_data_dir
from app.platform.core.secrets import MemorySecretStore
from app.stages.collect.adaptive.store.gate import build_supplier_gate
from app.stages.collect.collection import ProductCollectionService
from app.stages.collect.extension.capture import CaptureEnvelope
from app.stages.collect.extension.policy import POLICY_FILE
from app.stages.collect.extension.service import (
    EXTENSION_CAPTURE_BUFFER_MISSING,
    EXTENSION_CAPTURE_JOB,
    EXTENSION_CAPTURE_POLICY,
    MINIMUM_INGEST_INTERVAL_S,
    CaptureReport,
    ExtensionCaptureService,
)
from app.stages.collect.facts import FactsStatus
from app.stages.collect.models import CollectionOutcome, TransportKind
from automation.acceptance.m3.rehearsal import fake_shop
from automation.acceptance.m3.rehearsal.fake_shop import FakeGateway, StubSessions
from tests.support.extension_support import (
    BODY,
    envelope,
    frame,
    pair,
    post_capture,
    wait_for_outcome,
)
from tests.support.jobs_support import TEST_JOBS, FakeClock
from tests.support.shadow_support import count, enabled_profile, registered, rows

pytestmark = pytest.mark.integration

MARKER = "합성-캡처-표식-7f3a"
KM_IMAGES = [
    "https://kmretail.co.kr/web/product/big/synthetic-9001.jpg",
    "https://kmretail.co.kr/web/upload/synthetic/detail-1.jpg",
]


def _accept(app: Container, html: str | None = None, **changes: Any) -> str:
    accepted = app.extension_capture.ingest(
        CaptureEnvelope.model_validate(envelope(html, **changes))
    )
    return accepted.collection_run_id


def _job(config: AppConfig, run_id: str) -> tuple[Any, ...]:
    return rows(
        config,
        "SELECT j.job_type, j.state, j.attempt_count, j.max_attempts, j.last_error_code,"
        " j.payload_json FROM jobs j JOIN collection_runs r ON r.job_id = j.job_id"
        " WHERE r.collection_run_id = ?",
        run_id,
    )[0]


# ---------------------------------------------------------------- N-1, the five required tests


def test_1_an_accepted_job_with_its_buffer_is_recorded(
    container: Container, config: AppConfig, gateway: FakeGateway
) -> None:
    run_id = _accept(container)
    assert container.collection.run(run_id).outcome is CollectionOutcome.PENDING
    assert len(container.extension_capture._buffer) == 1
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.SUCCEEDED
    run = container.collection.run(run_id)
    assert run.outcome is CollectionOutcome.RECORDED and run.revision_id is not None
    assert run.provenance is not None
    assert run.provenance.transport_kind is TransportKind.EXTENSION
    assert _job(config, run_id)[:4] == (EXTENSION_CAPTURE_JOB, "SUCCEEDED", 1, 1)
    # The capture was handed over once and is held nowhere afterwards.
    assert len(container.extension_capture._buffer) == 0
    assert container.runner.run_next() is None
    # The server read no product page: the browser did. It fetched the images the page names.
    assert gateway.document_reads == 0
    assert gateway.image_reads == KM_IMAGES


def test_2_a_queued_job_whose_buffer_is_gone_fails_and_is_never_retried(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    # The process that accepted the capture stops before its job runs.
    with _process(config, clock, gateway) as first:
        run_id = _accept(first)
        assert _job(config, run_id)[1] == "QUEUED"
    # A new process: the same durable job, and an empty buffer.
    with _process(config, clock, gateway) as second:
        assert len(second.extension_capture._buffer) == 0
        second.runner.recover_interrupted()
        result = second.runner.run_next()
        assert result is not None and result.state is JobState.DEAD
        assert result.error_code == EXTENSION_CAPTURE_BUFFER_MISSING
        run = second.collection.run(run_id)
        assert (run.outcome, run.detail) == (
            CollectionOutcome.FAILED,
            EXTENSION_CAPTURE_BUFFER_MISSING,
        )
        assert run.revision_id is None
        # One attempt, terminal, and nothing schedules another.
        assert _job(config, run_id)[1:5] == ("DEAD", 1, 1, EXTENSION_CAPTURE_BUFFER_MISSING)
        clock.advance(3600)
        assert second.runner.run_next() is None
        assert second.runner.reconcile_terminal_owners() == 0
        assert count(config, "collection_runs") == 1
        assert count(config, "product_facts_revisions") == 0
        assert gateway.image_reads == []


def test_3_a_running_interruption_is_never_replayed(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _process(config, clock, gateway) as first:
        run_id = _accept(first)
        # The worker claims the job, and the process stops mid-attempt.
        assert first.runner._claim() is not None
        assert _job(config, run_id)[1] == "RUNNING"
    calls: list[str] = []
    monkeypatch.setattr(
        ExtensionCaptureService,
        "_document",
        lambda self, record, capture: calls.append(record.collection_run_id),
    )
    with _process(config, clock, gateway) as second:
        # The job definition says so itself: it is not replayable.
        definition = second.job_registry.get(EXTENSION_CAPTURE_JOB)
        assert definition.idempotent is False
        assert (EXTENSION_CAPTURE_POLICY.max_attempts, definition.retry_policy) == (
            1,
            EXTENSION_CAPTURE_POLICY,
        )
        assert second.runner.recover_interrupted() == 1
        assert _job(config, run_id)[1:5] == ("DEAD", 1, 1, "INTERRUPTED_OUTCOME_UNKNOWN")
        second.runner.reconcile_terminal_owners()
        run = second.collection.run(run_id)
        assert run.outcome is CollectionOutcome.FAILED
        assert run.detail == "JOB_ENDED_WITHOUT_RESULT:INTERRUPTED_OUTCOME_UNKNOWN"
        # Nothing ran the capture again: no attempt, no document, no revision, no image request.
        clock.advance(3600)
        assert second.runner.run_next() is None
        assert calls == []
        assert count(config, "product_facts_revisions") == 0
        assert gateway.image_reads == []


def test_3b_an_attempt_that_appended_and_died_is_finished_from_its_revision(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The revision is immutable and is already the answer. An attempt that appended it and died
    # before settling the run is finished from it: nothing is captured, fetched or appended again.
    def dies(self: ProductCollectionService, run_id: str, result: Any) -> None:
        raise KeyboardInterrupt("the process stops after the append, before the run is settled")

    with _process(config, clock, gateway) as first:
        run_id = _accept(first)
        monkeypatch.setattr(ProductCollectionService, "settle", dies)
        with pytest.raises(KeyboardInterrupt):
            first.runner.run_next()
        assert first.collection.run(run_id).outcome is CollectionOutcome.PENDING
        assert count(config, "product_facts_revisions") == 1
    monkeypatch.undo()
    fetched = list(gateway.image_reads)
    with _process(config, clock, gateway) as second:
        second.runner.recover_interrupted()
        second.runner.reconcile_terminal_owners()
        run = second.collection.run(run_id)
        assert run.outcome is CollectionOutcome.RECORDED and run.revision_id is not None
        assert count(config, "product_facts_revisions") == 1
        assert gateway.image_reads == fetched
        assert second.runner.run_next() is None


def test_4_the_capture_never_reaches_a_job_payload_or_a_log(
    client: TestClient, config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    app: Container = client.app.state.container  # type: ignore[attr-defined]
    record = pair(app)
    marked = frame(body=BODY + f'<div id="extra"><p>{MARKER}</p></div>')
    with caplog.at_level(logging.DEBUG):
        response = post_capture(client, record, envelope(marked))
        assert response.status_code == 202
        run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert run["outcome"] == "RECORDED"
    # The durable job payload holds identifiers and provenance only.
    payload = json.loads(_job(config, run["collection_run_id"])[5])
    assert set(payload) == {
        "supplier_key",
        "source_url",
        "capture_policy_revision",
        "capture_policy_digest",
    }
    # The capture itself is handed over in memory. Text the extractor does not read as a fact is
    # in no log record ...
    for entry in caplog.records:
        assert MARKER not in entry.getMessage() and MARKER not in json.dumps(
            {k: repr(v) for k, v in vars(entry).items()}, ensure_ascii=False
        )
    # ... and in no byte of the data directory. What the revision holds is facts and their quoted
    # evidence, through the canonical stores, never the captured document.
    needles = [MARKER.encode("utf-8"), MARKER.encode("utf-16-le"), b"<!doctype html>"]
    files = [path for path in Path(config.data_dir).rglob("*") if path.is_file()]
    assert any(path.name == "icbm.db" for path in files)
    assert any(path.suffix == ".jsonl" for path in files), "the log file is part of the scan"
    for path in files:
        content = path.read_bytes()
        for needle in needles:
            assert needle not in content, (path.name, needle[:12])
    assert app.secrets.get("collect-extension-pairing") is not None


def test_5_the_handoff_depends_on_the_in_process_worker(container: Container) -> None:
    # ADR-0002 Option A is what makes an in-memory handoff possible at all. An ingest owner told
    # that the worker is not in this process accepts nothing.
    service = container.extension_capture
    elsewhere = ExtensionCaptureService(
        db=container.db,
        clock=container.clock,
        jobs=container.jobs,
        runs=service._runs,
        policies=service._policies,
        buffer=service._buffer,
        final_scan=service._final_scan,
        worker_in_process=False,
        recorder=container.collection,
        collections=tuple(service._collections.values()),
    )
    with pytest.raises(AppError) as refused:
        elsewhere.ingest(CaptureEnvelope.model_validate(envelope()))
    assert refused.value.code == "EXTENSION_WORKER_NOT_IN_PROCESS"
    assert container.collection.recent_runs() == ()
    assert service._worker_in_process is container.worker.IN_PROCESS is True


# ---------------------------------------------------------------- ceilings across time


def test_one_capture_at_a_time_then_the_minimum_interval(
    container: Container, clock: FakeClock
) -> None:
    first = _accept(container)
    # While the first capture's run is PENDING, nothing else is accepted.
    with pytest.raises(AppError) as busy:
        _accept(container)
    assert busy.value.code == "EXTENSION_CEILING_CONCURRENCY"
    assert container.runner.run_next() is not None
    assert container.collection.run(first).outcome is CollectionOutcome.RECORDED
    # Settled, but too recent.
    clock.advance(MINIMUM_INGEST_INTERVAL_S - 1)
    with pytest.raises(AppError) as recent:
        _accept(container)
    assert recent.value.code == "EXTENSION_CEILING_INTERVAL"
    clock.advance(1)
    second = _accept(container)
    assert second != first and len(container.collection.recent_runs()) == 2


def test_the_ceilings_are_read_from_the_canonical_runs_and_survive_a_restart(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    with _process(config, clock, gateway) as first:
        _accept(first)
    with _process(config, clock, gateway) as second:
        with pytest.raises(AppError) as busy:
            _accept(second)
        assert busy.value.code == "EXTENSION_CEILING_CONCURRENCY"


def test_a_policy_revised_after_acceptance_fails_the_run(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, tmp_path: Path
) -> None:
    # The capture was cut under one policy; the run never proceeds under another.
    root = _policy_root(tmp_path, json.loads(_km_policy_text()))
    with _process(config, clock, gateway, capture_policy_root=root) as app:
        run_id = _accept(app, policy=_reference(root))
        revised = json.loads(_km_policy_text())
        revised["revision"] = "kmretail-capture-next"
        (root / "kmretail" / POLICY_FILE).write_text(json.dumps(revised), "utf-8")
        result = app.runner.run_next()
        assert result is not None and result.error_code == "EXTENSION_CAPTURE_POLICY_CHANGED"
        run = app.collection.run(run_id)
        assert (run.outcome, run.detail) == (
            CollectionOutcome.FAILED,
            "EXTENSION_CAPTURE_POLICY_CHANGED",
        )
        assert count(config, "product_facts_revisions") == 0 and gateway.image_reads == []


# ---------------------------------------------------------------- the revision and the report


def test_the_recorded_revision_is_the_canonical_extractors_with_extension_provenance(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    reports: list[CaptureReport] = []
    with _process(config, clock, gateway, extension_report_sink=reports.append) as app:
        run_id = _accept(app)
        assert app.runner.run_next() is not None
        run = app.collection.run(run_id)
        assert run.outcome is CollectionOutcome.RECORDED and run.revision_id is not None
        revision = app.revisions.get(run.revision_id)
        assert revision is not None
        # The canonical KM extractor wrote it, from the captured document.
        assert (revision.supplier_key, revision.source_product_id) == ("kmretail", "9001")
        assert revision.collection_run_id == run_id
        statuses = {key: fact.status.value for key, fact in revision.fields.items()}
        assert statuses["original_name"] == "CONFIRMED"
        assert statuses["prices"] == "CONFIRMED"
        assert statuses["stock"] == "CONFIRMED"
        assert statuses["shipping"] == "CONFIRMED"
        assert statuses["minimum_sale_price"] == "ABSENT"
        # The provenance of the run is the revision's, and is no part of its identity.
        reference = envelope()["policy"]
        assert (
            revision.transport_kind,
            revision.capture_policy_revision,
            revision.capture_policy_digest,
        ) == ("EXTENSION", reference["revision"], reference["digest"])
        # The images are the server's own policed fetch: its checksum, its source assets.
        assert [image.role.value for image in revision.images] == ["REPRESENTATIVE", "DETAIL"]
        assert all(image.sha256 for image in revision.images)
        assert count(config, "source_assets") >= 1
        assert gateway.document_reads == 0 and gateway.image_reads == KM_IMAGES
        # The run reserved no server product read; its decisions are frozen like any run's.
        assert run.product_read_at is None
        assert run.frozen is not None and run.frozen.shadow.decision == "DISABLED"
        assert run.frozen.capture is not None and run.frozen.capture.decision == "OFF"
        (report,) = reports
        assert report.collection_run_id == run_id
        assert (report.outcome, report.revision_id, report.facts_status) == (
            CollectionOutcome.RECORDED,
            run.revision_id,
            revision.facts_status,
        )
        assert isinstance(report.facts_status, FactsStatus)
        assert report.transport_kind is TransportKind.EXTENSION
        assert (report.capture_policy_revision, report.capture_policy_digest) == (
            reference["revision"],
            reference["digest"],
        )
        assert report.evidence == {
            "response_status": 200,
            "redirect_count": 0,
            "content_type": "text/html",
            "character_set": "UTF-8",
        }
        # No captured value is part of the report.
        rendered = repr(report)
        assert "합성 샘플 상품" not in rendered and "12,000" not in rendered


def test_a_second_capture_of_the_same_product_appends_a_second_revision(
    container: Container, clock: FakeClock, config: AppConfig
) -> None:
    # Revisions are append-only: a later capture never rewrites an earlier revision.
    first = _accept(container)
    assert container.runner.run_next() is not None
    clock.advance(MINIMUM_INGEST_INTERVAL_S)
    second = _accept(container)
    assert container.runner.run_next() is not None
    one, two = container.collection.run(first), container.collection.run(second)
    assert one.revision_id and two.revision_id and one.revision_id != two.revision_id
    assert rows(config, "SELECT sequence FROM product_facts_revisions ORDER BY sequence") == [
        (1,),
        (2,),
    ]


def test_an_image_the_provider_refuses_is_kept_as_an_unfetched_reference(
    config: AppConfig, clock: FakeClock
) -> None:
    # Exactly as on the direct path: one image that could not be turned into bytes is not a
    # failed collection. The revision keeps the reference and says so.
    gateway = FakeGateway(documents=[], images={KM_IMAGES[1]: fake_shop.refusal()})
    with _process(config, clock, gateway) as app:
        run_id = _accept(app)
        assert app.runner.run_next() is not None
        run = app.collection.run(run_id)
        assert run.outcome is CollectionOutcome.RECORDED and run.revision_id is not None
        revision = app.revisions.get(run.revision_id)
        assert revision is not None
        fetched = {image.role.value: image.sha256 for image in revision.images}
        assert fetched["REPRESENTATIVE"] is not None and fetched["DETAIL"] is None


def test_a_failing_report_sink_never_reaches_the_run(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway
) -> None:
    def broken(report: CaptureReport) -> None:
        raise RuntimeError("the sink is broken")

    with _process(config, clock, gateway, extension_report_sink=broken) as app:
        run_id = _accept(app)
        result = app.runner.run_next()
        assert result is not None and result.state is JobState.SUCCEEDED
        assert app.collection.run(run_id).outcome is CollectionOutcome.RECORDED


# ---------------------------------------------------------------- the shadow, on an extension run

FAKE_URL = fake_shop.PRODUCT_URL
FAKE_POLICY: dict[str, Any] = {
    "schema": "icbm-browser-capture-policy/v1",
    "supplier_key": fake_shop.SUPPLIER_KEY,
    "revision": "fakeshop-capture-1",
    "host": fake_shop.HOST,
    "product_root": {"by": "class", "token": "goods"},
    "allowed_regions": [],
    "excluded_regions": [],
    "excluded_tags": ["script", "style"],
    "allowed_attributes": {"*": ["id", "class"], "img": ["src"], "meta": ["property", "content"]},
    "head_allowance": [{"tag": "meta", "attribute": "property", "value": "product:id"}],
    "bounds": {"max_html_bytes": 65536, "max_nodes": 500, "max_image_refs": 10},
}
FAKE_CAPTURE = frame(
    head='<meta property="product:id" content="4242">',
    body=f'<div class="goods"><img id="primary" src="{fake_shop.PRIMARY_URL}"></div>',
)


def _fake_envelope(root: Path) -> dict[str, Any]:
    url = {"url": FAKE_URL, "navigation_name": FAKE_URL}
    return envelope(
        FAKE_CAPTURE,
        supplier_key=fake_shop.SUPPLIER_KEY,
        policy=_reference(root, fake_shop.SUPPLIER_KEY),
        transport={**envelope()["transport"], **url},
    )


def test_without_an_enabled_bundle_an_extension_run_has_no_shadow(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, tmp_path: Path
) -> None:
    root = _policy_root(tmp_path, FAKE_POLICY)
    with _fake_process(config, clock, gateway, root) as app:
        accepted = app.extension_capture.ingest(
            CaptureEnvelope.model_validate(_fake_envelope(root))
        )
        assert app.runner.run_next() is not None
        run = app.collection.run(accepted.collection_run_id)
        assert run.outcome is CollectionOutcome.RECORDED
        assert run.frozen is not None and run.frozen.shadow.decision == "DISABLED"
        assert count(config, "adaptive_shadow_records") == 0


def test_an_enabled_bundle_shadows_an_extension_run_like_a_direct_one(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, tmp_path: Path
) -> None:
    # One pipeline after capture (ADR-0019 §2): the shadow runs for an extension run exactly as
    # for a direct one — frozen at the run's start, after the canonical write, never canonical.
    root = _policy_root(tmp_path, FAKE_POLICY)
    with _fake_process(config, clock, gateway, root) as app:
        _, bundle_key = enabled_profile(app)
        accepted = app.extension_capture.ingest(
            CaptureEnvelope.model_validate(_fake_envelope(root))
        )
        result = app.runner.run_next()
        assert result is not None and result.state is JobState.SUCCEEDED
        run = app.collection.run(accepted.collection_run_id)
        # The canonical extractor is still the only revision writer.
        assert run.outcome is CollectionOutcome.RECORDED and run.revision_id is not None
        assert run.frozen is not None
        assert (run.frozen.shadow.decision, run.frozen.shadow.bundle_key) == ("ENABLED", bundle_key)
        assert rows(
            config,
            "SELECT collection_run_id FROM adaptive_shadow_records",
        ) == [(accepted.collection_run_id,)]
        states = rows(config, "SELECT DISTINCT to_state FROM adaptive_profile_transitions")
        assert ("ACTIVE",) not in states
        assert gateway.document_reads == 0


def test_an_unclassified_failure_never_carries_its_own_text(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The extractor holds captured page content, so whatever it raises is reduced to a fixed code
    # and its type before it reaches the run, the job's stored error or a log.
    secret_text = "synthetic page text 010-0000-0000"

    def leak(*_: Any, **__: Any) -> Any:
        raise ValueError(secret_text)

    with _process(config, clock, gateway) as app:
        run_id = _accept(app)
        known = app.collection._collections["kmretail"]
        monkeypatch.setitem(
            app.collection._collections,
            "kmretail",
            replace(known, collection=replace(known.collection, fields=leak)),
        )
        result = app.runner.run_next()
        assert result is not None and result.state is JobState.DEAD
        assert result.error_code == "EXTENSION_PROCESSING_FAILED"
        run = app.collection.run(run_id)
        assert (run.outcome, run.detail) == (
            CollectionOutcome.FAILED,
            "EXTENSION_PROCESSING_FAILED",
        )
        assert _job(config, run_id)[1] == "DEAD"
        assert count(config, "product_facts_revisions") == 0
    for path in Path(config.data_dir).rglob("*"):
        if path.is_file():
            assert secret_text.encode() not in path.read_bytes(), path.name


def test_only_an_extension_run_records_a_captured_document(
    container: Container, config: AppConfig
) -> None:
    # The collection owner's captured-document entry is for a run the extension opened. A direct
    # run is read by the server's own gateway and never takes a document from anywhere else.
    submitted = container.collection.submit(
        "kmretail", "https://kmretail.co.kr/product/synthetic-sample/9001/"
    )
    record = container.collection.run(submitted.collection_run_id)
    with pytest.raises(AppError) as refused:
        container.collection.record_captured_document(
            record,
            object(),  # type: ignore[arg-type]
            captured_at=container.clock.now(),
        )
    assert refused.value.code == "COLLECT_RUN_NOT_EXTENSION"
    assert count(config, "product_facts_revisions") == 0


# ---------------------------------------------------------------- helpers


def _km_policy_text() -> str:
    from tests.support.extension_support import KM_POLICY

    return KM_POLICY.read_text("utf-8")


def _policy_root(tmp_path: Path, document: dict[str, Any]) -> Path:
    root = tmp_path / "suppliers"
    package = root / document["supplier_key"]
    package.mkdir(parents=True)
    (package / POLICY_FILE).write_text(json.dumps(document, ensure_ascii=False), "utf-8")
    return root


def _reference(root: Path, supplier_key: str = "kmretail") -> dict[str, str]:
    from app.stages.collect.extension.policy import CapturePolicySource

    policy = CapturePolicySource(root).load(supplier_key)
    return {"revision": policy.revision, "digest": policy.digest}


@contextlib.contextmanager
def _process(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, **wiring: Any
) -> Iterator[Container]:
    """One application process over the data directory; each entry is a fresh process, with a
    fresh buffer and replay cache, the same keyring and the same scripted gateway."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=TEST_JOBS,
            collection_gateway=gateway,
            collection_sessions=StubSessions(),
            **wiring,
        )
        try:
            yield built
        finally:
            built.db.dispose()


@contextlib.contextmanager
def _fake_process(
    config: AppConfig, clock: FakeClock, gateway: FakeGateway, root: Path
) -> Iterator[Container]:
    with _process(
        config,
        clock,
        gateway,
        collections=(registered(),),
        adaptive_supplier_gate=build_supplier_gate({fake_shop.SUPPLIER_KEY}),
        capture_policy_root=root,
    ) as app:
        yield app
