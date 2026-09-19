"""M4 PR-F: the offline acceptance harness, run end to end on fresh temporary roots (Issue #80
kickoff 5739459941 §Q).

Every run here is the real harness:
- synthetic source truth through COLLECT's own stores;
- the real M4 owners;
- a restart;
- the sanitized, digested report.

No supplier, marketplace or AI provider is contacted, and no campaign runtime is opened. The
module-scoped run goes through the command line exactly as an operator would run it.
"""

import contextlib
import importlib
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.config import AppConfig
from app.container import Container
from scripts.m4_acceptance import main
from scripts.m4accept import evidence, harness
from scripts.m4accept.harness import run_acceptance
from scripts.m4accept.owners import open_owners
from scripts.m4accept.root import (
    DATA,
    MARKER,
    REPO_ROOT,
    REPORT,
    RootRefused,
    initialize_root,
    root_problems,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class Accepted:
    root: Path
    report: dict[str, Any]
    exit_code: int


@pytest.fixture(scope="module")
def accepted(tmp_path_factory: pytest.TempPathFactory) -> Accepted:
    root = tmp_path_factory.mktemp("m4-acceptance") / "fresh-root"
    code = main(["--root", str(root)])
    report = json.loads((root / REPORT).read_text("utf-8"))
    return Accepted(root, report, code)


def _checks(report: dict[str, Any]) -> dict[str, bool]:
    return {str(check["name"]): bool(check["passed"]) for check in report["checks"]}


# ---------------------------------------------------------------- the passing run


def test_a_fresh_root_passes_every_check(accepted: Accepted) -> None:
    # Kickoff §Q 1, 40-42: exit 0, every check passes, the schema is still at 0015.
    report = accepted.report
    assert accepted.exit_code == 0
    assert report["problems"] == []
    assert report["checks_passed"] == report["checks_total"] >= 140
    assert report["mode"] == "OFFLINE_SYNTHETIC" and report["claim"].startswith("HARNESS_RUN")
    assert report["alembic_head"] == report["database_revision"] == "0015_m4_quantity_offers"
    marker = json.loads((accepted.root / MARKER).read_text("utf-8"))
    assert (marker["state"], marker["run_id"]) == ("PASSED", report["run_id"])
    assert sorted(p.name for p in accepted.root.iterdir()) == sorted([DATA, MARKER, REPORT])


REQUIRED_CHECKS = {
    "6 R1 stable group, default Item, BASE_PRODUCT": (
        "s1.r1.one_group_one_confirmed_member",
        "s1.r1.default_single_unit_item",
        "s1.r1.base_product_binding",
        "s1.r1.current_source_revision",
    ),
    "7 canonical minimum precedence": (
        "s1.r1.canonical_minimum_precedence",
        "s1.r1.pricing.exact_amounts",
    ),
    "8 immutable pricing snapshot": ("s1.r2.history_unchanged", "history.since_s1_unchanged"),
    "9 derived artifact apart from SourceAsset": (
        "s1.r1.image.derived_bytes_apart_from_source_assets",
        "boundary.derived_apart_from_source",
        "boundary.stored_bytes_content_addressed",
    ),
    "10 operator image selection required": ("s1.r1.operator_selection_is_required",),
    "11 exact-binary QA required": ("s1.r1.image.exact_binary_qa_is_required",),
    "12 base and pricing READY": ("s1.r1.base_ready", "s1.r1.pricing_ready"),
    "13 restart preserves identities and pointers": (
        "restart.s1.read_back_identical",
        "restart.final.read_back_identical",
        "restart.s1.schema_at_head",
    ),
    "14 restart writes nothing": (
        "restart.s1.reading_wrote_nothing",
        "restart.s1.replays_wrote_nothing",
        "restart.final.reading_wrote_nothing",
        "restart.final.replays_wrote_nothing",
    ),
    "15 R2 advances without rewriting R1": (
        "s1.r2.pointer_advanced",
        "s1.r2.r1_unchanged",
        "s1.r2.same_product_group",
        "s1.r2.same_default_item",
        "s1.r2.binding_replaced",
    ),
    "16 old pricing superseded": ("s1.r2.old_pricing_stale",),
    "17 old image selection and QA not current": ("s1.r2.old_image_decisions_not_current",),
    "18 refresh restores readiness": ("s1.r2.base_ready_again", "s1.r2.pricing_ready_again"),
    "19 exact offer totals": ("s2.q1.three_exact_offers", "s2.q1.no_unit_price_or_multiple"),
    "20 generic prices cannot alter offer cost": (
        "s2.q1.generic_prices_unused",
        "s2.q1.costs_are_offer_totals",
    ),
    "21 q1 reuses the default signature": ("s2.q1.q1_is_the_default_single_unit",),
    "22 q2/q3 distinct Items": ("s2.q1.distinct_quantity_items",),
    "23 exact SOURCE_OFFER binding quantity": ("s2.q1.exact_source_offer_bindings",),
    "24 no SourceSKU": ("s2.q1.no_source_sku",),
    "25 no automatic image selection": (
        "s2.q1.every_item_needs_its_own_selection",
        "s2.q1.q2_q3_do_not_inherit_q1_selection",
        "s2.q1.one_selection_per_item",
    ),
    "26 Q2 keeps Items, replaces offers and bindings": (
        "s2.q2.same_group_and_items",
        "s2.q2.new_revision_scoped_offers",
        "s2.q2.old_offers_kept",
        "s2.q2.old_bindings_closed_new_opened",
        "s2.q2.exact_new_bindings",
    ),
    "27 old quantity pricing stale": ("s2.q2.old_pricing_stale", "s2.q2.repriced_from_new_totals"),
    "28 REVIEW_REQUIRED example": ("s2.q1.every_item_needs_its_own_selection",),
    "29 STALE example": ("s1.r2.old_pricing_stale", "s2.q2.old_image_decisions_not_current"),
    "30 BLOCKED example": ("s3.base_blocked_by_sold_out", "s3.pricing_blocked_by_loss"),
    "31 REGISTER candidates 0": (
        "boundary.registration_candidates_zero",
        "boundary.no_registration_state",
    ),
    "32-33 no supplier or marketplace effect": (
        "hard_zero.egress_grants",
        "boundary.no_supplier_or_marketplace_audit",
        "boundary.no_job_ran",
    ),
    "34 no AI or OCR": ("hard_zero.forbidden_imports",),
    "35 no external network attempt": ("hard_zero.external_network_attempts",),
    "36 no preserved campaign access": (
        "boundary.preserved_campaign_access",
        "boundary.writes_outside_root",
    ),
}


@pytest.mark.parametrize("names", REQUIRED_CHECKS.values(), ids=REQUIRED_CHECKS.keys())
def test_each_required_proof_is_a_passing_check(accepted: Accepted, names: tuple[str, ...]) -> None:
    checks = _checks(accepted.report)
    for name in names:
        assert checks.get(name) is True, name


def test_the_report_carries_the_exact_synthetic_results(accepted: Accepted) -> None:
    # Kickoff §E, §I, §K, §L: the amounts come from durable read-back, not from write results.
    report = accepted.report
    s1, s2, s3 = report["s1_base_product"], report["s2_quantity_offer"], report["s3_blocked"]
    assert (s1["pricing_r1"]["final_sale_price_krw"], s1["pricing_r1"]["price_basis"]) == (
        18000,
        "MINIMUM_SALE_PRICE",
    )
    assert s1["pricing_r1"]["target_margin_price_krw"] == 22728
    assert s1["pricing_r2"]["purchase_cost_krw"] == 11000
    assert s1["readiness_r1"] == s1["readiness_after_refresh"] == ["READY", "READY"]
    assert s1["readiness_after_r2"] == ["STALE", "STALE"]
    assert [offer[1:] for offer in s2["offers_q1"]] == [[1, 19900], [2, 37900], [3, 53900]]
    assert [offer[1:] for offer in s2["offers_q2"]] == [[1, 19900], [2, 36900], [3, 52900]]
    assert [s2["pricing_q1"][q]["final_sale_price_krw"] for q in "123"] == [36184, 68910, 98000]
    assert [s2["pricing_q2"][q]["final_sale_price_krw"] for q in "123"] == [36184, 67093, 96184]
    assert s2["readiness_before_selection"] == dict.fromkeys("123", "REVIEW_REQUIRED")
    assert s3["readiness"] == ["BLOCKED", "BLOCKED"]
    assert s3["pricing"]["price_guard"] == "LOSS"
    states = report["readiness_states"]
    assert set(states) == {"READY", "REVIEW_REQUIRED", "STALE", "BLOCKED", "DUPLICATE"}
    assert states["DUPLICATE"].startswith("NOT_EXERCISED")


def test_the_hard_zero_counters_are_measured_zero(accepted: Accepted) -> None:
    # Kickoff §C, §Q 32-36.
    zero = accepted.report["external_hard_zero"]
    for counter in (
        "external_network_attempts",
        "egress_grants_opened",
        "preserved_campaign_paths_refused",
        "writes_outside_root",
        "supplier_writes",
        "marketplace_reads_writes",
        "ai_calls",
        "ocr_calls",
    ):
        assert zero[counter] == 0, counter
    assert zero["forbidden_imports_blocked"] == zero["forbidden_modules_loaded_during_run"] == []
    assert accepted.report["preserved_campaign_access"] == 0
    boundary = accepted.report["boundary"]
    assert (boundary["registration_candidate_count"], boundary["jobs"]) == (0, 0)


def test_restart_reads_back_exactly_and_writes_nothing(accepted: Accepted) -> None:
    # Kickoff §G, §Q 13-14, 39.
    for phase, (products, items) in (("restart_after_s1", (1, 1)), ("final_restart", (3, 5))):
        restart = accepted.report[phase]
        assert (restart["products"], restart["items"]) == (products, items)
        assert restart["read_back_identical"] and restart["database_digest_unchanged"]


def test_the_report_is_sanitized(accepted: Accepted) -> None:
    # Kickoff §O, §Q 37: no URL, absolute path, secret-like value or business text.
    text = (accepted.root / REPORT).read_text("utf-8")
    assert evidence.leaks(accepted.report, (str(accepted.root), str(accepted.root.resolve()))) == []
    assert text.isascii() and "://" not in text
    for local in (str(accepted.root), str(REPO_ROOT), accepted.root.name):
        assert local not in text
    assert "synthetic price" not in text and "M4 synthetic product" not in text


def test_the_report_digest_verifies_and_catches_tampering(
    accepted: Accepted, tmp_path: Path
) -> None:
    # Kickoff §Q 38.
    assert evidence.verify_report(accepted.report)
    assert main(["--verify", str(accepted.root / REPORT)]) == 0
    tampered = json.loads(json.dumps(accepted.report))
    tampered["s2_quantity_offer"]["offers_q1"][1][2] = 39800
    assert not evidence.verify_report(tampered)
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(tampered), "utf-8")
    assert main(["--verify", str(forged)]) == 1


# ---------------------------------------------------------------- roots


PRESERVED = ("m3-accept-04", "m3-accept-01", "m3-recon-02", "ICBM-M3-ACCEPT-03", "M3_ACCEPT_02")


@pytest.mark.parametrize("name", PRESERVED)
def test_a_preserved_campaign_root_is_refused_before_anything_exists(
    tmp_path: Path, name: str
) -> None:
    # Kickoff §B, §Q 3, 36: refused on the name alone; nothing is created, listed or read.
    root = tmp_path / "ICBM-acceptance" / name / "m4-rehearsal"
    with pytest.raises(RootRefused, match="preserved campaign"):
        run_acceptance(root, {})
    assert main(["--root", str(root)]) == 2
    assert not (tmp_path / "ICBM-acceptance").exists()


def test_a_root_that_is_not_fresh_is_refused(accepted: Accepted, tmp_path: Path) -> None:
    # Kickoff §Q 2: a foreign directory, a file, and a root that already ran.
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("not an acceptance root", "utf-8")
    plain = tmp_path / "plain-file"
    plain.write_text("x", "utf-8")
    cases: list[tuple[Path, str]] = [
        (foreign, "not empty"),
        (plain, "not a directory"),
        (accepted.root, "already been used"),
    ]
    for root, why in cases:
        with pytest.raises(RootRefused, match=why):
            run_acceptance(root, {})
    assert (foreign / "notes.txt").read_text("utf-8") == "not an acceptance root"


def test_a_root_overlapping_the_repository_or_an_icbm_data_directory_is_refused(
    tmp_path: Path,
) -> None:
    inside_repo = REPO_ROOT / "m4-acceptance-refused"
    assert any("repository" in p for p in root_problems(inside_repo, {}))
    data = tmp_path / "icbm-data"
    environ = {"ICBM_DATA_DIR": str(data)}
    for root in (data / "m4", tmp_path):
        with pytest.raises(RootRefused, match="configured ICBM data directory"):
            run_acceptance(root, environ)
    assert not inside_repo.exists() and not data.exists()


def test_an_initialized_root_is_accepted_once(tmp_path: Path) -> None:
    root = initialize_root(tmp_path / "prepared", {})
    assert [p.name for p in root.iterdir()] == [MARKER]
    assert root_problems(root, {}) == []
    report = run_acceptance(root, {})
    assert report["problems"] == []
    assert root_problems(root, {}) != []


# ---------------------------------------------------------------- the guards fail the run


def _failing_run(
    monkeypatch: pytest.MonkeyPatch, root: Path, act: Callable[[harness.Run], None]
) -> dict[str, Any]:
    def scenario_base(run: harness.Run) -> dict[str, object]:
        act(run)
        raise harness.Failed("the injected act ends the run")

    monkeypatch.setattr(harness, "scenario_base", scenario_base)
    return run_acceptance(root, {})


def test_an_external_network_attempt_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Kickoff §C, §Q 35: an attempt is counted, and the provider counters become unknown rather
    # than an invented zero.
    def act(_run: harness.Run) -> None:
        with contextlib.suppress(OSError):
            socket.create_connection(("203.0.113.7", 443), timeout=0.05)

    report = _failing_run(monkeypatch, tmp_path / "root", act)
    assert "hard_zero.external_network_attempts" in report["problems"]
    zero = report["external_hard_zero"]
    assert zero["external_network_attempts"] >= 1 and zero["supplier_writes"] is None


def test_a_provider_import_fails_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Kickoff §C, §Q 4-5, 34: AI, OCR and provider code cannot even be imported during the run.
    def act(_run: harness.Run) -> None:
        with contextlib.suppress(ImportError):
            importlib.import_module("app.ai.provider")

    report = _failing_run(monkeypatch, tmp_path / "root", act)
    assert "hard_zero.forbidden_imports" in report["problems"]
    assert report["external_hard_zero"]["forbidden_imports_blocked"] == ["app.ai"]
    assert report["external_hard_zero"]["ai_calls"] is None


def test_touching_a_preserved_campaign_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Kickoff §B, §Q 36: the path is refused before the file system is touched.
    campaign = tmp_path / "ICBM-acceptance" / "m3-accept-04"

    def act(_run: harness.Run) -> None:
        with contextlib.suppress(PermissionError), open(campaign / "ledger.sqlite3", "rb"):
            pass

    report = _failing_run(monkeypatch, tmp_path / "root", act)
    assert "boundary.preserved_campaign_access" in report["problems"]
    assert report["preserved_campaign_access"] == 1
    assert not campaign.exists()


def test_a_write_outside_the_root_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def act(_run: harness.Run) -> None:
        (tmp_path / "outside.txt").write_text("stray", "utf-8")

    report = _failing_run(monkeypatch, tmp_path / "root", act)
    assert "boundary.writes_outside_root" in report["problems"]


# ---------------------------------------------------------------- composition


def test_the_harness_composes_the_owners_the_container_composes(
    container: Container, config: AppConfig, tmp_path: Path
) -> None:
    # The run drives the same production classes the application wires, with nothing more.
    owners = open_owners(tmp_path / "data", migrate=True)
    try:
        for name in (
            "revisions",
            "source_assets",
            "product_store",
            "materializer",
            "products",
            "pricing",
            "images",
            "product_readiness",
        ):
            assert type(getattr(owners, name)) is type(getattr(container, name)), name
        assert owners.images.qa_rule_version == container.images.qa_rule_version
        assert owners.database_revision() == "0015_m4_quantity_offers"
    finally:
        owners.close()
