"""Gate 3 area 3 (ADR-0018 §9): the reviewed populated visual acceptance record.

Provider-zero. ``VISUAL_ACCEPTANCE_RECORDED`` is proven only from a reviewed record of exactly the
running code at exactly the schema head, recorded only through the verified report path; a passing
run alone, a stale or foreign report, or any HTTP route records nothing.
"""

import copy
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.config import AppConfig
from app.container import Container
from app.core.code_identity import (
    checkout_sha,
    code_digest,
    running_checkout_sha,
    running_code_digest,
)
from app.core.errors import InputValidationError
from app.db.migrate import head_revision
from app.live import model as live_model
from app.live import visual
from app.live.store import LiveAuthorityStore
from app.live.visual import VisualAcceptanceService
from app.main import create_app
from tests.conftest import LOCAL
from tests.gate1_support import OPERATOR
from tests.visual_support import reseal, sealed

pytestmark = pytest.mark.integration

REVIEW = "5843380581"
CID = "corr-g3c"


@pytest.fixture
def api(config: AppConfig) -> Iterator[TestClient]:
    with TestClient(create_app(config), base_url=LOCAL) as client:
        yield client


@pytest.fixture
def container(api: TestClient) -> Container:
    served: Container = api.app.state.container
    return served


def current(container: Container, **overrides: Any) -> dict[str, Any]:
    head = head_revision()
    assert head is not None
    sha = running_checkout_sha()
    assert sha is not None, "the tests run from a git checkout"
    values: dict[str, Any] = {
        "code_sha": sha,
        "code_digest": running_code_digest(container.config.ui_dir),
        "schema_head": head,
    }
    values.update(overrides)
    return sealed(**values)


def record(container: Container, report: dict[str, Any], **overrides: Any) -> str:
    values: dict[str, Any] = {
        "approved_by": "architect",
        "authorization_ref": REVIEW,
        "actor": OPERATOR,
        "correlation_id": CID,
    }
    values.update(overrides)
    return container.visual_acceptance.record(report, **values)


def rows(config: AppConfig) -> int:
    with sqlite3.connect(config.data_dir / "runtime" / "icbm.db") as raw:
        return int(raw.execute("SELECT COUNT(*) FROM visual_acceptances").fetchone()[0])


# ---------------------------------------------------------------- the code identity


def _tree(root: Path) -> Path:
    (root / "app" / "core").mkdir(parents=True)
    (root / "app" / "core" / "x.py").write_bytes(b"VALUE = 1\n")
    (root / "integrations").mkdir()
    (root / "integrations" / "y.py").write_bytes(b"OTHER = 2\n")
    (root / "ui" / "web" / "js").mkdir(parents=True)
    (root / "ui" / "web" / "js" / "page.js").write_bytes(b"export default 1;\n")
    (root / "ui" / "web" / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "pyproject.toml").write_bytes(b"[project]\n")
    (root / "docs").mkdir()
    return root / "ui" / "web"


def test_the_code_identity_is_exactly_the_executed_and_served_code(tmp_path: Path) -> None:
    ui = _tree(tmp_path)
    before = code_digest(ui, tmp_path)
    assert code_digest(ui, tmp_path) == before
    # Documents, evidence and bytecode are not code the application runs: nothing moves.
    (tmp_path / "docs" / "acceptance.md").write_text("evidence")
    (tmp_path / "app" / "core" / "__pycache__").mkdir()
    (tmp_path / "app" / "core" / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\x00")
    (tmp_path / "app" / "core" / "x.pyc").write_bytes(b"\x00")
    assert code_digest(ui, tmp_path) == before
    # A checkout that converts line endings is still the same code; a binary is hashed as it is.
    (tmp_path / "app" / "core" / "x.py").write_bytes(b"VALUE = 1\r\n")
    assert code_digest(ui, tmp_path) == before
    # Any executed, served or pinned change is new code.
    for path, content in (
        (tmp_path / "app" / "core" / "x.py", b"VALUE = 2\n"),
        (tmp_path / "integrations" / "y.py", b"OTHER = 3\n"),
        (ui / "js" / "page.js", b"export default 2;\n"),
        (ui / "icon.png", b"\x89PNG\n\x1a\n"),
        (tmp_path / "pyproject.toml", b"[project]\nname = 'x'\n"),
    ):
        original = path.read_bytes()
        path.write_bytes(content)
        assert code_digest(ui, tmp_path) != before, path
        path.write_bytes(original)
    assert code_digest(ui, tmp_path) == before
    (ui / "js" / "new.js").write_bytes(b"1;\n")
    assert code_digest(ui, tmp_path) != before


# ---------------------------------------------------------------- the report contract


def test_a_complete_sealed_passing_report_satisfies_the_contract(container: Container) -> None:
    assert visual.verify_report(current(container)) == []


def _without(report: dict[str, Any], target: str, viewport: str) -> dict[str, Any]:
    changed = copy.deepcopy(report)
    changed["results"] = [
        r for r in changed["results"] if (r["target"], r["viewport"]) != (target, viewport)
    ]
    return reseal(changed)


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (
            lambda r: _without(r, "register", "portrait-1080x1920"),
            "result_missing:register@portrait-1080x1920",
        ),
        (
            lambda r: _without(r, "settings-metadata", "landscape-1920x1080"),
            "result_missing:settings-metadata@landscape-1920x1080",
        ),
        (
            lambda r: reseal({**r, "viewports": {"landscape-1920x1080": [1920, 1080]}}),
            "viewport_missing:portrait-1080x1920",
        ),
        (
            lambda r: reseal(
                {**r, "viewports": {**r["viewports"], "portrait-1080x1920": [1080, 1080]}}
            ),
            "viewport_missing:portrait-1080x1920",
        ),
        (
            lambda r: reseal({**r, "results": [*r["results"], r["results"][0]]}),
            "result_missing:collect@landscape-1920x1080",
        ),
        (lambda r: reseal({**r, "external_request_count": 1}), "external_requests"),
        (lambda r: reseal({**r, "console_errors": ["TypeError"]}), "console_or_page_errors"),
        (lambda r: reseal({**r, "page_errors": ["ReferenceError"]}), "console_or_page_errors"),
        (lambda r: reseal({**r, "server_external_attempts": 1}), "server_egress"),
        (lambda r: reseal({**r, "checkout_clean": False}), "checkout_not_clean"),
        (
            lambda r: reseal({**r, "state_selectors": list(visual.STATE_SELECTORS[:-1])}),
            "state_selectors_not_the_contract",
        ),
        (lambda r: reseal({**r, "verdict": "FAILED"}), "verdict_not_passed"),
        (lambda r: reseal({**r, "browser": "https://example.invalid/x"}), "unsanitized"),
        (lambda r: {**r, "verdict": "PASSED", "browser": "tampered"}, "digest_mismatch"),
        (lambda r: reseal({**r, "code_sha": "xyz"}), "code_sha_missing"),
    ],
)
def test_a_report_short_of_the_contract_is_refused(
    container: Container, mutate: Any, problem: str
) -> None:
    assert problem in visual.verify_report(mutate(current(container)))


def test_a_hidden_truncated_or_covered_blocker_fails_the_contract(container: Container) -> None:
    report = current(container)
    for check in ("state_visible", "state_not_truncated", "state_not_covered"):
        changed = copy.deepcopy(report)
        for item in changed["results"]:
            if item["target"] == "register" and item["viewport"] == "portrait-1080x1920":
                item["checks"][check] = {
                    "passed": False,
                    "failures": ['.register-preflight[data-preflight] [data-reason="X"]'],
                }
        assert f"check_failed:{check}:register@portrait-1080x1920" in visual.verify_report(
            reseal(changed)
        )
    # A check the run silently dropped is as missing as a failed one.
    dropped = copy.deepcopy(report)
    del dropped["results"][0]["checks"]["no_horizontal_overflow"]
    assert "check_missing:no_horizontal_overflow:collect@landscape-1920x1080" in (
        visual.verify_report(reseal(dropped))
    )
    # An unpopulated screen never passes: the required server-owned state must have rendered.
    empty = copy.deepcopy(report)
    for item in empty["results"]:
        if item["target"] == "register":
            item["populated"]['[data-role="live-brake"][data-brake-state]'] = 0
    assert "not_populated:register:register@landscape-1920x1080" in visual.verify_report(
        reseal(empty)
    )


def test_the_contract_pins_the_required_surfaces_and_viewports() -> None:
    assert dict(visual.REQUIRED_VIEWPORTS) == {
        "landscape-1920x1080": (1920, 1080),
        "portrait-1080x1920": (1080, 1920),
    }
    assert {t.name for t in visual.REQUIRED_TARGETS} == {
        "collect",
        "db",
        "register",
        "dashboard",
        "soldout",
        "settings-policy",
        "settings-metadata",
    }
    register = next(t for t in visual.REQUIRED_TARGETS if t.name == "register")
    for state in ("data-preflight", "data-scope-state", "data-brake-state", "data-grant-state"):
        assert any(state in selector for selector in register.populated), state


# ---------------------------------------------------------------- recording and readiness


def test_only_a_reviewed_report_of_the_running_code_proves_visual_acceptance(
    container: Container, config: AppConfig
) -> None:
    proofs = container.safety_stack._proofs  # the production stage proofs the stack reads
    assert not proofs.visual_acceptance_recorded()
    acceptance_id = record(container, current(container))
    assert acceptance_id and rows(config) == 1
    assert proofs.visual_acceptance_recorded()
    assert container.visual_acceptance.recorded()
    with LiveAuthorityStore(container.db, container.clock, container.audit).reading() as unit:
        (recorded,) = unit.visual_acceptances()
    assert recorded["code_sha"] == running_checkout_sha()
    assert recorded["authorization_ref"] == REVIEW

    def service(**overrides: Any) -> VisualAcceptanceService:
        values: dict[str, Any] = {
            "code_sha": running_checkout_sha,
            "code_identity": lambda: running_code_digest(container.config.ui_dir),
            "schema_head": head_revision,
        }
        values.update(overrides)
        return VisualAcceptanceService(
            store=LiveAuthorityStore(container.db, container.clock, container.audit), **values
        )

    assert service().recorded()
    # ADR-0018 §9: a new accepted code SHA — a documents-, tests- or harness-only commit included,
    # which leaves the running code digest unchanged — is not proven by the older record.
    assert not service(code_sha=lambda: "e" * 40).recorded()
    # No readable commit: never recorded.
    assert not service(code_sha=lambda: None).recorded()
    # Other executed or served code at the same commit (a working-tree change) is not proven.
    assert not service(code_identity=lambda: "b" * 64).recorded()
    # Another schema head is not proven.
    assert not service(schema_head=lambda: "0031_other_head").recorded()
    # The record is append-only.
    with sqlite3.connect(config.data_dir / "runtime" / "icbm.db") as raw:
        for statement in (
            "UPDATE visual_acceptances SET code_digest = 'c'",
            "DELETE FROM visual_acceptances",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                raw.execute(statement)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"report": {"code_digest": "b" * 64}}, live_model.VISUAL_CODE_NOT_CURRENT),
        (
            {"report": {"schema_head": "0029_g3_restore_retention"}},
            live_model.VISUAL_SCHEMA_NOT_CURRENT,
        ),
        ({"report": {"code_sha": "c" * 40}}, live_model.VISUAL_COMMIT_NOT_CHECKED_OUT),
        ({"record": {"authorization_ref": "looks fine"}}, live_model.VISUAL_REVIEW_MISSING),
        ({"record": {"approved_by": ""}}, live_model.VISUAL_REVIEW_MISSING),
        ({"report": {"verdict": "FAILED"}}, live_model.VISUAL_REPORT_INVALID),
    ],
)
def test_a_foreign_stale_unreviewed_or_failed_report_is_never_recorded(
    container: Container, config: AppConfig, change: dict[str, Any], code: str
) -> None:
    report = current(container, **change.get("report", {}))
    with pytest.raises(InputValidationError) as refused:
        record(container, report, **change.get("record", {}))
    assert refused.value.code == code
    assert rows(config) == 0
    assert not container.visual_acceptance.recorded()


def test_the_stack_reads_the_record_yet_every_stage_stays_blocked(
    api: TestClient, container: Container, config: AppConfig
) -> None:
    from tests.integration.test_g3b_restore_retention import asset_grant, durable_unit

    record(container, current(container))
    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    missing = set(container.asset_uploads.readiness(grant_id).missing)
    assert live_model.VISUAL_UNRECORDED not in missing
    assert {
        live_model.MODE_NOT_LIVE,
        live_model.ELIGIBILITY_UNPROVEN,
        live_model.SENDER_NOT_WIRED,
    } <= missing


# ---------------------------------------------------------------- the only recording path


def test_the_command_records_a_verified_report_and_refuses_anything_else(
    tmp_path_factory: pytest.TempPathFactory,
    migrated_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil

    from app.config import database_path

    data = tmp_path_factory.mktemp("visual-cli")
    database = database_path(data)
    database.parent.mkdir(parents=True)
    shutil.copyfile(migrated_template, database)
    monkeypatch.setenv("ICBM_DATA_DIR", str(data))
    monkeypatch.setenv("ICBM_SECRET_BACKEND", "memory")
    head = head_revision()
    assert head is not None
    ui = AppConfig.from_env().ui_dir
    sha = running_checkout_sha()
    assert sha is not None
    report = sealed(code_sha=sha, code_digest=running_code_digest(ui), schema_head=head)
    path = tmp_path_factory.mktemp("visual-report") / "report.json"
    args = [
        "live",
        "record-visual-acceptance",
        "--report",
        str(path),
        "--approved-by",
        "architect",
        "--authorization-ref",
        REVIEW,
        "--actor",
        OPERATOR,
    ]

    def count() -> int:
        with sqlite3.connect(database) as raw:
            return int(raw.execute("SELECT COUNT(*) FROM visual_acceptances").fetchone()[0])

    # A report taken at another commit than this checkout's: refused, nothing recorded.
    other = sealed(code_sha="d" * 40, code_digest=running_code_digest(ui), schema_head=head)
    path.write_text(json.dumps(other))
    assert cli.main(args) == 1 and count() == 0
    # A tampered report: refused, nothing recorded.
    path.write_text(json.dumps({**report, "failures": ["x"]}))
    assert cli.main(args) == 1 and count() == 0
    # The verified report at its own commit: recorded once.
    path.write_text(json.dumps(report))
    assert cli.main(args) == 0 and count() == 1


def test_no_http_route_can_record_a_visual_acceptance(api: TestClient, config: AppConfig) -> None:
    routes = [
        (sorted(getattr(route, "methods", None) or ()), getattr(route, "path", ""))
        for route in api.app.routes
    ]
    assert not [path for _, path in routes if "visual" in path.lower()]
    writes = [(m, p) for m, p in routes if set(m) - {"GET", "HEAD"} and "/live" in p]
    assert writes == []
    # The read-only projection records nothing, and the stack stays unproven.
    before = rows(config)
    response = api.get("/api/v1/register/live", headers={"X-ICBM-Client": "icbm-web"})
    assert response.status_code == 200, response.text
    live = response.json()
    assert live["proofs"] == {
        "evidence_retention_ready": False,
        "visual_acceptance_recorded": False,
    }
    assert live["brake"]["state"] == "ENGAGED" and live["brake"]["recorded"] is False
    assert rows(config) == before == 0


def test_the_live_projection_shows_the_brake_every_grant_and_its_readiness(
    api: TestClient, container: Container, config: AppConfig
) -> None:
    from tests.integration.test_g3b_restore_retention import asset_grant, durable_unit

    unit = durable_unit(api, container, config)
    grant_id, _ = asset_grant(container, unit)
    container.live_authority.engage_brake(
        actor=OPERATOR, reason_code="OPERATOR_STOP", correlation_id=CID
    )
    live = api.get("/api/v1/register/live", headers={"X-ICBM-Client": "icbm-web"}).json()
    assert live["brake"] | {"changed_at": None} == {
        "state": "ENGAGED",
        "recorded": True,
        "generation": live["brake"]["generation"],
        "reason_code": "OPERATOR_STOP",
        "changed_at": None,
    }
    (grant,) = live["grants"]
    assert grant["grant_id"] == grant_id and grant["stage"] == "ASSET"
    assert grant["state"] == "ACTIVE" and grant["budget_used"] == 0
    assert grant["readiness"]["verdict"] == "BLOCKED"
    assert {
        live_model.MODE_NOT_LIVE,
        live_model.BRAKE_ENGAGED,
        live_model.VISUAL_UNRECORDED,
    } <= set(grant["readiness"]["missing"])


def test_evidence_retention_guards_the_visual_acceptance_record(
    api: TestClient, container: Container, config: AppConfig
) -> None:
    """ADR-0018 §8: the record is canary evidence; retention fails closed without its guard."""
    retention = container.retention
    assert retention.checks().checks["no_delete_triggers"]["visual_acceptances"] is True
    _, verdict = retention.prove(actor=OPERATOR, correlation_id=CID)
    assert retention.ready()
    with sqlite3.connect(config.data_dir / "runtime" / "icbm.db") as raw:
        raw.execute("DROP TRIGGER trg_visual_acceptances_no_delete")
    checks = retention.checks()
    assert checks.checks["no_delete_triggers"]["visual_acceptances"] is False
    assert not checks.passed and not retention.ready()
    assert verdict.value == "PASSED"


def test_no_readable_commit_records_nothing(container: Container, config: AppConfig) -> None:
    unreadable = VisualAcceptanceService(
        store=LiveAuthorityStore(container.db, container.clock, container.audit),
        code_sha=lambda: None,
        code_identity=lambda: running_code_digest(container.config.ui_dir),
        schema_head=head_revision,
    )
    with pytest.raises(InputValidationError) as refused:
        unreadable.record(
            current(container),
            approved_by="architect",
            authorization_ref=REVIEW,
            actor=OPERATOR,
            correlation_id=CID,
        )
    assert refused.value.code == live_model.VISUAL_COMMIT_NOT_CHECKED_OUT
    assert rows(config) == 0


def _git(root: Path, head: str, **files: str) -> Path:
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head)
    for name, content in files.items():
        path = git / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return git


def test_the_commit_is_read_from_the_checkouts_own_git_metadata(tmp_path: Path) -> None:
    a, b = "a" * 40, "b" * 40
    # Detached HEAD.
    assert checkout_sha(_git(tmp_path / "detached", f"{a}\n").parent) == a
    # A loose branch ref, and a packed one.
    loose = _git(tmp_path / "loose", "ref: refs/heads/main\n", refs__heads__main=f"{b}\n")
    assert checkout_sha(loose.parent) == b
    packed = _git(
        tmp_path / "packed",
        "ref: refs/heads/main\n",
        **{"packed-refs": f"# pack-refs with: peeled\n{a} refs/heads/other\n{b} refs/heads/main\n"},
    )
    assert checkout_sha(packed.parent) == b
    # A linked worktree: ``.git`` names its git dir, whose ``commondir`` holds the refs.
    common = _git(tmp_path / "main", "ref: refs/heads/main\n", refs__heads__topic=f"{a}\n")
    linked = common / "worktrees" / "wt"
    linked.mkdir(parents=True)
    (linked / "HEAD").write_text("ref: refs/heads/topic\n")
    (linked / "commondir").write_text("../..\n")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {linked}\n")
    assert checkout_sha(worktree) == a
    # Unreadable, malformed or escaping: never guessed.
    assert checkout_sha(tmp_path / "nowhere") is None
    assert checkout_sha(_git(tmp_path / "bad", "ref: refs/heads/none\n").parent) is None
    assert checkout_sha(_git(tmp_path / "junk", "not a ref\n").parent) is None
    assert checkout_sha(_git(tmp_path / "up", "ref: refs/../../x\n").parent) is None
    short = _git(tmp_path / "short", "ref: refs/heads/main\n", refs__heads__main="abc\n")
    assert checkout_sha(short.parent) is None


def test_the_running_commit_is_the_checkouts_head() -> None:
    import shutil
    import subprocess

    from app.core.code_identity import REPOSITORY_ROOT

    if shutil.which("git") is None:
        pytest.skip("no git executable to compare with")
    head = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert checkout_sha() == head
