"""The Adaptive shadow foundation over the real COLLECT path (ADR-0017 §10–§11; Issue #110 P3).

Every test drives the real container — the durable job, the run store, the revision store and the
shadow owner — against the fake shop and a scripted transport. What this proves:

- the switch is off for every supplier until an explicit entry, only a currently VALIDATED EPR is
  enabled, and only the switch moves an EPR into or out of ``SHADOW`` (B1 of P2, closed here);
- the first product-read reservation freezes the switch entry and the exact bundle in its own
  canonical unit; a retry keeps them, and a later switch change alters neither that run's shadow
  execution nor its eligibility (carry-forward item 2);
- the shadow runs after the canonical commit in its own unit, with zero extra requests, and leaves
  the canonical state identical to a run with the shadow off (S1, S2); its failures never reach
  the run (S3), and a process killed between the two counts ``SHADOW_MISSING_AFTER_RECOVERY`` with
  no refetch (§11.2);
- raw retention, the ledger fold, windows, on-demand reconciliation before ``ENDED`` / ``CLOSED``
  (item 3) and the exact-bundle rule (item 1) behave as ADR-0017 and `5824551569` say.

No supplier is read, no network is reached, and no real Phase C window is opened: every window
here is synthetic, over the fake shop.
"""

import contextlib
import shutil
import socket
import sqlite3
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command

import app.collect.adaptive_shadow.runner as runner_module
from app.collect.adaptive.validation import freshness_tuple
from app.collect.adaptive_shadow.compare import Comparison
from app.collect.adaptive_shadow.evidence import (
    BundleVerdict,
    Cause,
    CountAs,
    EventKind,
    Resolution,
    RunVerdict,
    State,
    WindowVerdict,
)
from app.collect.adaptive_shadow.store import (
    ADAPTIVE_BUNDLE_BLOCKED,
    ADAPTIVE_RESOLUTION_REFUSED,
    ADAPTIVE_RETENTION_INVALID,
    ADAPTIVE_WINDOW_REFUSED,
    RawRetention,
    ShadowEvidenceStore,
    ShadowEvidenceTampered,
)
from app.collect.adaptive_shadow.switch import ADAPTIVE_SHADOW_REFUSED, bundle_key_of
from app.collect.adaptive_store.gate import build_supplier_gate
from app.collect.adaptive_store.store import ADAPTIVE_LIFECYCLE_REFUSED
from app.collect.models import CollectionOutcome
from app.config import AppConfig, database_path
from app.container import Container
from app.core.errors import AppError, InputValidationError, TransientError
from app.db.database import create_sqlite_engine
from app.db.migrate import alembic_config, current_revision, upgrade_to_head
from app.jobs.models import JobState
from integrations.suppliers.collection import CollectionProfile, DocumentView, ReadKind
from integrations.suppliers.transport.collection import RequestBudget
from scripts.m3collect.fake_shop import PRODUCT_URL, SUPPLIER_KEY, FakeGateway
from tests.adaptive_support import epr, template
from tests.conftest import make_config
from tests.shadow_support import (
    CORRELATION,
    INTERVAL,
    OPERATOR,
    canonical_snapshot,
    collect_once,
    comparison_json,
    container,
    count,
    enabled_profile,
    gateway,
    job_context,
    raw,
    rows,
    save_profile,
    validate,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

SHADOW_TABLES = (
    "adaptive_shadow_switch_entries",
    "adaptive_shadow_records",
    "adaptive_evidence_windows",
    "adaptive_evidence_window_events",
    "adaptive_shadow_ledger_events",
)


class NetworkRefused(AssertionError):
    pass


class ProcessKilled(BaseException):
    """What a killed process looks like to the code that was running: nothing after it runs."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkRefused("the shadow owner makes no network call")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


@pytest.fixture
def shop() -> FakeGateway:
    return gateway()


@pytest.fixture
def engine_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every Adaptive engine call the shadow step makes, counted."""
    calls: list[str] = []
    real = runner_module.extract

    def counted(*args: Any, **kwargs: Any) -> Any:
        calls.append("extract")
        return real(*args, **kwargs)

    monkeypatch.setattr(runner_module, "extract", counted)
    return calls


def script(monkeypatch: pytest.MonkeyPatch, *verdicts: RunVerdict) -> None:
    """Make the comparison of successive shadow runs answer these verdicts, in order."""
    queue = list(verdicts)

    def scripted(**_: Any) -> Comparison:
        verdict = queue.pop(0) if len(queue) > 1 else queue[0]
        return Comparison(
            verdict,
            None,
            {"canonical": "RESOLVED", "adaptive": "RESOLVED", "agrees": True},
            {"outcome": "MATCHED", "matched": True},
        )

    monkeypatch.setattr(runner_module, "compare", scripted)


def declare(app: Container, bundle_key: str, **kwargs: Any) -> str:
    return app.shadow_evidence.declare(
        SUPPLIER_KEY,
        bundle_key,
        actor=OPERATOR,
        reason="SYNTHETIC_WINDOW",
        correlation_id=CORRELATION,
        **kwargs,
    )


def refused(error: pytest.ExceptionInfo[AppError], code: str) -> None:
    assert error.value.code == code, error.value


# ================================================================ the switch and SHADOW


def test_every_supplier_is_off_by_default_and_a_disabled_run_makes_no_engine_call(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, engine_calls: list[str]
) -> None:
    with container(config, clock, shop) as app:
        assert count(config, "adaptive_shadow_switch_entries") == 0
        for supplier in (SUPPLIER_KEY, "kmretail"):
            assert app.shadow_switch.current(supplier) is None
        run_id = collect_once(app, clock, PRODUCT_URL)
        run = app.collection.run(run_id)
        assert run.outcome is CollectionOutcome.RECORDED
        assert run.frozen is not None and run.frozen.shadow.decision == "DISABLED"
        assert run.frozen.shadow.switch_entry_id is None and run.frozen.shadow.bundle_key is None
        assert engine_calls == [], "a disabled run makes zero Adaptive engine calls (S7)"
        assert count(config, "adaptive_shadow_records") == 0


def test_the_production_gate_admits_only_a_supplier_with_connect_and_an_envelope(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    # The fake shop has a COLLECT envelope here but no CONNECT definition, and KM통상 has a CONNECT
    # definition but no envelope in this container: neither is admitted, so neither gets a
    # profile, a switch entry or a window.
    with container(config, clock, shop, admit=False) as app:
        for supplier in (SUPPLIER_KEY, "kmretail"):
            with pytest.raises(InputValidationError):
                app.adaptive_profiles.save_template(
                    template("plain", choice=False, supplier=supplier),
                    created_by=OPERATOR,
                    correlation_id=CORRELATION,
                )
        run_id = collect_once(app, clock, PRODUCT_URL)
        frozen = app.collection.run(run_id).frozen
        assert frozen is not None and frozen.shadow.decision == "DISABLED"


def test_only_a_currently_validated_epr_is_enabled_and_it_enters_shadow(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        digest = save_profile(app)
        current = freshness_tuple(app.adaptive_profiles.load_bundle(digest), [], None)
        with pytest.raises(InputValidationError) as error:  # no PASS run yet
            app.shadow_switch.enable(
                digest, current, actor=OPERATOR, reason="R", correlation_id=CORRELATION
            )
        refused(error, ADAPTIVE_SHADOW_REFUSED)
        freshness = validate(app, digest)
        for index in (1, 2, 3, 4, 6):
            stale = tuple(f"{p}-old" if i == index else p for i, p in enumerate(freshness))
            with pytest.raises(InputValidationError) as error:
                app.shadow_switch.enable(
                    digest, stale, actor=OPERATOR, reason="R", correlation_id=CORRELATION
                )
            refused(error, ADAPTIVE_SHADOW_REFUSED)
        assert app.adaptive_profiles.state(digest) == "DRAFT"
        entry = app.shadow_switch.enable(
            digest, freshness, actor=OPERATOR, reason="R", correlation_id=CORRELATION
        )
        assert app.adaptive_profiles.state(digest) == "SHADOW"
        current_entry = app.shadow_switch.current(SUPPLIER_KEY)
        assert current_entry is not None and current_entry.entry_id == entry
        assert current_entry.action == "ENABLE" and current_entry.epr_digest == digest
        assert current_entry.freshness == freshness
        assert current_entry.bundle_key == bundle_key_of(digest)

        # A second EPR enabled for the supplier replaces the first, which returns to DRAFT.
        other, _ = enabled_profile(app, "other")
        assert app.adaptive_profiles.state(other) == "SHADOW"
        assert app.adaptive_profiles.state(digest) == "DRAFT"
        app.shadow_switch.disable(SUPPLIER_KEY, actor=OPERATOR, reason="OFF", correlation_id="c")
        assert app.adaptive_profiles.state(other) == "DRAFT"
        with pytest.raises(InputValidationError) as error:
            app.shadow_switch.disable(
                SUPPLIER_KEY, actor=OPERATOR, reason="OFF", correlation_id="c"
            )
        refused(error, ADAPTIVE_SHADOW_REFUSED)
        assert [e.action for e in app.shadow_switch.entries(SUPPLIER_KEY)] == [
            "ENABLE",
            "ENABLE",
            "DISABLE",
        ]
        app.adaptive_profiles.retire(digest, actor=OPERATOR, reason="R", correlation_id="c")
        with pytest.raises(InputValidationError) as error:
            app.shadow_switch.enable(
                digest, freshness, actor=OPERATOR, reason="R", correlation_id="c"
            )
        refused(error, ADAPTIVE_SHADOW_REFUSED)


def test_nothing_but_the_switch_moves_an_epr_into_or_out_of_shadow(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        digest = save_profile(app)
        validate(app, digest)
        with pytest.raises(InputValidationError) as error:
            app.adaptive_profiles._transition(digest, "SHADOW", OPERATOR, "R", CORRELATION)
        refused(error, ADAPTIVE_LIFECYCLE_REFUSED)
        insert = (
            "INSERT INTO adaptive_profile_transitions VALUES (?, ?, 2, ?, ?, 'R', 'a', 'c',"
            " '2026-09-13 00:00:00.000000')"
        )
        with contextlib.closing(raw(config)) as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, ("t-1", digest, "DRAFT", "SHADOW"))
        enabled, _ = enabled_profile(app, "enabled")
        seq = len(app.adaptive_profiles.lifecycle(enabled)) + 1
        with contextlib.closing(raw(config)) as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                insert.replace(", 2,", f", {seq},"), ("t-2", enabled, "SHADOW", "DRAFT")
            )
        assert app.adaptive_profiles.state(enabled) == "SHADOW"


def test_the_switch_history_is_append_only(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        enabled_profile(app)
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            "UPDATE adaptive_shadow_switch_entries SET reason = 'X'",
            "DELETE FROM adaptive_shadow_switch_entries",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)


# ================================================================ the frozen decision


class FlakyGateway(FakeGateway):
    """Fails the first product read with a transient error, then serves the page."""

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        if self.document_reads == 0:
            self.document_reads += 1
            raise TransientError("SYNTHETIC_TRANSIENT", "the provider did not answer")
        return super().read_document(profile, url, kind=kind, budget=budget, session=session)


def test_the_first_reservation_freezes_the_decision_and_a_retry_keeps_it(
    config: AppConfig, clock: FakeClock
) -> None:
    flaky = FlakyGateway(documents=gateway().documents, images=gateway().images)
    with container(config, clock, flaky) as app:
        digest, bundle_key = enabled_profile(app)
        entry = app.shadow_switch.current(SUPPLIER_KEY)
        assert entry is not None
        submitted = app.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
        app.runner.run_next()  # the first attempt reserves, freezes and fails transiently
        first = app.collection.run(submitted.collection_run_id)
        assert first.outcome is CollectionOutcome.PENDING
        assert first.frozen is not None
        assert first.frozen.shadow.decision == "ENABLED"
        assert first.frozen.shadow.switch_entry_id == entry.entry_id
        assert first.frozen.shadow.bundle_key == bundle_key
        # The switch changes before the retry: the retry keeps what the first reservation froze.
        app.shadow_switch.disable(SUPPLIER_KEY, actor=OPERATOR, reason="OFF", correlation_id="c")
        clock.advance(max(INTERVAL, 600) + 1)
        app.runner.run_next()
        settled = app.collection.run(submitted.collection_run_id)
        assert settled.outcome is CollectionOutcome.RECORDED
        assert settled.frozen == first.frozen
        assert settled.product_read_at is not None and first.product_read_at is not None
        assert settled.product_read_at > first.product_read_at, "the retry was a real read"
        assert settled.frozen.first_product_read_at == first.frozen.first_product_read_at
        # The shadow ran on the frozen bundle, although the switch is off now.
        record = app.shadow_evidence.record(submitted.collection_run_id)
        assert record.bundle_key == bundle_key
        assert app.adaptive_profiles.state(digest) == "DRAFT"


def test_a_frozen_decision_is_never_rewritten(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        run_id = collect_once(app, clock, PRODUCT_URL)
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            "UPDATE collection_runs SET shadow_decision = 'ENABLED' WHERE collection_run_id = ?",
            "UPDATE collection_runs SET first_product_read_at = NULL WHERE collection_run_id = ?",
            "UPDATE collection_runs SET shadow_decision = NULL WHERE collection_run_id = ?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (run_id,))


class SwitchingGateway(FakeGateway):
    """Changes the shadow switch while the product is being read, after the reservation."""

    change: Callable[[], None] | None = None

    def read_document(
        self,
        profile: CollectionProfile,
        url: str,
        *,
        kind: ReadKind,
        budget: RequestBudget,
        session: bytes | None = None,
    ) -> DocumentView:
        if self.change is not None:
            self.change()
        return super().read_document(profile, url, kind=kind, budget=budget, session=session)


def test_a_switch_change_after_the_reservation_changes_neither_execution_nor_eligibility(
    config: AppConfig, clock: FakeClock, engine_calls: list[str]
) -> None:
    switching = SwitchingGateway(documents=gateway().documents, images=gateway().images)
    with container(config, clock, switching) as app:
        _, first_bundle = enabled_profile(app, "first")
        window = declare(app, first_bundle)

        def move_the_switch() -> None:
            enabled_profile(app, "second")  # enables another EPR for the supplier mid-read

        switching.change = move_the_switch
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert engine_calls == ["extract"]
        assert app.shadow_evidence.record(run_id).bundle_key == first_bundle
        events = app.shadow_evidence.events(run_id)
        assert [e.kind for e in events] == [EventKind.OUTCOME_RECORDED]
        assert events[0].window_id == window
        assert set(app.shadow_evidence.window_evidence(window).states) == {run_id}


# ================================================================ invariance and isolation


def _second_config(tmp_path: Path, migrated_template: Path) -> AppConfig:
    other = tmp_path / "shadow-off"
    database = database_path(other)
    database.parent.mkdir(parents=True)
    shutil.copyfile(migrated_template, database)
    return make_config(other)


def test_shadow_on_and_off_leave_identical_canonical_state_and_requests(
    config: AppConfig, tmp_path: Path, migrated_template: Path, engine_calls: list[str]
) -> None:
    off_config = _second_config(tmp_path, migrated_template)
    on_shop, off_shop = gateway(), gateway()
    on_clock, off_clock = FakeClock(), FakeClock()  # the same instants on both sides
    with container(config, on_clock, on_shop) as on:
        enabled_profile(on)
        collect_once(on, on_clock, PRODUCT_URL)
    with container(off_config, off_clock, off_shop) as off:
        save_profile(off)  # the same profile stored, but no switch entry
        collect_once(off, off_clock, PRODUCT_URL)
    assert engine_calls == ["extract"], "the shadow ran once, for the enabled run only"
    assert canonical_snapshot(config) == canonical_snapshot(off_config), "S2"
    assert (on_shop.document_reads, on_shop.image_reads) == (
        off_shop.document_reads,
        off_shop.image_reads,
    ), "S1: zero additional supplier requests"
    assert count(config, "adaptive_shadow_records") == 1
    assert count(off_config, "adaptive_shadow_records") == 0


def test_a_failing_shadow_write_leaves_the_canonical_commit_intact(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)

        def broken(self: ShadowEvidenceStore, **_: object) -> None:
            raise RuntimeError("the shadow's own unit fails")

        monkeypatch.setattr(ShadowEvidenceStore, "record_outcome", broken)
        submitted = app.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
        app.runner.run_next()
        run = app.collection.run(submitted.collection_run_id)
        assert run.outcome is CollectionOutcome.RECORDED and run.revision_id is not None
        assert app.jobs.get(submitted.job_id).state == JobState.SUCCEEDED
        assert count(config, "adaptive_shadow_records") == 0
        monkeypatch.undo()
        # The missing outcome is made visible, and it blocks: it is not a recovery.
        assert app.shadow_evidence.reconcile(window) == 1
        assert app.shadow_evidence.state(run.collection_run_id) == State(
            CountAs.INCOMPLETE, Cause.SHADOW_MISSING
        )
        assert app.shadow_evidence.bundle(bundle_key).verdict is BundleVerdict.BLOCKED


def test_an_engine_failure_is_a_shadow_failed_record_that_counts_as_a_failure(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exploding(*_: object, **__: object) -> None:
        raise ValueError("the engine gave up")

    monkeypatch.setattr(runner_module, "extract", exploding)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert app.collection.run(run_id).outcome is CollectionOutcome.RECORDED
        record = app.shadow_evidence.record(run_id)
        assert record.verdict is RunVerdict.SHADOW_FAILED
        assert record.comparison == {"verdict": "SHADOW_FAILED", "failure": "ValueError"}
        assert app.shadow_evidence.state(run_id) == State(CountAs.FAILURE)
        assert app.shadow_evidence.window_evidence(window).verdict is WindowVerdict.FAIL


def test_a_process_killed_between_commit_and_shadow_counts_after_recovery_with_no_refetch(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        submitted = app.collection.submit(SUPPLIER_KEY, PRODUCT_URL)

        def killed(_: object) -> None:
            raise ProcessKilled

        app.collection._shadow = killed  # type: ignore[assignment]
        with pytest.raises(ProcessKilled):
            app.collection._run_job(job_context(app, submitted.collection_run_id, 1))
        pending = app.collection.run(submitted.collection_run_id)
        assert pending.outcome is CollectionOutcome.PENDING
        assert count(config, "product_facts_revisions") == 1, "the canonical unit committed"
    reads = shop.document_reads, list(shop.image_reads)
    clock.advance(INTERVAL + 1)
    with container(config, clock, shop) as restarted:  # a new process recovers the run
        restarted.collection._run_job(job_context(restarted, submitted.collection_run_id, 2))
        run = restarted.collection.run(submitted.collection_run_id)
        assert run.outcome is CollectionOutcome.RECORDED and run.settled_by_recovery is True
        assert (shop.document_reads, shop.image_reads) == reads, "recovery never refetches"
        assert count(config, "adaptive_shadow_records") == 0
        # The run stays in the denominator as INCOMPLETE, made visible by reconciliation.
        assert restarted.shadow_evidence.reconcile() == 1
        assert restarted.shadow_evidence.reconcile() == 0, "idempotent"
        after = State(CountAs.INCOMPLETE, Cause.SHADOW_MISSING_AFTER_RECOVERY)
        assert restarted.shadow_evidence.state(submitted.collection_run_id) == after
        restarted.shadow_evidence.end(window, actor=OPERATOR, reason="END")
        closeout = restarted.shadow_evidence.close(window, actor=OPERATOR, reason="CLOSE")
        assert closeout["verdict"] == WindowVerdict.INCOMPLETE.value
        assert closeout["denominator"] == 1
        # The one supersession the contract allows: the same bundle, before any new collection.
        assert restarted.shadow_evidence.bundle(bundle_key).reasons == (
            f"RECOVERY_WINDOW_NOT_SUPERSEDED:{window}",
            "NO_CLOSED_PASS_WINDOW",
        )
        successor = declare(restarted, bundle_key, supersedes=window)
        superseded = restarted.shadow_evidence.window(window)
        assert superseded.superseded and superseded.events[-1] == "SUPERSEDED"
        evidence = restarted.shadow_evidence.bundle_windows(bundle_key)
        assert [w.window_id for w in evidence] == [window, successor], "it stays in the evidence"


def test_a_run_frozen_for_another_bundle_is_outside_the_window(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        other_digest = save_profile(app, "other")
        validate(app, other_digest)
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key_of(other_digest))  # a window for a different bundle
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert app.shadow_evidence.record(run_id).bundle_key == bundle_key
        assert app.shadow_evidence.events(run_id) == ()
        assert app.shadow_evidence.window_evidence(window).states == {}


# ================================================================ the ledger and retention


def test_pruning_an_unresolved_run_appends_the_prune_and_a_late_resolution_is_refused(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MISMATCH)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        declare(app, bundle_key)
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert app.shadow_evidence.state(run_id) == State(
            CountAs.INCOMPLETE, Cause.UNRESOLVED_MISMATCH
        )
        clock.advance(timedelta(days=90).total_seconds() + 1)
        report = app.shadow_evidence.prune()
        assert (report.pruned, report.unresolved_pruned) == (1, 1)
        assert [e.kind for e in app.shadow_evidence.events(run_id)] == [
            EventKind.OUTCOME_RECORDED,
            EventKind.RAW_PRUNED_UNRESOLVED,
        ]
        assert app.shadow_evidence.state(run_id) == State(
            CountAs.INCOMPLETE, Cause.PRUNED_BEFORE_RESOLUTION
        )
        with pytest.raises(InputValidationError) as error:
            app.shadow_evidence.resolve(
                run_id,
                Resolution.ADAPTIVE_CORRECT,
                evidence_ref="fixture:1",
                adaptive_failed_closed=False,
                actor=OPERATOR,
                correlation_id=CORRELATION,
            )
        refused(error, ADAPTIVE_RESOLUTION_REFUSED)
        # A permanent cause: this exact bundle is never given another window.
        with pytest.raises(InputValidationError) as error:
            declare(app, bundle_key)
        refused(error, ADAPTIVE_BUNDLE_BLOCKED)


def test_pruning_a_terminal_run_appends_nothing(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MATCH)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        declare(app, bundle_key)
        run_id = collect_once(app, clock, PRODUCT_URL)
        clock.advance(timedelta(days=91).total_seconds())
        assert app.shadow_evidence.prune().pruned == 1
        assert [e.kind for e in app.shadow_evidence.events(run_id)] == [EventKind.OUTCOME_RECORDED]
        assert app.shadow_evidence.state(run_id) == State(CountAs.SUCCESS)


def test_the_count_bound_prunes_beyond_it_and_reports_the_nearer_bound(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MATCH)
    with container(config, clock, shop) as app:
        enabled_profile(app)
        runs = [collect_once(app, clock, PRODUCT_URL) for _ in range(3)]
        tight = ShadowEvidenceStore(
            app.db,
            clock,
            supplier_gate=build_supplier_gate({SUPPLIER_KEY}),
            profiles=app.adaptive_profiles,
            retention=RawRetention(timedelta(days=90), 2),
            process_run_id="test-process",
        )
        status = tight.retention_status(SUPPLIER_KEY)
        assert (status.records, status.count_headroom, status.nearer_bound) == (3, 0, "COUNT")
        assert tight.prune().pruned == 1
        assert count(config, "adaptive_shadow_records") == 2
        with pytest.raises(AppError):
            tight.record(runs[0])  # the oldest went first
        roomy = app.shadow_evidence.retention_status(SUPPLIER_KEY)
        assert (roomy.records, roomy.count_headroom, roomy.nearer_bound) == (2, 4998, "AGE")
        assert roomy.age_deadline is not None and roomy.oldest_recorded_at is not None
        assert roomy.age_deadline - roomy.oldest_recorded_at == timedelta(days=90)


def test_the_adr_count_bound_is_five_thousand_raw_records_per_supplier(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    # The production bounds exactly (90 days, 5,000): 5,001 raw records, the oldest pruned first.
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        start = clock.now()
        runs = [f"run-{i:05d}" for i in range(5001)]
        with contextlib.closing(raw(config)) as connection:
            connection.executemany(
                "INSERT INTO collection_runs (collection_run_id, job_id, correlation_id,"
                " supplier_key, source_url, outcome, detail, requested_at, finished_at)"
                " VALUES (?, ?, 'c', ?, ?, 'NO_REVISION', 'x', ?, ?)",
                [(run, run, SUPPLIER_KEY, PRODUCT_URL, str(start), str(start)) for run in runs],
            )
            connection.executemany(
                "INSERT INTO adaptive_shadow_records VALUES (?, ?, NULL, ?, 'MATCH', NULL, '{}',"
                " ?, 'p', ?)",
                [
                    (run, SUPPLIER_KEY, bundle_key, "0" * 64, str(start + timedelta(seconds=i)))
                    for i, run in enumerate(runs)
                ],
            )
            connection.commit()
        status = app.shadow_evidence.retention_status(SUPPLIER_KEY)
        assert (status.records, status.count_headroom, status.nearer_bound) == (5001, 0, "COUNT")
        assert app.shadow_evidence.prune().pruned == 1
        assert count(config, "adaptive_shadow_records") == 5000
        remaining = rows(config, "SELECT collection_run_id FROM adaptive_shadow_records")
        assert ("run-00000",) not in remaining and count(config, "collection_runs") >= 5001


@pytest.mark.parametrize(
    ("age", "records"),
    [(timedelta(days=91), 5000), (timedelta(days=90), 5001), (timedelta(0), 10), (timedelta(1), 0)],
)
def test_the_raw_bounds_are_explicit_and_never_looser_than_the_adr(
    age: timedelta, records: int
) -> None:
    with pytest.raises(InputValidationError) as error:
        RawRetention(age, records)
    refused(error, ADAPTIVE_RETENTION_INVALID)


def test_a_resolved_run_is_counted_once_and_no_event_follows_a_terminal_state(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MISMATCH)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        run_id = collect_once(app, clock, PRODUCT_URL)
        resolved = app.shadow_evidence.resolve(
            run_id,
            Resolution.ADAPTIVE_CORRECT,
            evidence_ref="fixture:source-page",
            adaptive_failed_closed=False,
            actor=OPERATOR,
            correlation_id=CORRELATION,
        )
        assert resolved == State(CountAs.SUCCESS)
        assert len(app.shadow_evidence.events(run_id)) == 2
        assert app.shadow_evidence.window_evidence(window).states == {run_id: resolved}
        with pytest.raises(InputValidationError) as error:
            app.shadow_evidence.resolve(
                run_id,
                Resolution.CURRENT_CORRECT,
                evidence_ref="fixture:again",
                adaptive_failed_closed=False,
                actor=OPERATOR,
                correlation_id=CORRELATION,
            )
        refused(error, ADAPTIVE_RESOLUTION_REFUSED)
    with contextlib.closing(raw(config)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO adaptive_shadow_ledger_events SELECT 'e-x', collection_run_id, 3,"
                " window_id, 'RAW_PRUNED_UNRESOLVED', 'INCOMPLETE', 'PRUNED_BEFORE_RESOLUTION',"
                " NULL, NULL, NULL, NULL, NULL, process_run_id, actor, correlation_id,"
                " occurred_at FROM adaptive_shadow_ledger_events WHERE seq = 2"
            )
        for statement in (
            "UPDATE adaptive_shadow_ledger_events SET count_as = 'FAILURE'",
            "DELETE FROM adaptive_shadow_ledger_events",
            "UPDATE adaptive_shadow_records SET run_verdict = 'MATCH'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)


# ================================================================ windows


def test_a_window_holding_an_unresolved_mismatch_can_end_but_not_close(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MISMATCH)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        collect_once(app, clock, PRODUCT_URL)
        app.shadow_evidence.end(window, actor=OPERATOR, reason="END")
        with pytest.raises(InputValidationError) as error:
            app.shadow_evidence.close(window, actor=OPERATOR, reason="CLOSE")
        refused(error, ADAPTIVE_WINDOW_REFUSED)
        assert app.shadow_evidence.window(window).events == ("DECLARED", "ENDED")
        # A run reserved after the end is not in the window.
        collect_once(app, clock, PRODUCT_URL)
        assert len(app.shadow_evidence.window_evidence(window).states) == 1


def test_end_and_close_reconcile_missing_outcomes_first_and_idempotently(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        real = ShadowEvidenceStore.record_outcome

        def broken(self: ShadowEvidenceStore, **_: object) -> None:
            raise RuntimeError("the shadow's own unit fails")

        monkeypatch.setattr(ShadowEvidenceStore, "record_outcome", broken)
        run_id = collect_once(app, clock, PRODUCT_URL)
        monkeypatch.setattr(ShadowEvidenceStore, "record_outcome", real)
        assert app.shadow_evidence.events(run_id) == ()
        app.shadow_evidence.end(window, actor=OPERATOR, reason="END")  # no restart in between
        assert [e.kind for e in app.shadow_evidence.events(run_id)] == [EventKind.OUTCOME_RECORDED]
        closeout = app.shadow_evidence.close(window, actor=OPERATOR, reason="CLOSE")
        assert len(app.shadow_evidence.events(run_id)) == 1, "close reconciled nothing twice"
        assert closeout["runs"] == [
            {
                "collection_run_id": run_id,
                "count_as": "INCOMPLETE",
                "cause": "SHADOW_MISSING",
                "last_seq": 1,
            }
        ]
    with contextlib.closing(raw(config)) as connection:
        for statement in (
            "UPDATE adaptive_evidence_window_events SET detail_json = '{}'",
            "DELETE FROM adaptive_evidence_window_events",
            "DELETE FROM adaptive_evidence_windows",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)


def _pass_window(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, bundle_key: str, window: str
) -> None:
    """Three MATCH runs across two processes, then end and close the window."""
    with container(config, clock, shop) as first:
        for _ in range(2):
            collect_once(first, clock, PRODUCT_URL)
    with container(config, clock, shop) as second:
        collect_once(second, clock, PRODUCT_URL)
        second.shadow_evidence.end(window, actor=OPERATOR, reason="END")
        closeout = second.shadow_evidence.close(window, actor=OPERATOR, reason="CLOSE")
        assert closeout["verdict"] == "PASS" and closeout["restart_seen"] is True


def test_a_pass_window_needs_k_runs_after_a_restart_and_then_the_bundle_passes(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MATCH)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        for _ in range(3):
            collect_once(app, clock, PRODUCT_URL)
        evidence = app.shadow_evidence.window_evidence(window)
        assert len(evidence.states) == 3
        assert evidence.verdict is WindowVerdict.INCOMPLETE, "no restart yet"
    _pass_window(config, clock, shop, bundle_key, window)
    with container(config, clock, shop) as app:
        assert app.shadow_evidence.bundle(bundle_key).verdict is BundleVerdict.PASS


def test_a_later_pass_window_never_hides_an_earlier_blocking_cause(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MISMATCH, RunVerdict.MATCH)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        earlier = declare(app, bundle_key)
        unresolved = collect_once(app, clock, PRODUCT_URL)
        app.shadow_evidence.end(earlier, actor=OPERATOR, reason="END")
        later = declare(app, bundle_key)
    _pass_window(config, clock, shop, bundle_key, later)
    with container(config, clock, shop) as app:
        verdict = app.shadow_evidence.bundle(bundle_key)
        assert verdict.verdict is BundleVerdict.BLOCKED
        assert verdict.reasons == (f"UNRESOLVED_MISMATCH:{earlier}:{unresolved}",)
        # Resolving it lifts the block, and the earlier window still counts: below K, not PASS.
        app.shadow_evidence.resolve(
            unresolved,
            Resolution.ADAPTIVE_CORRECT,
            evidence_ref="fixture:source",
            adaptive_failed_closed=False,
            actor=OPERATOR,
            correlation_id=CORRELATION,
        )
        app.shadow_evidence.close(earlier, actor=OPERATOR, reason="CLOSE")
        again = app.shadow_evidence.bundle(bundle_key)
        assert again.verdict is BundleVerdict.INCOMPLETE
        assert again.reasons == (f"WINDOW_NOT_PASSED:{earlier}",)


def test_only_a_closed_all_after_recovery_window_is_superseded(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.SHADOW_FAILED)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        with pytest.raises(InputValidationError) as error:  # still open
            declare(app, bundle_key, supersedes=window)
        refused(error, ADAPTIVE_WINDOW_REFUSED)
        collect_once(app, clock, PRODUCT_URL)
        app.shadow_evidence.end(window, actor=OPERATOR, reason="END")
        app.shadow_evidence.close(window, actor=OPERATOR, reason="CLOSE")
        # A FAIL window disqualifies the exact bundle: no supersession, no further window.
        for kwargs in ({"supersedes": window}, {}):
            with pytest.raises(InputValidationError) as error:
                declare(app, bundle_key, **kwargs)
            refused(error, ADAPTIVE_BUNDLE_BLOCKED)


def test_one_window_per_supplier_is_open_and_windows_are_declared_for_what_this_engine_runs(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        digest, bundle_key = enabled_profile(app)
        declare(app, bundle_key)
        with pytest.raises(InputValidationError) as error:
            declare(app, bundle_key)
        refused(error, ADAPTIVE_WINDOW_REFUSED)
        with contextlib.closing(raw(config)) as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO adaptive_evidence_windows VALUES ('w-x', ?, ?, 3, 'a', 'c',"
                " '2026-09-13 00:00:00.000000')",
                (SUPPLIER_KEY, bundle_key),
            )
        for bad in (bundle_key_of(digest, "adaptive-engine-0"), bundle_key_of("0" * 64)):
            with pytest.raises(AppError):
                declare(app, bad)
        with pytest.raises(InputValidationError):
            declare(app, bundle_key, min_size=2)


def test_an_exact_bundle_with_a_permanent_cause_is_never_retried_under_another_digest_claim(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Carry-forward `5818794101` item 1, with IMAGE_UNMATCHABLE (§10.4) as the permanent cause.
    script(monkeypatch, RunVerdict.IMAGE_UNMATCHABLE)
    with container(config, clock, shop) as app:
        digest, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        run_id = collect_once(app, clock, PRODUCT_URL)
        assert app.shadow_evidence.state(run_id) == State(
            CountAs.INCOMPLETE, Cause.IMAGE_UNMATCHABLE
        )
        app.shadow_evidence.end(window, actor=OPERATOR, reason="END")
        closeout = app.shadow_evidence.close(window, actor=OPERATOR, reason="CLOSE")
        assert closeout["verdict"] == "INCOMPLETE", "never a success, and never resolvable"
        with pytest.raises(InputValidationError) as error:
            app.shadow_evidence.resolve(
                run_id,
                Resolution.ADAPTIVE_CORRECT,
                evidence_ref="fixture:x",
                adaptive_failed_closed=True,
                actor=OPERATOR,
                correlation_id=CORRELATION,
            )
        refused(error, ADAPTIVE_RESOLUTION_REFUSED)
        # A content-identical EPR — whatever its note, origin or author — is the same digest and
        # the same bundle, so it inherits the block and cannot open fresh evidence.
        ptr = app.adaptive_profiles.load_bundle(digest).templates[0][0]
        again = app.adaptive_profiles.save_draft(
            epr([ptr], supplier=SUPPLIER_KEY),
            created_by="operator:other",
            correlation_id="corr-reissue",
            origin="IMPORT",
            change_note="reissued",
        )
        assert again == digest and bundle_key_of(again) == bundle_key
        with pytest.raises(InputValidationError) as error:
            declare(app, bundle_key)
        refused(error, ADAPTIVE_BUNDLE_BLOCKED)
        assert app.shadow_evidence.bundle(bundle_key).verdict is BundleVerdict.BLOCKED
        # Only a genuinely content-different EPR is a new bundle with fresh evidence.
        _, fresh_bundle = enabled_profile(app, "fixed", sold_out_words=["품절", "일시품절"])
        assert fresh_bundle != bundle_key
        declare(app, fresh_bundle)


# ================================================================ restart, tamper, migration


def test_shadow_evidence_reads_back_after_a_restart(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MISMATCH)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        window = declare(app, bundle_key)
        run_id = collect_once(app, clock, PRODUCT_URL)
        before = (
            app.shadow_switch.entries(SUPPLIER_KEY),
            app.shadow_evidence.record(run_id),
            app.shadow_evidence.events(run_id),
            app.shadow_evidence.window(window),
            app.shadow_evidence.window_evidence(window),
        )
    with container(config, clock, shop) as restarted:
        after = (
            restarted.shadow_switch.entries(SUPPLIER_KEY),
            restarted.shadow_evidence.record(run_id),
            restarted.shadow_evidence.events(run_id),
            restarted.shadow_evidence.window(window),
            restarted.shadow_evidence.window_evidence(window),
        )
    assert after == before


def test_shadow_evidence_changed_out_of_band_fails_closed(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(monkeypatch, RunVerdict.MISMATCH)
    with container(config, clock, shop) as app:
        _, bundle_key = enabled_profile(app)
        declare(app, bundle_key)
        run_id = collect_once(app, clock, PRODUCT_URL)
    with contextlib.closing(raw(config)) as connection:
        connection.execute("DROP TRIGGER trg_adaptive_shadow_records_no_update")
        connection.execute("DROP TRIGGER trg_adaptive_shadow_ledger_events_no_update")
        connection.execute("UPDATE adaptive_shadow_records SET run_verdict = 'MATCH'")
        connection.execute(
            "UPDATE adaptive_shadow_ledger_events SET count_as = 'SUCCESS', cause = NULL"
        )
        connection.commit()
    with container(config, clock, shop) as app:
        with pytest.raises(ShadowEvidenceTampered):
            app.shadow_evidence.record(run_id)
        with pytest.raises(ShadowEvidenceTampered):
            app.shadow_evidence.state(run_id)
        with pytest.raises(ShadowEvidenceTampered):
            app.shadow_evidence.bundle(bundle_key)


def test_the_comparison_record_keeps_no_url_or_written_reference(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        enabled_profile(app)
        run_id = collect_once(app, clock, PRODUCT_URL)
    text = str(comparison_json(config, run_id))
    assert "http" not in text and "invalid" not in text and "/p/" not in text
    # S5: the document body stays in memory; nothing of the page's markup is kept.
    assert "<" not in text and "img" not in text


def test_0025_is_additive_and_its_downgrade_never_destroys_a_frozen_decision(
    tmp_path: Path, clock: FakeClock
) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    command.upgrade(alembic_config(url), "0024_adaptive_profile_validation")

    def schema() -> dict[str, str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            found = connection.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
            return dict(found.fetchall())

    before = schema()
    upgrade_to_head(url)
    after = schema()
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"collection_runs"}, "only the run record gains its nullable columns"
    added = set(after) - set(before)
    assert set(SHADOW_TABLES) <= added
    assert all(
        name.startswith(("adaptive_", "trg_adaptive_", "ix_adaptive_", "trg_collection_runs_"))
        for name in added
    ), added
    with contextlib.closing(sqlite3.connect(database)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(collection_runs)")}
    assert {
        "shadow_decision",
        "shadow_switch_entry_id",
        "shadow_bundle_key",
        "first_product_read_at",
        "settled_by_recovery",
    } <= columns
    # An empty shadow owner steps down; a frozen decision or any shadow row never does.
    command.downgrade(alembic_config(url), "0024_adaptive_profile_validation")
    assert schema() == before
    upgrade_to_head(url)
    engine = create_sqlite_engine(url)
    try:
        assert current_revision(engine) == "0025_adaptive_shadow_foundation"
    finally:
        engine.dispose()


def test_the_downgrade_keeps_disabled_runs_and_refuses_while_shadow_evidence_exists(
    config: AppConfig, clock: FakeClock, shop: FakeGateway, tmp_path: Path, migrated_template: Path
) -> None:
    # A DISABLED run carries no shadow evidence: the step down keeps the run and drops only the
    # columns, as every earlier fail-closed downgrade keeps its rows.
    with container(config, clock, shop) as app:
        run_id = collect_once(app, clock, PRODUCT_URL)
        url = app.db.url
    command.downgrade(alembic_config(url), "0024_adaptive_profile_validation")
    assert rows(config, "SELECT collection_run_id, outcome FROM collection_runs") == [
        (run_id, "RECORDED")
    ]
    upgrade_to_head(url)
    # An ENABLED run, and the switch history behind it, are evidence: the step down refuses.
    enabled = _second_config(tmp_path, migrated_template)
    with container(enabled, clock, shop) as app:
        enabled_profile(app)
        collect_once(app, clock, PRODUCT_URL)
        enabled_url = app.db.url
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(enabled_url), "0024_adaptive_profile_validation")
    assert rows(enabled, "SELECT shadow_decision FROM collection_runs") == [("ENABLED",)]
    with contextlib.closing(raw(enabled)) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert set(SHADOW_TABLES) <= tables
