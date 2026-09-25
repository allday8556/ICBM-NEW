"""Phase C C0 over the real COLLECT path and the harness (Issue #110 `5826469852`).

Everything here runs against synthetic data roots and the fake shop: no supplier is read, the
socket layer is refused, and no real KM통상 profile, sample, switch entry or window exists. What
this proves:

- the capture decision is frozen at a run's genuinely first reservation, default OFF, consuming at
  most one live request; a retry keeps it, a pre-seam run is never captured (item 3);
- a requested capture keeps only sanitized candidate material, never the page body; its failure or
  refusal never touches the canonical run; capture ON and OFF leave identical canonical state;
- a candidate becomes a new immutable ValidationSample only through the P2 owner (item 4);
- the harness holds the ADR-0006 lease for every data-root command, fails ``DATA_DIR_IN_USE``
  under contention with no side effect, gates stages and approvals, and runs the later C1–C4
  operations end to end on a synthetic root (items 1, 2, 6–8);
- migration 0027 is additive and its downgrade never destroys capture evidence.
"""

import contextlib
import io
import json
import shutil
import socket
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command

import app.collect.adaptive_capture.runner as capture_runner
import app.collect.adaptive_shadow.runner as shadow_runner
from app.collect.adaptive_shadow.compare import Comparison
from app.collect.adaptive_shadow.evidence import RunVerdict
from app.collect.adaptive_store.gate import build_supplier_gate
from app.collect.collection import pacing_key
from app.collect.models import CollectionOutcome
from app.config import AppConfig, database_path
from app.container import Container, build_container
from app.core.ownership import DataDirLease, acquire_data_dir
from app.db.migrate import alembic_config, upgrade_to_head
from scripts.m3collect.fake_shop import PRODUCT_URL, SUPPLIER_KEY, FakeGateway, StubSessions, page
from scripts.phasec.harness import (
    EXIT_DATA_DIR_IN_USE,
    EXIT_OK,
    EXIT_REFUSED,
    run,
)
from scripts.phasec.ledger import CampaignLedger, approval_phrase
from tests.conftest import make_config
from tests.shadow_support import (
    INTERVAL,
    OPERATOR,
    canonical_snapshot,
    collect_once,
    container,
    count,
    gateway,
    job_context,
    raw,
    registered,
    rows,
    save_profile,
    validate,
)
from tests.support import FakeClock

pytestmark = pytest.mark.integration

CODE_SHA = "c" * 40
CAMPAIGN = "phase-c-synthetic-c0"
SECRET_PAGE = page().replace(
    "<body>",
    '<body><form><input type="hidden" name="csrf_token" value="deadbeefdeadbeefdeadbeefdeadbeef">'
    "</form><script>track('visitor', 42);</script>",
)


class NetworkRefused(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise NetworkRefused("C0 makes no network call")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


@pytest.fixture
def shop() -> FakeGateway:
    return FakeGateway(documents=[SECRET_PAGE], images=gateway().images)


@pytest.fixture
def campaign_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("campaign-root") / "campaign"


@pytest.fixture
def environ(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    return {"USERPROFILE": str(tmp_path_factory.mktemp("home"))}


def target(app: Container) -> str:
    return pacing_key(registered().collection, PRODUCT_URL).url


def request_capture(app: Container, *, lifetime: timedelta = timedelta(hours=1)) -> str:
    return app.capture_store.request(
        campaign_id=CAMPAIGN,
        supplier_key=SUPPLIER_KEY,
        target=target(app),
        lifetime=lifetime,
        requested_by=OPERATOR,
        correlation_id="corr-c0",
    )


# ================================================================ the capture seam


def test_capture_is_off_by_default_and_frozen_at_the_first_reservation(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        run_id = collect_once(app, clock, PRODUCT_URL)
        frozen = app.collection.run(run_id).frozen
        assert frozen is not None and frozen.capture is not None
        assert frozen.capture.decision == "OFF" and frozen.capture.request_id is None
        assert count(config, "adaptive_capture_candidates") == 0


def test_a_requested_capture_keeps_only_sanitized_material_and_is_consumed_once(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        request_id = request_capture(app)
        first = collect_once(app, clock, PRODUCT_URL)
        second = collect_once(app, clock, PRODUCT_URL)
        frozen = app.collection.run(first).frozen
        assert frozen is not None and frozen.capture is not None
        assert (frozen.capture.decision, frozen.capture.request_id) == ("REQUESTED", request_id)
        later = app.collection.run(second).frozen
        assert later is not None and later.capture is not None
        assert later.capture.decision == "OFF", "a request is consumed by one run only"
        view = app.capture_store.candidate(first)
        assert view.status == "CAPTURED" and view.candidate is not None
        assert view.revision_id == app.collection.run(first).revision_id
        text = view.candidate.structure_json
        # The accepted P1 sanitizer keeps a hidden control's structure and strips its value, and
        # keeps no executable code.
        assert "deadbeef" not in text and "track(" not in text and '"value"' not in text
        assert "<" not in text, "never the page body"
        (record,) = app.capture_store.requests(CAMPAIGN)
        assert record.consumed_by == first


def test_an_expired_request_captures_nothing(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        request_capture(app, lifetime=timedelta(minutes=5))
        clock.advance(timedelta(minutes=6).total_seconds())
        run_id = collect_once(app, clock, PRODUCT_URL)
        frozen = app.collection.run(run_id).frozen
        assert frozen is not None and frozen.capture is not None
        assert frozen.capture.decision == "OFF"
        (record,) = app.capture_store.requests(CAMPAIGN)
        assert record.consumed_by is None


def test_a_request_lifetime_is_bounded_and_the_supplier_registered(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    from app.core.errors import InputValidationError

    with container(config, clock, shop) as app, pytest.raises(InputValidationError):
        request_capture(app, lifetime=timedelta(hours=25))
    with container(config, clock, shop, admit=False) as app, pytest.raises(InputValidationError):
        request_capture(app)


def test_a_retry_keeps_the_capture_decision_and_a_pre_seam_run_is_never_captured(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        request_capture(app)
        submitted = app.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
        # A run whose first reservation predates the seam: read before, no decision frozen.
        with contextlib.closing(raw(config)) as connection:
            connection.execute(
                "UPDATE collection_runs SET product_read_at = requested_at"
                " WHERE collection_run_id = ?",
                (submitted.collection_run_id,),
            )
            connection.commit()
        clock.advance(INTERVAL + 1)
        app.collection._run_job(job_context(app, submitted.collection_run_id, 2))
        frozen = app.collection.run(submitted.collection_run_id).frozen
        assert frozen is not None and frozen.capture is None, "never captured, nothing backfilled"
        (record,) = app.capture_store.requests(CAMPAIGN)
        assert record.consumed_by is None
        assert count(config, "adaptive_capture_candidates") == 0


def test_capture_on_and_off_leave_identical_canonical_state(
    config: AppConfig, tmp_path: Path, migrated_template: Path
) -> None:
    other = tmp_path / "capture-off"
    database = database_path(other)
    database.parent.mkdir(parents=True)
    shutil.copyfile(migrated_template, database)
    off_config = make_config(other)
    on_shop = FakeGateway(documents=[SECRET_PAGE], images=gateway().images)
    off_shop = FakeGateway(documents=[SECRET_PAGE], images=gateway().images)
    on_clock, off_clock = FakeClock(), FakeClock()
    with container(config, on_clock, on_shop) as app:
        request_capture(app)
        collect_once(app, on_clock, PRODUCT_URL)
    with container(off_config, off_clock, off_shop) as app:
        collect_once(app, off_clock, PRODUCT_URL)
    assert canonical_snapshot(config) == canonical_snapshot(off_config)
    assert (on_shop.document_reads, on_shop.image_reads) == (
        off_shop.document_reads,
        off_shop.image_reads,
    ), "capture makes no request of its own"
    assert count(config, "adaptive_capture_candidates") == 1
    assert count(off_config, "adaptive_capture_candidates") == 0


@pytest.mark.parametrize("failure", ["refused", "exploded"])
def test_a_failed_capture_never_touches_the_canonical_run(
    config: AppConfig,
    clock: FakeClock,
    shop: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from app.collect.adaptive.capture import CaptureRefused

    def broken(_: str) -> None:
        raise (
            CaptureRefused("residual secret or private material: [TEXT@p#.]")
            if (failure == "refused")
            else RuntimeError("the capture gave up")
        )

    monkeypatch.setattr(capture_runner, "capture_candidate", broken)
    with container(config, clock, shop) as app:
        request_capture(app)
        submitted = app.collection.submit(SUPPLIER_KEY, PRODUCT_URL)
        app.runner.run_next()
        run_record = app.collection.run(submitted.collection_run_id)
        assert run_record.outcome is CollectionOutcome.RECORDED
        view = app.capture_store.candidate(submitted.collection_run_id)
        assert view.status == "REFUSED" and view.candidate is None
        expected = "residual" if failure == "refused" else "CAPTURE_FAILED:RuntimeError"
        assert view.refusal is not None and expected in view.refusal


def test_a_candidate_becomes_a_new_immutable_sample_only_through_the_p2_owner(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    from app.collect.adaptive.capture import OperatorScope

    with container(config, clock, shop) as app:
        request_capture(app)
        run_id = collect_once(app, clock, PRODUCT_URL)
        scope = OperatorScope(OPERATOR, "2026-09-25T00:00:00Z", "primary", ())
        first = app.capture_store.finalize(
            run_id,
            scope=scope,
            expected={"original_name": "A"},
            stored_by=OPERATOR,
            correlation_id="c",
        )
        corrected = app.capture_store.finalize(
            run_id,
            scope=scope,
            expected={"original_name": "B"},
            stored_by=OPERATOR,
            correlation_id="c",
        )
        assert first.digest != corrected.digest, "a correction is a new sample"
        for sample in (first, corrected):
            assert app.adaptive_validation.sample(sample.digest) == sample
            assert (
                sample.provenance["candidate"]
                == app.capture_store.candidate(run_id).candidate.digest
            )  # type: ignore[union-attr]
            assert "profile" not in json.dumps(sample.provenance).lower()


def test_the_database_holds_the_capture_seam_shape(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        request_id = request_capture(app)
        requested = collect_once(app, clock, PRODUCT_URL)
        off = collect_once(app, clock, PRODUCT_URL)
        revision_of_off = app.collection.run(off).revision_id
    with contextlib.closing(raw(config)) as connection:
        for statement, args in (
            (
                "UPDATE collection_runs SET capture_decision = 'OFF' WHERE collection_run_id = ?",
                (requested,),
            ),
            (
                "UPDATE collection_runs SET capture_decision = 'REQUESTED', capture_request_id = ?"
                " WHERE collection_run_id = ?",
                (request_id, off),
            ),
            ("UPDATE adaptive_capture_requests SET campaign_id = 'x'", ()),
            ("DELETE FROM adaptive_capture_requests", ()),
            ("UPDATE adaptive_capture_candidates SET refusal = 'x'", ()),
            ("DELETE FROM adaptive_capture_candidates", ()),
            (
                "INSERT INTO adaptive_capture_candidates VALUES (?, ?, ?, ?, 'REFUSED', NULL, NULL,"
                " NULL, NULL, 'x', '2026-09-13 00:00:00.000000')",
                (off, request_id, SUPPLIER_KEY, revision_of_off),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, args)


def test_the_frozen_capture_and_its_candidate_read_back_after_a_restart(
    config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    with container(config, clock, shop) as app:
        request_capture(app)
        run_id = collect_once(app, clock, PRODUCT_URL)
        before = (app.collection.run(run_id).frozen, app.capture_store.candidate(run_id))
    with container(config, clock, shop) as restarted:
        after = (restarted.collection.run(run_id).frozen, restarted.capture_store.candidate(run_id))
    assert after == before


# ================================================================ the harness


def _compose(clock: FakeClock, shop: FakeGateway) -> Any:
    def compose(config: AppConfig, lease: DataDirLease) -> Container:
        return build_container(
            make_config(config.data_dir),
            ownership=lease,
            clock=clock,
            collection_gateway=shop,
            collection_sessions=StubSessions(),
            collections=(registered(),),
            adaptive_supplier_gate=build_supplier_gate({SUPPLIER_KEY}),
        )

    return compose


class Harness:
    def __init__(
        self,
        campaign_root: Path,
        data_root: Path,
        environ: dict[str, str],
        clock: FakeClock,
        shop: FakeGateway,
    ) -> None:
        self.campaign_root, self.data_root, self.environ = campaign_root, data_root, environ
        self.compose = _compose(clock, shop)

    def __call__(self, *argv: str, approve: str | None = None) -> tuple[int, Any]:
        out = io.StringIO()
        base = ["--campaign-root", str(self.campaign_root), "--data-root", str(self.data_root)]
        base += ["--actor", OPERATOR]
        if approve is not None:
            base += ["--approve", approve]
        code = run(
            [*base, *argv],
            environ=self.environ,
            compose=self.compose,
            code_sha=lambda: CODE_SHA,
            out=out,
        )
        lines = out.getvalue().strip().splitlines()
        return code, json.loads(lines[-1]) if lines else None

    def phrase(self, name: str) -> str:
        return approval_phrase(CampaignLedger(self.campaign_root.resolve()).campaign(), name)


@pytest.fixture
def harness(
    campaign_root: Path,
    config: AppConfig,
    environ: dict[str, str],
    clock: FakeClock,
    shop: FakeGateway,
) -> Harness:
    h = Harness(campaign_root, config.data_dir, environ, clock, shop)
    code, _ = h("init", "--campaign-id", CAMPAIGN, "--authorization", "5826469852")
    assert code == EXIT_OK
    return h


def test_a_data_root_command_fails_data_dir_in_use_with_no_side_effect(
    harness: Harness, config: AppConfig
) -> None:
    ledger = CampaignLedger(harness.campaign_root.resolve())
    before = (len(ledger.events()), count(config, "adaptive_capture_requests"))
    with acquire_data_dir(config.data_dir, app_version="a-live-server"):
        code, result = harness("status", "--supplier", SUPPLIER_KEY)
    assert code == EXIT_DATA_DIR_IN_USE and result["code"] == "DATA_DIR_IN_USE"
    assert (len(ledger.events()), count(config, "adaptive_capture_requests")) == before
    code, _ = harness("status", "--supplier", SUPPLIER_KEY)
    assert code == EXIT_OK, "once the application is stopped, the command runs"


def test_an_unauthorized_stage_or_a_wrong_phrase_is_refused_before_any_effect(
    harness: Harness, config: AppConfig
) -> None:
    code, result = harness(
        "request-capture",
        "--supplier",
        SUPPLIER_KEY,
        "--target-url",
        PRODUCT_URL,
        approve=harness.phrase("request-capture"),
    )
    assert code == EXIT_REFUSED and "not authorized" in result["refused"]
    harness(
        "authorize-stage",
        "--stage",
        "C1",
        "--authorization",
        "1111111",
        approve=harness.phrase("authorize-stage"),
    )
    code, result = harness(
        "request-capture",
        "--supplier",
        SUPPLIER_KEY,
        "--target-url",
        PRODUCT_URL,
        approve="I approve",
    )
    assert code == EXIT_REFUSED and "approval phrase" in result["refused"]
    assert count(config, "adaptive_capture_requests") == 0


def test_the_campaign_root_may_not_sit_inside_the_data_root(
    config: AppConfig, environ: dict[str, str], clock: FakeClock, shop: FakeGateway
) -> None:
    inside = Harness(config.data_dir / "campaign", config.data_dir, environ, clock, shop)
    code, result = inside("init", "--campaign-id", CAMPAIGN, "--authorization", "5826469852")
    assert code == EXIT_REFUSED and "overlaps the data root" in result["refused"]


def test_the_harness_runs_c1_to_c4_end_to_end_on_a_synthetic_root(
    harness: Harness,
    config: AppConfig,
    clock: FakeClock,
    shop: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for stage, authorization in (
        ("C1", "1111111"),
        ("C2", "2222222"),
        ("C3", "3333333"),
        ("C4", "4444444"),
    ):
        code, _ = harness(
            "authorize-stage",
            "--stage",
            stage,
            "--authorization",
            authorization,
            approve=harness.phrase("authorize-stage"),
        )
        assert code == EXIT_OK
    # C1: the harness leaves a capture request; the operator's ordinary collection consumes it.
    code, requested = harness(
        "request-capture",
        "--supplier",
        SUPPLIER_KEY,
        "--target-url",
        PRODUCT_URL,
        approve=harness.phrase("request-capture"),
    )
    assert code == EXIT_OK and "://" not in json.dumps(requested)
    with container(config, clock, shop) as app:
        captured_run = collect_once(app, clock, PRODUCT_URL)
        digest = save_profile(app)
        freshness = validate(app, digest)  # a PASS run for the exact freshness, synthetic
    code, candidates = harness("candidates")
    assert code == EXIT_OK and [c["status"] for c in candidates] == ["CAPTURED"]
    code, exported = harness("export-candidate", "--run", captured_run)
    exported_path = harness.campaign_root / "candidates" / exported["exported"]
    assert "<" not in exported_path.read_text("utf-8")
    scope = harness.campaign_root / "scope.json"
    scope.write_text(json.dumps({"product_boundary": "primary"}), "utf-8")
    expected = harness.campaign_root / "expected.json"
    expected.write_text(json.dumps({"original_name": "Synthetic"}), "utf-8")
    code, finalized = harness(
        "finalize-sample",
        "--run",
        captured_run,
        "--scope",
        str(scope),
        "--expected",
        str(expected),
        approve=harness.phrase("finalize-sample"),
    )
    assert code == EXIT_OK and len(finalized["sample_digest"]) == 64
    code, validated = harness(
        "validate",
        "--epr",
        digest,
        "--sample",
        finalized["sample_digest"],
        approve=harness.phrase("validate"),
    )
    assert code == EXIT_OK and validated["verdict"] in {"PASS", "FAIL", "INCOMPLETE"}
    # C2: enable the exact validated bundle and declare the window (0 reads).
    code, enabled = harness("enable", "--epr", digest, approve=harness.phrase("enable"))
    assert code == EXIT_OK, enabled
    assert freshness[0] == digest
    code, declared = harness("declare", "--epr", digest, approve=harness.phrase("declare"))
    assert code == EXIT_OK
    window = declared["window_id"]

    # C3: an ordinary collection inside the window, an unresolved mismatch, then the operator's
    # resolution through an artifact.
    def mismatch(**_: Any) -> Comparison:
        return Comparison(RunVerdict.MISMATCH, None, {"agrees": True}, {"matched": True})

    monkeypatch.setattr(shadow_runner, "compare", mismatch)
    with container(config, clock, shop) as app:
        shadowed = collect_once(app, clock, PRODUCT_URL)
    code, status = harness("status", "--supplier", SUPPLIER_KEY)
    assert code == EXIT_OK
    (pending,) = status["unresolved_mismatches"]
    assert pending["collection_run_id"] == shadowed and "resolve_before" in pending
    assert status["retention"]["nearer_bound"] in {"AGE", "COUNT"}
    code, resolved = harness(
        "resolve",
        "--run",
        shadowed,
        "--resolution",
        "ADAPTIVE_CORRECT",
        "--adaptive-failed-closed",
        "no",
        "--dimension",
        "prices",
        "--source-evidence",
        "pfr:field:prices:evidence:0",
        approve=harness.phrase("resolve"),
    )
    assert code == EXIT_OK and resolved["count_as"] == "SUCCESS"
    assert resolved["evidence_ref"].startswith(f"phase-c:{CAMPAIGN}:resolution:")
    sha = resolved["evidence_ref"].rsplit(":", 1)[1]
    assert (harness.campaign_root / "resolutions" / f"{sha}.json").is_file()
    # C4: end and close; the closeout is kept in the campaign root.
    assert harness("end", "--window", window, approve=harness.phrase("end"))[0] == EXIT_OK
    code, closeout = harness("close", "--window", window, approve=harness.phrase("close"))
    assert code == EXIT_OK and closeout["denominator"] == 1
    assert (harness.campaign_root / "closeouts" / f"{window}.json").is_file()
    kinds = [e["kind"] for e in CampaignLedger(harness.campaign_root.resolve()).events()]
    for kind in (
        "REQUEST_CAPTURE",
        "FINALIZE_SAMPLE",
        "VALIDATE",
        "ENABLE",
        "DECLARE",
        "RESOLVE",
        "END",
        "CLOSE",
    ):
        assert kind in kinds, kind


# ================================================================ migration 0027


def test_0027_is_additive_and_its_downgrade_never_destroys_capture_evidence(
    tmp_path: Path, config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    database = tmp_path / "icbm.db"
    url = f"sqlite:///{database.as_posix()}"
    command.upgrade(alembic_config(url), "0026_g3_live_authority")

    def schema() -> dict[str, str]:
        with contextlib.closing(sqlite3.connect(database)) as connection:
            found = connection.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL")
            return dict(found.fetchall())

    before = schema()
    upgrade_to_head(url)
    after = schema()
    assert {name for name in before if before[name] != after[name]} == {"collection_runs"}
    assert {"adaptive_capture_requests", "adaptive_capture_candidates"} <= set(after) - set(before)
    command.downgrade(alembic_config(url), "0026_g3_live_authority")
    assert schema() == before
    # With a request in the live root, the step down refuses.
    with container(config, clock, shop) as app:
        request_capture(app)
        live = app.db.url
    with pytest.raises(RuntimeError, match="never silently destroyed"):
        command.downgrade(alembic_config(live), "0026_g3_live_authority")
    assert rows(config, "SELECT COUNT(*) FROM adaptive_capture_requests") == [(1,)]
