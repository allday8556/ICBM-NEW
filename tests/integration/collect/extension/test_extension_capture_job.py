"""The extension capture job and its in-process buffer (ruling 5906712259 N-1), the Adaptive dry
run (ruling 5906290729 B-7) and what a processed capture reports (E1 specification §2.3).

These tests run the owners directly, with a clock they control and no worker: each job attempt
happens exactly when the test runs it, so a restart and an interruption can be staged.
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
from app.stages.collect.adaptive.shadow import dry_run as dry_run_module
from app.stages.collect.adaptive.store.gate import build_supplier_gate
from app.stages.collect.extension.capture import CaptureEnvelope
from app.stages.collect.extension.policy import POLICY_FILE
from app.stages.collect.extension.service import (
    EXTENSION_ADAPTIVE_COMPARE_FAILED,
    EXTENSION_CAPTURE_BUFFER_MISSING,
    EXTENSION_CAPTURE_JOB,
    EXTENSION_CAPTURE_POLICY,
    EXTENSION_COMPARE_ONLY,
    MINIMUM_INGEST_INTERVAL_S,
    CaptureReport,
    ExtensionCaptureService,
)
from app.stages.collect.models import CollectionOutcome, TransportKind
from automation.acceptance.m3.rehearsal import fake_shop
from tests.support.extension_support import (
    BODY,
    envelope,
    frame,
    pair,
    post_capture,
    table_counts,
    untouched,
    wait_for_outcome,
)
from tests.support.jobs_support import TEST_JOBS, FakeClock
from tests.support.shadow_support import count, enabled_profile, registered, rows

pytestmark = pytest.mark.integration

MARKER = "합성-캡처-표식-7f3a"


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


def test_1_an_accepted_job_with_its_buffer_processes_normally(
    container: Container, config: AppConfig
) -> None:
    run_id = _accept(container)
    assert container.collection.run(run_id).outcome is CollectionOutcome.PENDING
    assert len(container.extension_capture._buffer) == 1
    result = container.runner.run_next()
    assert result is not None and result.state is JobState.SUCCEEDED
    run = container.collection.run(run_id)
    assert (run.outcome, run.detail) == (CollectionOutcome.NO_REVISION, EXTENSION_COMPARE_ONLY)
    assert run.provenance is not None
    assert run.provenance.transport_kind is TransportKind.EXTENSION
    assert _job(config, run_id)[:4] == (EXTENSION_CAPTURE_JOB, "SUCCEEDED", 1, 1)
    # The capture was handed over once and is held nowhere afterwards.
    assert len(container.extension_capture._buffer) == 0
    assert container.runner.run_next() is None


def test_2_a_queued_job_whose_buffer_is_gone_fails_and_is_never_retried(
    config: AppConfig, clock: FakeClock
) -> None:
    # The process that accepted the capture stops before its job runs.
    with _process(config, clock) as first:
        run_id = _accept(first)
        assert _job(config, run_id)[1] == "QUEUED"
    # A new process: the same durable job, and an empty buffer.
    with _process(config, clock) as second:
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


def test_3_a_running_interruption_is_never_replayed(
    config: AppConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _process(config, clock) as first:
        run_id = _accept(first)
        # The worker claims the job, and the process stops mid-attempt.
        assert first.runner._claim() is not None
        assert _job(config, run_id)[1] == "RUNNING"
    calls: list[str] = []
    monkeypatch.setattr(
        ExtensionCaptureService,
        "_compare",
        lambda self, record, capture: calls.append(record.collection_run_id),
    )
    with _process(config, clock) as second:
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
        # Nothing ran the capture again: no attempt, no comparison, no revision.
        clock.advance(3600)
        assert second.runner.run_next() is None
        assert calls == []
        assert count(config, "product_facts_revisions") == 0


def test_4_the_capture_never_reaches_a_durable_store_or_a_log(
    client: TestClient, config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    app: Container = client.app.state.container  # type: ignore[attr-defined]
    record = pair(app)
    marked = frame(body=BODY + f'<div id="extra"><p>{MARKER}</p></div>')
    with caplog.at_level(logging.DEBUG):
        response = post_capture(client, record, envelope(marked))
        assert response.status_code == 202
        run = wait_for_outcome(client, response.json()["collection_run_id"])
    assert run["outcome"] == "NO_REVISION"
    # The durable job payload holds identifiers and provenance only.
    payload = json.loads(_job(config, run["collection_run_id"])[5])
    assert set(payload) == {
        "supplier_key",
        "source_url",
        "capture_policy_revision",
        "capture_policy_digest",
    }
    # Not in any log record ...
    for entry in caplog.records:
        assert MARKER not in entry.getMessage() and MARKER not in json.dumps(
            {k: repr(v) for k, v in vars(entry).items()}, ensure_ascii=False
        )
    # ... and not in any byte of the data directory: the database, its journal, the log file,
    # or any other file the application wrote.
    needles = [MARKER.encode("utf-8"), MARKER.encode("utf-16-le"), b"xans-product-detail"]
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
    assert container.collection.run(first).outcome is CollectionOutcome.NO_REVISION
    # Settled, but too recent.
    clock.advance(MINIMUM_INGEST_INTERVAL_S - 1)
    with pytest.raises(AppError) as recent:
        _accept(container)
    assert recent.value.code == "EXTENSION_CEILING_INTERVAL"
    clock.advance(1)
    second = _accept(container)
    assert second != first and len(container.collection.recent_runs()) == 2


def test_the_ceilings_are_read_from_the_canonical_runs_and_survive_a_restart(
    config: AppConfig, clock: FakeClock
) -> None:
    with _process(config, clock) as first:
        _accept(first)
    with _process(config, clock) as second:
        with pytest.raises(AppError) as busy:
            _accept(second)
        assert busy.value.code == "EXTENSION_CEILING_CONCURRENCY"


def test_a_policy_revised_after_acceptance_fails_the_run(
    config: AppConfig, clock: FakeClock, tmp_path: Path
) -> None:
    # The capture was cut under one policy; the run never proceeds under another.
    root = _policy_root(tmp_path, json.loads(_km_policy_text()))
    with _process(config, clock, capture_policy_root=root) as app:
        run_id = _accept(app, policy=_reference(root))
        revised = json.loads(_km_policy_text())
        revised["revision"] = "kmretail-capture-2"
        (root / "kmretail" / POLICY_FILE).write_text(json.dumps(revised), "utf-8")
        result = app.runner.run_next()
        assert result is not None and result.error_code == "EXTENSION_CAPTURE_POLICY_CHANGED"
        run = app.collection.run(run_id)
        assert (run.outcome, run.detail) == (
            CollectionOutcome.FAILED,
            "EXTENSION_CAPTURE_POLICY_CHANGED",
        )


# ---------------------------------------------------------------- the report and NO_BUNDLE


def test_the_report_of_a_no_bundle_run_is_the_acceptance_artifact(
    config: AppConfig, clock: FakeClock
) -> None:
    reports: list[CaptureReport] = []
    with _process(config, clock, extension_report_sink=reports.append) as app:
        before = table_counts(config)
        run_id = _accept(app)
        assert app.runner.run_next() is not None
        (report,) = reports
        # E1 specification §2.3: the run, NO_BUNDLE, the canonical extractor result, and what
        # proves zero write and the capture's own security evidence.
        assert report.collection_run_id == run_id
        assert (report.outcome, report.detail) == (
            CollectionOutcome.NO_REVISION,
            EXTENSION_COMPARE_ONLY,
        )
        assert report.transport_kind is TransportKind.EXTENSION
        assert (report.capture_policy_revision, report.capture_policy_digest) == (
            envelope()["policy"]["revision"],
            envelope()["policy"]["digest"],
        )
        assert report.evidence == {
            "response_status": 200,
            "redirect_count": 0,
            "content_type": "text/html",
            "character_set": "UTF-8",
        }
        assert report.source_product_id == "9001" and report.identity_reason is None
        # The canonical KM extractor read the capture: every registered field has a status.
        assert report.fields["original_name"] == "CONFIRMED"
        assert report.fields["prices"] == "CONFIRMED"
        assert report.fields["stock"] == "CONFIRMED"
        assert report.fields["shipping"] == "CONFIRMED"
        assert report.fields["minimum_sale_price"] == "ABSENT"
        assert report.image_roles == {"DETAIL": 1, "PRIMARY": 1}
        # NO_BUNDLE, and never worded as anything else.
        assert (report.adaptive.state, report.adaptive.bundle_key) == ("NO_BUNDLE", None)
        assert dict(report.adaptive.summary) == {}
        rendered = repr(report)
        for word in ("PASS", "VALIDATED", "SHADOW", "ACTIVE", "MATCH"):
            assert word not in rendered, word
        # No captured value is part of the report.
        assert "합성 샘플 상품" not in rendered and "12,000" not in rendered
        assert untouched(before, table_counts(config)) == {}


def test_a_failing_report_sink_never_reaches_the_run(config: AppConfig, clock: FakeClock) -> None:
    def broken(report: CaptureReport) -> None:
        raise RuntimeError("the sink is broken")

    with _process(config, clock, extension_report_sink=broken) as app:
        run_id = _accept(app)
        result = app.runner.run_next()
        assert result is not None and result.state is JobState.SUCCEEDED
        assert app.collection.run(run_id).outcome is CollectionOutcome.NO_REVISION


# ---------------------------------------------------------------- the dry run with a bundle

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


def test_without_an_enabled_bundle_the_dry_run_answers_no_bundle(
    config: AppConfig, clock: FakeClock, tmp_path: Path
) -> None:
    reports: list[CaptureReport] = []
    root = _policy_root(tmp_path, FAKE_POLICY)
    with _fake_process(config, clock, root, reports.append) as app:
        app.extension_capture.ingest(CaptureEnvelope.model_validate(_fake_envelope(root)))
        assert app.runner.run_next() is not None
        (report,) = reports
        assert report.adaptive.state == "NO_BUNDLE"
        assert count(config, "adaptive_shadow_records") == 0


def test_an_enabled_bundle_is_compared_in_memory_and_nothing_is_written(
    config: AppConfig, clock: FakeClock, tmp_path: Path
) -> None:
    reports: list[CaptureReport] = []
    root = _policy_root(tmp_path, FAKE_POLICY)
    with _fake_process(config, clock, root, reports.append) as app:
        _, bundle_key = enabled_profile(app)
        before = table_counts(config)
        accepted = app.extension_capture.ingest(
            CaptureEnvelope.model_validate(_fake_envelope(root))
        )
        result = app.runner.run_next()
        assert result is not None and result.state is JobState.SUCCEEDED
        (report,) = reports
        # The reviewed, enabled bundle ran in memory beside the canonical extractor.
        assert (report.adaptive.state, report.adaptive.bundle_key) == ("COMPARED", bundle_key)
        summary = dict(report.adaptive.summary)
        assert set(summary) == {
            "verdict",
            "severity",
            "template",
            "facts_status",
            "field_verdicts",
            "image_outcomes",
            "canonical_images_fetched",
        }
        assert summary["canonical_images_fetched"] is False
        # The run is still compare only: NO_REVISION, never RECORDED, and never ACTIVE.
        run = app.collection.run(accepted.collection_run_id)
        assert (run.outcome, run.detail) == (CollectionOutcome.NO_REVISION, EXTENSION_COMPARE_ONLY)
        # The dry run wrote nothing: no shadow record, window, ledger event or profile transition,
        # and no canonical row. The run froze no shadow decision, so it is never shadow-eligible.
        assert untouched(before, table_counts(config)) == {}
        assert count(config, "adaptive_shadow_records") == 0
        assert run.frozen is None
        states = rows(config, "SELECT DISTINCT to_state FROM adaptive_profile_transitions")
        assert ("ACTIVE",) not in states


def _boom(*_: Any, **__: Any) -> Any:
    raise RuntimeError("synthetic evaluation failure")


def test_an_unclassified_failure_never_carries_its_own_text(
    config: AppConfig, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The extractor holds captured page content, so whatever it raises is reduced to a fixed code
    # and its type before it reaches the run, the job's stored error or a log.
    secret_text = "synthetic page text 010-0000-0000"

    def leak(*_: Any, **__: Any) -> Any:
        raise ValueError(secret_text)

    with _process(config, clock) as app:
        run_id = _accept(app)
        registered = app.extension_capture._collections["kmretail"]
        monkeypatch.setitem(
            app.extension_capture._collections,
            "kmretail",
            replace(registered, collection=replace(registered.collection, fields=leak)),
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
    for path in Path(config.data_dir).rglob("*"):
        if path.is_file():
            assert secret_text.encode() not in path.read_bytes(), path.name


@pytest.mark.parametrize("where", ["inside-the-comparer", "escaped-from-the-step"])
def test_a_dry_run_that_cannot_compare_fails_the_run(
    config: AppConfig,
    clock: FakeClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    # GPT audit 5365019650 B-2; E1 specification §3: step 12 is the Adaptive dry run, and any
    # failure from step 9 on settles the run FAILED. NO_BUNDLE and a comparison are the only
    # answers of a successful run.
    reports: list[CaptureReport] = []
    root = _policy_root(tmp_path, FAKE_POLICY)
    with _fake_process(config, clock, root, reports.append) as app:
        enabled_profile(app)
        if where == "inside-the-comparer":
            monkeypatch.setattr(dry_run_module, "extract", _boom)
        else:
            monkeypatch.setattr(app.extension_capture, "_dry_run", _boom)
        before = table_counts(config)
        accepted = app.extension_capture.ingest(
            CaptureEnvelope.model_validate(_fake_envelope(root))
        )
        result = app.runner.run_next()
        assert result is not None and result.state is JobState.DEAD
        assert result.error_code == EXTENSION_ADAPTIVE_COMPARE_FAILED
        run = app.collection.run(accepted.collection_run_id)
        assert (run.outcome, run.detail) == (
            CollectionOutcome.FAILED,
            EXTENSION_ADAPTIVE_COMPARE_FAILED,
        )
        # No report of a successful comparison exists, and nothing was written or retried.
        assert reports == []
        assert untouched(before, table_counts(config)) == {}
        assert count(config, "adaptive_shadow_records") == 0
        assert app.runner.run_next() is None


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
def _process(config: AppConfig, clock: FakeClock, **wiring: Any) -> Iterator[Container]:
    """One application process over the data directory; each entry is a fresh process, with a
    fresh buffer and replay cache, and the same keyring."""
    with acquire_data_dir(config.data_dir, app_version="test") as lease:
        built = build_container(
            config,
            ownership=lease,
            clock=clock,
            secret_store=MemorySecretStore(),
            extra_jobs=TEST_JOBS,
            **wiring,
        )
        try:
            yield built
        finally:
            built.db.dispose()


@contextlib.contextmanager
def _fake_process(
    config: AppConfig, clock: FakeClock, root: Path, sink: Any
) -> Iterator[Container]:
    with _process(
        config,
        clock,
        collections=(registered(),),
        adaptive_supplier_gate=build_supplier_gate({fake_shop.SUPPLIER_KEY}),
        capture_policy_root=root,
        extension_report_sink=sink,
    ) as app:
        yield app
