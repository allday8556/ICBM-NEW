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
- review ``5313663701``: every command runs only at the campaign's exact clean SHA (B1); stages
  open only by typed grants in order and the C1 ceilings are reserved and enforced (B2); a
  campaign reaches none of another campaign's objects (B3); one writer per campaign, and an
  unfinished action stops the campaign fail-closed (B4);
- migration 0027 is additive and its downgrade never destroys capture evidence.
"""

import contextlib
import io
import json
import shutil
import socket
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command

import app.collect.adaptive_capture.runner as capture_runner
import app.collect.adaptive_shadow.runner as shadow_runner
import scripts.phasec.harness as harness_module
from app.collect.adaptive.validation import ValidationRun, Verdict, freshness_tuple
from app.collect.adaptive_capture.store import CaptureStore
from app.collect.adaptive_shadow.compare import Comparison
from app.collect.adaptive_shadow.evidence import RunVerdict
from app.collect.adaptive_shadow.switch import bundle_key_of
from app.collect.adaptive_store.gate import build_supplier_gate
from app.collect.collection import pacing_key
from app.collect.models import CollectionOutcome
from app.config import AppConfig, database_path
from app.container import Container, build_container
from app.core.errors import InputValidationError
from app.core.ownership import DataDirLease, acquire_data_dir
from app.db.migrate import alembic_config, upgrade_to_head
from scripts.m3collect.fake_shop import PRODUCT_URL, SUPPLIER_KEY, FakeGateway, StubSessions, page
from scripts.phasec.ceilings import CEILINGS
from scripts.phasec.grants import GRANT_SCHEMA, grant_digest
from scripts.phasec.harness import (
    EXIT_CAMPAIGN_IN_USE,
    EXIT_DATA_DIR_IN_USE,
    EXIT_OK,
    EXIT_REFUSED,
    run,
)
from scripts.phasec.ledger import CampaignLedger, LedgerRefused, approval_phrase
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
        with pytest.raises(InputValidationError, match="its own capture"):
            app.capture_store.finalize(  # review 5313663701 B3: owner-level campaign binding
                run_id,
                campaign_id="phase-c-another-campaign",
                scope=scope,
                expected={"original_name": "A"},
                stored_by=OPERATOR,
                correlation_id="c",
            )
        assert app.capture_store.candidate(run_id).campaign_id == CAMPAIGN
        first = app.capture_store.finalize(
            run_id,
            campaign_id=CAMPAIGN,
            scope=scope,
            expected={"original_name": "A"},
            stored_by=OPERATOR,
            correlation_id="c",
        )
        corrected = app.capture_store.finalize(
            run_id,
            campaign_id=CAMPAIGN,
            scope=scope,
            expected={"original_name": "B"},
            stored_by=OPERATOR,
            correlation_id="c",
        )
        assert first.digest != corrected.digest, "a correction is a new sample"
        candidate = app.capture_store.candidate(run_id).candidate
        assert candidate is not None
        for sample in (first, corrected):
            assert app.adaptive_validation.sample(sample.digest) == sample
            assert sample.provenance["candidate"] == candidate.digest
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

C1_AUTH, C2_AUTH, C3_AUTH, C4_AUTH = (
    "issuecomment-5900000001",
    "issuecomment-5900000002",
    "issuecomment-5900000003",
    "issuecomment-5900000004",
)
OTHER_TARGET = "f" * 64


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
        campaign_id: str = CAMPAIGN,
    ) -> None:
        self.campaign_root, self.data_root, self.environ = campaign_root, data_root, environ
        self.campaign_id = campaign_id
        self.compose = _compose(clock, shop)

    def __call__(
        self, *argv: str, approve: str | None = None, code_sha: Callable[[], str] | None = None
    ) -> tuple[int, Any]:
        out = io.StringIO()
        base = ["--campaign-root", str(self.campaign_root), "--data-root", str(self.data_root)]
        base += ["--actor", OPERATOR]
        if approve is not None:
            base += ["--approve", approve]
        code = run(
            [*base, *argv],
            environ=self.environ,
            compose=self.compose,
            code_sha=code_sha or (lambda: CODE_SHA),
            out=out,
        )
        lines = out.getvalue().strip().splitlines()
        return code, json.loads(lines[-1]) if lines else None

    def init(self) -> None:
        code, result = self("init", "--campaign-id", self.campaign_id, "--authorization", C0)
        assert code == EXIT_OK, result

    def ledger(self) -> CampaignLedger:
        return CampaignLedger(self.campaign_root.resolve())

    def phrase(self, name: str) -> str:
        return approval_phrase(self.ledger().campaign(), name)

    def act(self, name: str, *argv: str) -> tuple[int, Any]:
        return self(name, *argv, approve=self.phrase(name))

    def write_grant(self, stage: str, authorization: str, **scope: Any) -> tuple[Path, str]:
        grant = {
            "schema": GRANT_SCHEMA,
            "campaign_id": self.campaign_id,
            "code_sha": CODE_SHA,
            "stage": stage,
            "authorization": authorization,
            "scope": {"ceilings": dict(CEILINGS[stage]), **scope},
        }
        path = self.campaign_root.parent / f"{self.campaign_id}-{stage}-{authorization}.json"
        path.write_text(json.dumps(grant), "utf-8")
        subject = f"{stage} GRANT {grant_digest(grant)[:16]}"
        return path, approval_phrase(self.ledger().campaign(), "authorize-stage", subject)

    def authorize(self, stage: str, authorization: str, **scope: Any) -> tuple[int, Any]:
        path, phrase = self.write_grant(stage, authorization, **scope)
        return self("authorize-stage", "--grant", str(path), approve=phrase)

    def target(self) -> str:
        code, result = self(
            "target-digest", "--supplier", SUPPLIER_KEY, "--target-url", PRODUCT_URL
        )
        assert code == EXIT_OK, result
        return str(result["target_digest"])

    def request(self) -> tuple[int, Any]:
        return self.act("request-capture", "--supplier", SUPPLIER_KEY, "--target-url", PRODUCT_URL)

    def finalize(self, run_id: str) -> tuple[int, Any]:
        scope = self.campaign_root.parent / f"{self.campaign_id}-scope.json"
        scope.write_text(json.dumps({"product_boundary": "primary"}), "utf-8")
        expected = self.campaign_root.parent / f"{self.campaign_id}-expected.json"
        expected.write_text(json.dumps({"original_name": "Synthetic"}), "utf-8")
        return self.act(
            "finalize-sample", "--run", run_id, "--scope", str(scope), "--expected", str(expected)
        )


C0 = "issuecomment-5826469852"


def refused(result: tuple[int, Any], why: str) -> None:
    code, body = result
    assert code == EXIT_REFUSED and why in body["refused"], body


@pytest.fixture
def harness(
    campaign_root: Path,
    config: AppConfig,
    environ: dict[str, str],
    clock: FakeClock,
    shop: FakeGateway,
) -> Harness:
    h = Harness(campaign_root, config.data_dir, environ, clock, shop)
    h.init()
    return h


@pytest.fixture
def passing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PASS ValidationRun for the exact freshness, as the synthetic V1–V8 cannot give over one
    fake-shop sample; everything the harness does around it is real."""

    def passing_run(bundle: Any, samples: Any, negatives: Any) -> ValidationRun:
        return ValidationRun(Verdict.PASS, (), freshness_tuple(bundle, samples, None))

    monkeypatch.setattr(harness_module, "validate", passing_run)


@dataclass(frozen=True)
class C1:
    run_id: str
    sample: str
    epr: str
    validation: str


def c1_scope(target_digest: str) -> dict[str, Any]:
    return {"supplier_key": SUPPLIER_KEY, "target_digests": sorted([target_digest, OTHER_TARGET])}


def drive_c1(h: Harness, config: AppConfig, clock: FakeClock, shop: FakeGateway) -> C1:
    """C1 on a synthetic root: the grant, a capture request, the ordinary collection that consumes
    it, the sample, and a PASS validation, all through the harness except the collection."""
    code, result = h.authorize("C1", C1_AUTH, **c1_scope(h.target()))
    assert code == EXIT_OK, result
    code, requested = h.request()
    assert code == EXIT_OK and "://" not in json.dumps(requested), requested
    with container(config, clock, shop) as app:
        run_id = collect_once(app, clock, PRODUCT_URL)
        epr = save_profile(app)
    code, finalized = h.finalize(run_id)
    assert code == EXIT_OK, finalized
    code, validated = h.act("validate", "--epr", epr, "--sample", finalized["sample_digest"])
    assert code == EXIT_OK and validated["verdict"] == "PASS", validated
    return C1(run_id, finalized["sample_digest"], epr, validated["run_id"])


def c2_scope(c1: C1) -> dict[str, Any]:
    return {
        "supplier_key": SUPPLIER_KEY,
        "epr_digest": c1.epr,
        "sample_digests": [c1.sample],
        "validation_run_id": c1.validation,
        "window_min_size": 3,
    }


def drive_c2(h: Harness, c1: C1, authorization: str = C2_AUTH) -> str:
    code, result = h.authorize("C2", authorization, **c2_scope(c1))
    assert code == EXIT_OK, result
    code, enabled = h.act("enable", "--epr", c1.epr)
    assert code == EXIT_OK, enabled
    code, declared = h.act("declare", "--epr", c1.epr)
    assert code == EXIT_OK, declared
    return str(declared["window_id"])


def mismatch(**_: Any) -> Comparison:
    return Comparison(RunVerdict.MISMATCH, None, {"agrees": True}, {"matched": True})


def resolve(h: Harness, run_id: str) -> tuple[int, Any]:
    return h.act(
        "resolve",
        "--run",
        run_id,
        "--resolution",
        "ADAPTIVE_CORRECT",
        "--adaptive-failed-closed",
        "no",
        "--dimension",
        "prices",
        "--source-evidence",
        "pfr:field:prices:evidence:0",
    )


def test_a_data_root_command_fails_data_dir_in_use_with_no_side_effect(
    harness: Harness, config: AppConfig
) -> None:
    ledger = harness.ledger()
    before = (ledger.path.read_bytes(), count(config, "adaptive_capture_requests"))
    with acquire_data_dir(config.data_dir, app_version="a-live-server"):
        code, result = harness("status", "--supplier", SUPPLIER_KEY)
    assert code == EXIT_DATA_DIR_IN_USE and result["code"] == "DATA_DIR_IN_USE"
    assert (ledger.path.read_bytes(), count(config, "adaptive_capture_requests")) == before
    code, _ = harness("status", "--supplier", SUPPLIER_KEY)
    assert code == EXIT_OK, "once the application is stopped, the command runs"


def test_every_command_runs_only_at_the_campaigns_exact_clean_code_sha(
    harness: Harness, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 5313663701 B1: another SHA or an unclean checkout is refused before the lock, the
    # lease, the owners, any artifact and any ledger event.
    path, phrase = harness.write_grant("C1", C1_AUTH, **c1_scope("e" * 64))
    before = harness.ledger().path.read_bytes()

    def never(*_: Any, **__: Any) -> Any:
        raise AssertionError("nothing is leased or composed at another SHA")

    monkeypatch.setattr(harness_module, "acquire_data_dir", never)
    monkeypatch.setattr(harness, "compose", never)

    def unclean() -> str:
        raise LedgerRefused("1 tracked change", code="PHASE_C_CHECKOUT_UNCLEAN")

    for argv, approve in (
        (("status", "--supplier", SUPPLIER_KEY), None),
        (("authorize-stage", "--grant", str(path)), phrase),
        (("request-capture", "--supplier", SUPPLIER_KEY, "--target-url", PRODUCT_URL), "x"),
    ):
        code, result = harness(*argv, approve=approve, code_sha=lambda: "d" * 40)
        assert code == EXIT_REFUSED and "exact code SHA" in result["refused"], result
        code, result = harness(*argv, approve=approve, code_sha=unclean)
        assert code == EXIT_REFUSED and result["code"] == "PHASE_C_CHECKOUT_UNCLEAN", result
    assert harness.ledger().path.read_bytes() == before
    assert count(config, "adaptive_capture_requests") == 0


def test_a_second_command_on_the_same_campaign_fails_campaign_in_use(harness: Harness) -> None:
    before = harness.ledger().path.read_bytes()
    with harness.ledger().writer():
        code, result = harness("status", "--supplier", SUPPLIER_KEY)
    assert code == EXIT_CAMPAIGN_IN_USE and result["code"] == "CAMPAIGN_IN_USE"
    assert harness.ledger().path.read_bytes() == before
    assert harness("status", "--supplier", SUPPLIER_KEY)[0] == EXIT_OK


def test_stages_open_only_by_typed_grants_once_each_and_in_order(
    harness: Harness, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review 5313663701 B2: a stage is a typed grant, not a numeric self-assertion.
    target = harness.target()
    refused(harness.request(), "current stage is C0")
    refused(
        harness.authorize("C2", C1_AUTH, **c2_scope(C1("r", "s" * 64, "e" * 64, "v"))),
        "next stage of this campaign is C1",
    )
    refused(
        harness.authorize("C1", "issuecomment-1111111", **c1_scope(target)),
        "newer than every grant",
    )
    refused(
        harness.authorize("C1", C1_AUTH, supplier_key=SUPPLIER_KEY, target_digests=[target]),
        "exactly 2 distinct sorted target digests",
    )
    refused(
        harness.authorize(
            "C1",
            C1_AUTH,
            **c1_scope(target),
            ceilings={**CEILINGS["C1"], "product_reads": 40},
        ),
        "frozen C1 ceilings",
    )
    refused(harness.authorize("C1", C1_AUTH, **c1_scope(target), extra=True), "scope holds")
    path, _ = harness.write_grant("C1", C1_AUTH, **c1_scope(target))
    refused(
        harness("authorize-stage", "--grant", str(path), approve=harness.phrase("authorize-stage")),
        "approval phrase",
    )
    # The grant file is read once: the phrase is checked against, and the ledger records, it.
    reads: list[Path] = []
    real_load = harness_module._load_grant

    def counted(grant_path: Path) -> Any:
        reads.append(grant_path)
        return real_load(grant_path)

    monkeypatch.setattr(harness_module, "_load_grant", counted)
    assert harness.ledger().campaign().current_stage == "C0"
    code, result = harness.authorize("C1", C1_AUTH, **c1_scope(target))
    assert code == EXIT_OK, result
    assert len(reads) == 1, reads
    refused(harness.authorize("C1", "issuecomment-5900000009", **c1_scope(target)), "next stage")
    code, result = harness(
        "request-capture", "--supplier", SUPPLIER_KEY, "--target-url", PRODUCT_URL, approve="no"
    )
    assert code == EXIT_REFUSED and "approval phrase" in result["refused"]
    campaign = harness.ledger().campaign()
    assert campaign.current_stage == "C1" and campaign.authorized == {"C0": C0, "C1": C1_AUTH}
    assert campaign.grants["C1"].scope["target_digests"] == sorted([target, OTHER_TARGET])
    assert count(config, "adaptive_capture_requests") == 0


def test_the_c1_grant_bounds_every_capture_request_by_its_frozen_ceilings(
    harness: Harness, config: AppConfig, clock: FakeClock, shop: FakeGateway
) -> None:
    target = harness.target()
    assert harness.authorize("C1", C1_AUTH, **c1_scope(target))[0] == EXIT_OK
    assert (
        harness.act(
            "request-capture", "--supplier", "another-supplier", "--target-url", PRODUCT_URL
        )[0]
        == EXIT_REFUSED
    )
    code, first = harness.request()
    assert code == EXIT_OK, first
    refused(harness.request(), "requested once")
    counts = harness.ledger().counts("C1")
    assert (counts["collection_submissions"], counts["target_identities"]) == (1, 1)
    assert count(config, "adaptive_capture_requests") == 1
    # An owner refusal answers its intent, and the spent reservation stays spent.
    other = Harness(
        harness.campaign_root.parent / "second",
        harness.data_root,
        harness.environ,
        clock,
        shop,
        campaign_id="phase-c-synthetic-second",
    )
    other.init()
    assert other.authorize("C1", C1_AUTH, **c1_scope(target))[0] == EXIT_OK
    code, result = other.act(
        "request-capture",
        "--supplier",
        SUPPLIER_KEY,
        "--target-url",
        PRODUCT_URL,
        "--lifetime-hours",
        "48",
    )
    assert code == EXIT_REFUSED and result["code"] == "ADAPTIVE_CAPTURE_REFUSED", result
    kinds = [e["kind"] for e in other.ledger().events()]
    assert kinds[-2:] == ["INTENDED", "ACTION_REFUSED"]
    assert other.ledger().campaign().unfinished() == []
    refused(other.request(), "requested once")
    assert count(config, "adaptive_capture_requests") == 1


def test_the_c1_grant_refuses_a_target_it_does_not_name(
    harness: Harness, config: AppConfig
) -> None:
    assert (
        harness.authorize(
            "C1", C1_AUTH, supplier_key=SUPPLIER_KEY, target_digests=["e" * 64, OTHER_TARGET]
        )[0]
        == EXIT_OK
    )
    refused(harness.request(), "a target the C1 grant names")
    assert harness.ledger().counts("C1")["collection_submissions"] == 0
    assert count(config, "adaptive_capture_requests") == 0


def test_an_unfinished_action_stops_the_campaign_fail_closed(
    harness: Harness, config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert harness.authorize("C1", C1_AUTH, **c1_scope(harness.target()))[0] == EXIT_OK

    def crash(*_: Any, **__: Any) -> str:
        raise RuntimeError("the process died between the intent and the outcome")

    monkeypatch.setattr(CaptureStore, "request", crash)
    with pytest.raises(RuntimeError):
        harness.request()
    monkeypatch.undo()
    code, status = harness("status", "--supplier", SUPPLIER_KEY)
    assert code == EXIT_OK and len(status["unfinished_actions"]) == 1
    refused(harness.request(), "unfinished action")
    assert count(config, "adaptive_capture_requests") == 0


def test_the_harness_runs_c1_to_c4_end_to_end_on_a_synthetic_root(
    harness: Harness,
    config: AppConfig,
    clock: FakeClock,
    shop: FakeGateway,
    monkeypatch: pytest.MonkeyPatch,
    passing: None,
) -> None:
    c1 = drive_c1(harness, config, clock, shop)
    code, candidates = harness("candidates")
    assert code == EXIT_OK and [c["status"] for c in candidates] == ["CAPTURED"]
    code, exported = harness("export-candidate", "--run", c1.run_id)
    assert code == EXIT_OK, exported
    exported_path = harness.campaign_root / "candidates" / exported["exported"]
    assert "<" not in exported_path.read_text("utf-8")
    refused(harness.finalize(c1.run_id), "one approved sample per target run")
    window = drive_c2(harness, c1)
    bundle = bundle_key_of(c1.epr)
    code, result = harness.authorize(
        "C3", C3_AUTH, supplier_key=SUPPLIER_KEY, window_id=window, bundle_key=bundle
    )
    assert code == EXIT_OK, result
    # C3: an ordinary collection inside the window, an unresolved mismatch, then the operator's
    # resolution through an artifact.
    monkeypatch.setattr(shadow_runner, "compare", mismatch)
    with container(config, clock, shop) as app:
        shadowed = collect_once(app, clock, PRODUCT_URL)
    code, status = harness("status", "--supplier", SUPPLIER_KEY)
    assert code == EXIT_OK and status["current_stage"] == "C3"
    (pending,) = status["unresolved_mismatches"]
    assert pending["collection_run_id"] == shadowed and "resolve_before" in pending
    assert status["retention"]["nearer_bound"] in {"AGE", "COUNT"}
    code, resolved = resolve(harness, shadowed)
    assert code == EXIT_OK and resolved["count_as"] == "SUCCESS", resolved
    assert resolved["evidence_ref"].startswith(f"phase-c:{CAMPAIGN}:resolution:")
    sha = resolved["evidence_ref"].rsplit(":", 1)[1]
    assert (harness.campaign_root / "resolutions" / f"{sha}.json").is_file()
    # C4: end and close the same window; the closeout is kept in the campaign root.
    code, result = harness.authorize("C4", C4_AUTH, supplier_key=SUPPLIER_KEY, window_id=window)
    assert code == EXIT_OK, result
    assert harness.act("end", "--window", window)[0] == EXIT_OK
    code, closeout = harness.act("close", "--window", window)
    assert code == EXIT_OK and closeout["denominator"] == 1
    assert (harness.campaign_root / "closeouts" / f"{window}.json").is_file()
    campaign = harness.ledger().campaign()
    assert campaign.unfinished() == [] and campaign.current_stage == "C4"
    kinds = [e["kind"] for e in campaign.events]
    for kind in ("REQUEST_CAPTURE", "FINALIZE_SAMPLE", "VALIDATE", "ENABLE", "DECLARE"):
        assert kind in kinds, kind
    assert kinds[-2:] == ["INTENDED", "CLOSE"]
    assert harness("status", "--supplier", SUPPLIER_KEY)[0] == EXIT_OK, "it reads back"


def test_a_campaign_never_acts_on_another_campaigns_evidence(
    harness: Harness,
    config: AppConfig,
    environ: dict[str, str],
    clock: FakeClock,
    shop: FakeGateway,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    passing: None,
) -> None:
    # Review 5313663701 B3: campaign B, on the same data root, can reach none of campaign A's
    # candidates, samples, validation, bundle, window or runs, though each is globally valid.
    a = harness
    b = Harness(
        tmp_path_factory.mktemp("campaign-b") / "campaign",
        config.data_dir,
        environ,
        clock,
        shop,
        campaign_id="phase-c-synthetic-b",
    )
    b.init()
    ca = drive_c1(a, config, clock, shop)
    assert b.authorize("C1", C1_AUTH, **c1_scope(b.target()))[0] == EXIT_OK
    assert b.request()[0] == EXIT_OK
    with container(config, clock, shop) as app:
        run_b = collect_once(app, clock, PRODUCT_URL)
    refused(b("export-candidate", "--run", ca.run_id), "its own requests")
    refused(b.finalize(ca.run_id), "its own requests")
    code, finalized = b.finalize(run_b)
    assert code == EXIT_OK, finalized
    sample_b = finalized["sample_digest"]
    refused(b.act("validate", "--epr", ca.epr, "--sample", ca.sample), "finalized itself")
    code, validated = b.act("validate", "--epr", ca.epr, "--sample", sample_b)
    assert code == EXIT_OK and validated["verdict"] == "PASS"
    cb = C1(run_b, sample_b, ca.epr, validated["run_id"])
    # B's C2 grant cannot name A's samples and validation run.
    refused(b.authorize("C2", C2_AUTH, **c2_scope(ca)), "this campaign's own C1 recorded")
    assert b.ledger().campaign().current_stage == "C1"
    # A runs its window; B cannot take it as its own C3.
    window_a = drive_c2(a, ca)
    bundle = bundle_key_of(ca.epr)
    assert (
        a.authorize(
            "C3", C3_AUTH, supplier_key=SUPPLIER_KEY, window_id=window_a, bundle_key=bundle
        )[0]
        == EXIT_OK
    )
    monkeypatch.setattr(shadow_runner, "compare", mismatch)
    with container(config, clock, shop) as app:
        run_a = collect_once(app, clock, PRODUCT_URL)
    assert resolve(a, run_a)[0] == EXIT_OK
    assert a.authorize("C4", C4_AUTH, supplier_key=SUPPLIER_KEY, window_id=window_a)[0] == EXIT_OK
    assert a.act("end", "--window", window_a)[0] == EXIT_OK
    # B: its own C2 and window, and then none of A's.
    assert b.authorize("C2", C2_AUTH, **c2_scope(cb))[0] == EXIT_OK
    refused(b.act("enable", "--epr", "0" * 64), "exactly the EPR")
    assert b.act("enable", "--epr", cb.epr)[0] == EXIT_OK
    refused(b.act("declare", "--epr", cb.epr, "--supersedes", window_a), "declared itself")
    code, declared = b.act("declare", "--epr", cb.epr)
    assert code == EXIT_OK, declared
    window_b = declared["window_id"]
    refused(
        b.authorize(
            "C3", C3_AUTH, supplier_key=SUPPLIER_KEY, window_id=window_a, bundle_key=bundle
        ),
        "own C2 declared",
    )
    assert (
        b.authorize(
            "C3", C3_AUTH, supplier_key=SUPPLIER_KEY, window_id=window_b, bundle_key=bundle
        )[0]
        == EXIT_OK
    )
    refused(resolve(b, run_a), "its own C3 window")
    refused(
        b.authorize("C4", C4_AUTH, supplier_key=SUPPLIER_KEY, window_id=window_a), "same window"
    )
    assert b.authorize("C4", C4_AUTH, supplier_key=SUPPLIER_KEY, window_id=window_b)[0] == EXIT_OK
    refused(b.act("end", "--window", window_a), "its own C4 grant")
    refused(b.act("close", "--window", window_a), "its own C4 grant")
    # A's window was closable by A all along.
    code, closeout = a.act("close", "--window", window_a)
    assert code == EXIT_OK and closeout["denominator"] == 1, closeout
    assert b.ledger().campaign().unfinished() == [] and a.ledger().campaign().unfinished() == []


def test_the_campaign_root_may_not_sit_inside_the_data_root(
    config: AppConfig, environ: dict[str, str], clock: FakeClock, shop: FakeGateway
) -> None:
    inside = Harness(config.data_dir / "campaign", config.data_dir, environ, clock, shop)
    code, result = inside("init", "--campaign-id", CAMPAIGN, "--authorization", C0)
    assert code == EXIT_REFUSED and "overlaps the data root" in result["refused"]


def test_a_campaign_is_created_only_under_the_pinned_c0_authorization(
    campaign_root: Path,
    config: AppConfig,
    environ: dict[str, str],
    clock: FakeClock,
    shop: FakeGateway,
) -> None:
    h = Harness(campaign_root, config.data_dir, environ, clock, shop)
    code, result = h(
        "init", "--campaign-id", CAMPAIGN, "--authorization", "issuecomment-5826469853"
    )
    assert code == EXIT_REFUSED and "C0 authorization issuecomment-5826469852" in result["refused"]
    assert not (campaign_root / "campaign.sqlite3").exists()


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
