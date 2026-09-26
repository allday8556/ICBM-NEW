"""The Phase C harness building blocks, offline (Issue #110 C0 `5826469852`; review `5313663701`).

The campaign ledger is a crash-durable, single-writer, append-only SQLite file in the campaign
root whose tampering and tail loss are detected; stages open only by typed grants in order; the
frozen ceilings are enforced by reservations; the two roots stay apart; resolution artifacts are
immutable, sanitized and content-addressed; and the synthetic V4 controls prove fail-closed
behaviour without a supplier read.
"""

import contextlib
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import scripts.phasec.ledger as ledger_module
from app.collect.adaptive.capture import final_scan, residual_findings
from app.collect.adaptive.validation import NegativeClass, Verdict, validate
from app.collect.adaptive_capture.controls import SYNTHETIC_NEGATIVES
from scripts.phasec.artifacts import (
    ARTIFACT_SCHEMA,
    ArtifactRefused,
    read_resolution,
    write_resolution,
)
from scripts.phasec.ceilings import CEILINGS
from scripts.phasec.grants import (
    C0_AUTHORIZATION,
    GRANT_SCHEMA,
    GrantRefused,
    c0_grant,
    check_grant,
)
from scripts.phasec.ledger import (
    CampaignInUse,
    CampaignLedger,
    LedgerRefused,
    approval_phrase,
)
from scripts.phasec.roots import REPO_ROOT, RootsRefused, require_roots
from tests.adaptive_support import samples, synmart_bundle

SHA = "a" * 40
CAMPAIGN = "phase-c-synthetic-one"
TARGETS = sorted(["1" * 64, "2" * 64])


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[CampaignLedger]:
    root = tmp_path / "campaign"
    root.mkdir()
    ledger = CampaignLedger(root)
    with ledger.writer():
        ledger.create(
            campaign_id=CAMPAIGN,
            code_sha=SHA,
            c0_grant=c0_grant(CAMPAIGN, SHA, C0_AUTHORIZATION),
            actor="op",
        )
        yield ledger


def grant_of(stage: str, authorization: str, **scope: Any) -> dict[str, Any]:
    return {
        "schema": GRANT_SCHEMA,
        "campaign_id": CAMPAIGN,
        "code_sha": SHA,
        "stage": stage,
        "authorization": authorization,
        "scope": {"ceilings": dict(CEILINGS[stage]), **scope},
    }


def encode(value: Any) -> bytes:
    return json.dumps(value).encode("utf-8")


def grant(stage: str, authorization: str, **scope: Any) -> bytes:
    """A grant as the exact bytes an operator would copy."""
    return encode(grant_of(stage, authorization, **scope))


def open_c1(ledger: CampaignLedger) -> None:
    raw_grant = grant("C1", "issuecomment-5900000001", supplier_key="s", target_digests=TARGETS)
    check_grant(ledger.campaign(), raw_grant)
    ledger.authorize(raw_grant, actor="op", correlation_id="c1")


@contextlib.contextmanager
def raw(ledger: CampaignLedger) -> Iterator[sqlite3.Connection]:
    with contextlib.closing(sqlite3.connect(ledger.path, isolation_level=None)) as db:
        yield db


# ---------------------------------------------------------------- the campaign ledger


def test_a_campaign_freezes_its_identity_code_and_ceilings_and_grants_only_c0(
    ledger: CampaignLedger,
) -> None:
    campaign = ledger.campaign()
    assert (campaign.campaign_id, campaign.code_sha) == (CAMPAIGN, SHA)
    assert campaign.authorized == {"C0": C0_AUTHORIZATION} and campaign.current_stage == "C0"
    assert campaign.ceilings["C1"]["product_reads"] == 4
    assert campaign.ceilings["C3"]["collection_submissions"] == 3
    assert all(campaign.ceilings[stage]["product_reads"] == 0 for stage in ("C0", "C2", "C4"))
    assert approval_phrase(campaign, "enable") == f"I APPROVE enable FOR {CAMPAIGN} AT {SHA[:12]}"
    with pytest.raises(LedgerRefused):
        ledger.create(
            campaign_id=CAMPAIGN,
            code_sha=SHA,
            c0_grant=c0_grant(CAMPAIGN, SHA, C0_AUTHORIZATION),
            actor="op",
        )
    with pytest.raises(GrantRefused, match="issuecomment-5826469852"):
        c0_grant(CAMPAIGN, SHA, "issuecomment-5826469853")


def test_every_write_is_durable_and_needs_the_writer_lock(
    ledger: CampaignLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []
    real = sqlite3.connect

    def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = real(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    open_c1(ledger)
    assert "PRAGMA synchronous=FULL" in statements and "BEGIN IMMEDIATE" in statements
    assert statements.index("PRAGMA synchronous=FULL") < statements.index("BEGIN IMMEDIATE")
    unlocked = CampaignLedger(ledger.root)
    with pytest.raises(LedgerRefused, match="writer lock"):
        unlocked.intend("x", actor="op", correlation_id="k", payload={})


def test_a_second_writer_on_the_same_campaign_is_refused(ledger: CampaignLedger) -> None:
    with pytest.raises(CampaignInUse), CampaignLedger(ledger.root).writer():
        pass


def test_every_table_is_append_only(ledger: CampaignLedger) -> None:
    open_c1(ledger)
    ledger.intend(
        "request-capture",
        actor="op",
        correlation_id="k",
        payload={},
        reservations=[("collection_submissions", TARGETS[0])],
    )
    with raw(ledger) as db:
        for table in (
            "campaign",
            "ceilings",
            "events",
            "correlations",
            "grants",
            "reservations",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE ceilings SET ceiling = 40")
        with pytest.raises(sqlite3.IntegrityError, match="CEILINGS_FROZEN"):
            db.execute("INSERT INTO ceilings VALUES ('C1', 'new_class', 9)")


@pytest.mark.parametrize("change", ["drop_trigger", "truncate_tail", "edit_payload", "sequence"])
def test_tampering_or_a_lost_tail_is_detected(ledger: CampaignLedger, change: str) -> None:
    open_c1(ledger)
    ledger.intend("request-capture", actor="op", correlation_id="k", payload={})
    ledger.record("REQUEST_CAPTURE", actor="op", correlation_id="k", payload={"n": 1})
    with raw(ledger) as db:
        if change == "drop_trigger":
            db.execute("DROP TRIGGER reservations_guard")
        else:
            db.execute("DROP TRIGGER events_no_delete")
            db.execute("DROP TRIGGER events_no_update")
            if change == "truncate_tail":
                db.execute("DELETE FROM events WHERE seq = (SELECT MAX(seq) FROM events)")
            elif change == "edit_payload":
                db.execute(
                    "UPDATE events SET payload_json = '{\"n\":2}' WHERE kind = 'REQUEST_CAPTURE'"
                )
            else:
                db.execute("DELETE FROM events WHERE kind = 'INTENDED'")
            # Even the triggers put back leave the damage visible.
            for statement in ledger_module._statements(ledger_module._SCHEMA):
                if "TRIGGER events_no_" in statement:
                    db.execute(statement)
    with pytest.raises(LedgerRefused):
        ledger.campaign()
    with pytest.raises(LedgerRefused):
        ledger.intend("x", actor="op", correlation_id="k2", payload={})


def test_the_ceilings_are_those_review_5312911203_froze() -> None:
    c1, c3 = CEILINGS["C1"], CEILINGS["C3"]
    assert (c1["collection_submissions"], c1["target_identities"], c1["product_reads"]) == (2, 2, 4)
    assert (c1["connect_control_reads"], c1["connect_protected_reads"]) == (4, 4)
    assert (c1["connect_authenticate"], c1["policy_reads"]) == (0, 0)
    assert (c1["image_requests_per_attempt"], c1["image_bytes_max"]) == (13, 2 * 1024 * 1024)
    assert c1["new_image_bytes_per_attempt"] == 24 * 1024 * 1024
    assert (c3["collection_submissions"], c3["product_reads"], c3["window_min_size"]) == (3, 6, 3)


# ---------------------------------------------------------------- grants and reservations


def test_stages_advance_once_each_in_order_by_typed_grants(ledger: CampaignLedger) -> None:
    campaign = ledger.campaign()
    for bad, why in (
        (grant("C2", "issuecomment-5900000001"), "next stage of this campaign is C1"),
        (
            grant("C1", C0_AUTHORIZATION, supplier_key="s", target_digests=TARGETS),
            "opens one stage of a campaign, once",
        ),
        (
            grant("C1", "issuecomment-5900000001", supplier_key="s", target_digests=TARGETS[:1]),
            "exactly 2",
        ),
        (
            grant("C1", "issuecomment-5900000001", supplier_key="s", target_digests=TARGETS[::-1]),
            "sorted",
        ),
        (grant("C1", "issuecomment-5900000001", supplier_key="s"), "scope holds exactly"),
        (
            grant("C1", "see-thread", supplier_key="s", target_digests=TARGETS),
            "issuecomment-<id> or pullrequestreview-<id>",
        ),
        (
            encode({**grant_of("C1", "issuecomment-5900000001"), "code_sha": "b" * 40}),
            "another exact code SHA",
        ),
        (
            encode({**grant_of("C1", "issuecomment-5900000001"), "campaign_id": "phase-c-other"}),
            "another campaign",
        ),
        (encode({**grant_of("C1", "issuecomment-5900000001"), "extra": 1}), "holds exactly"),
        (b"\xff not utf-8", "UTF-8 JSON"),
    ):
        with pytest.raises(LedgerRefused, match=why):
            check_grant(campaign, bad)
    wide = grant_of("C1", "issuecomment-5900000001", supplier_key="s", target_digests=TARGETS)
    wide["scope"]["ceilings"] = {**CEILINGS["C1"], "product_reads": 40}
    with pytest.raises(GrantRefused, match="frozen C1 ceilings"):
        check_grant(campaign, encode(wide))
    open_c1(ledger)
    assert ledger.campaign().current_stage == "C1"
    with pytest.raises(GrantRefused, match="next stage of this campaign is C2"):
        check_grant(
            ledger.campaign(),
            grant("C1", "issuecomment-5900000002", supplier_key="s", target_digests=TARGETS),
        )


def test_the_grants_table_enforces_the_order_even_without_the_checks(
    ledger: CampaignLedger,
) -> None:
    # Bypassing check_grant, the table still refuses a skipped stage and a reused authorization.
    with pytest.raises(LedgerRefused) as skipped:
        ledger.authorize(grant("C2", "issuecomment-5900000001"), actor="op", correlation_id="x")
    assert skipped.value.code == "STAGE_OUT_OF_ORDER"
    with pytest.raises(LedgerRefused) as reused:
        ledger.authorize(grant("C1", C0_AUTHORIZATION), actor="op", correlation_id="y")
    assert reused.value.code == "PHASE_C_AUTHORIZATION_REUSED"
    assert ledger.campaign().current_stage == "C0"


def test_a_c2_grant_names_only_this_campaigns_own_pass_validation(ledger: CampaignLedger) -> None:
    open_c1(ledger)
    scope = {
        "supplier_key": "s",
        "epr_digest": "e" * 64,
        "sample_digests": ["5" * 64],
        "validation_run_id": "run-1",
        "window_min_size": 3,
    }
    with pytest.raises(GrantRefused, match="own C1 recorded"):
        check_grant(ledger.campaign(), grant("C2", "issuecomment-5900000002", **scope))
    ledger.intend("validate", actor="op", correlation_id="v", payload={})
    ledger.record(
        "VALIDATE",
        actor="op",
        correlation_id="v",
        payload={"epr": "e" * 64, "samples": ["5" * 64], "run_id": "run-1", "verdict": "PASS"},
    )
    for wrong in ({"validation_run_id": "run-2"}, {"window_min_size": 4}, {"supplier_key": "t"}):
        with pytest.raises(GrantRefused):
            check_grant(
                ledger.campaign(), grant("C2", "issuecomment-5900000002", **{**scope, **wrong})
            )
    raw_grant = grant("C2", "issuecomment-5900000002", **scope)
    check_grant(ledger.campaign(), raw_grant)
    ledger.authorize(raw_grant, actor="op", correlation_id="c2")
    assert ledger.campaign().grants["C2"].scope["epr_digest"] == "e" * 64


def test_a_reservation_beyond_its_frozen_ceiling_or_stage_is_refused_and_recorded(
    ledger: CampaignLedger,
) -> None:
    with pytest.raises(LedgerRefused) as closed:
        ledger.intend(
            "x",
            actor="op",
            correlation_id="c0",
            payload={},
            reservations=[("collection_submissions", TARGETS[0])],
        )
    assert closed.value.code == "CEILING", "C0 has a zero ceiling for every class"
    open_c1(ledger)
    for index in range(2):
        ledger.intend(
            "request-capture",
            actor="op",
            correlation_id=f"r{index}",
            payload={},
            reservations=[("collection_submissions", TARGETS[index])],
        )
        ledger.record("REQUEST_CAPTURE", actor="op", correlation_id=f"r{index}", payload={})
    with pytest.raises(LedgerRefused) as over:
        ledger.intend(
            "request-capture",
            actor="op",
            correlation_id="r2",
            payload={},
            reservations=[("collection_submissions", "3" * 64)],
        )
    assert over.value.code == "CEILING"
    with pytest.raises(LedgerRefused) as unknown:
        ledger.intend(
            "x", actor="op", correlation_id="r3", payload={}, reservations=[("naps", "3" * 64)]
        )
    assert unknown.value.code == "CLASS_NOT_BUDGETED"
    assert ledger.counts("C1")["collection_submissions"] == 2
    assert [r["reason"] for r in ledger.refusals()] == ["CEILING", "CEILING", "CLASS_NOT_BUDGETED"]
    assert ledger.campaign().unfinished() == [], "a refused reservation leaves no intent behind"
    with raw(ledger) as db, pytest.raises(sqlite3.IntegrityError, match="STAGE_NOT_CURRENT"):
        db.execute(
            "INSERT INTO reservations (at, stage, class, subject_digest, correlation_id) "
            "VALUES ('t', 'C3', 'collection_submissions', ?, 'z')",
            ("4" * 64,),
        )


def test_an_intent_is_answered_once_and_an_unfinished_one_stops_the_campaign(
    ledger: CampaignLedger,
) -> None:
    ledger.intend("a", actor="op", correlation_id="k", payload={})
    assert ledger.campaign().unfinished() == ["k"]
    with pytest.raises(LedgerRefused) as blocked:
        ledger.intend("b", actor="op", correlation_id="k2", payload={})
    assert blocked.value.code == "UNFINISHED_ACTION", "the correlations table enforces it"
    with pytest.raises(LedgerRefused):
        ledger.record("A", actor="op", correlation_id="other", payload={})
    ledger.record("A", actor="op", correlation_id="k", payload={})
    with pytest.raises(LedgerRefused):
        ledger.record("A", actor="op", correlation_id="k", payload={})
    with pytest.raises(LedgerRefused) as reused:
        ledger.intend("c", actor="op", correlation_id="k", payload={})
    assert reused.value.code == "PHASE_C_CORRELATION_REUSED"
    assert ledger.campaign().unfinished() == []


def test_the_ledger_reads_back_after_a_restart(ledger: CampaignLedger) -> None:
    open_c1(ledger)
    before = ledger.campaign()
    again = CampaignLedger(ledger.root).campaign()
    assert again == before and again.current_stage == "C1"


# ---------------------------------------------------------------- the two roots


def test_the_campaign_root_stays_apart_from_the_repository_and_every_data_root(
    tmp_path: Path,
) -> None:
    environ = {"USERPROFILE": str(tmp_path / "home")}
    data_root = tmp_path / "data"
    require_roots(tmp_path / "campaign", data_root, environ)
    for campaign_root, data in (
        (REPO_ROOT / "campaign", data_root),  # inside the repository
        (data_root / "campaign", data_root),  # inside the data root
        (tmp_path, data_root),  # containing the data root
        (tmp_path / "home" / "ICBM-NEW" / "data" / "x", data_root),  # the default data root
        (Path("relative-campaign"), data_root),
    ):
        with pytest.raises(RootsRefused):
            require_roots(campaign_root, data, environ)
    with pytest.raises(RootsRefused):
        require_roots(tmp_path / "campaign", Path("relative-data"), environ)


# ---------------------------------------------------------------- resolution artifacts


def _artifact(**overrides: object) -> dict[str, object]:
    artifact: dict[str, object] = {
        "schema": ARTIFACT_SCHEMA,
        "campaign_id": CAMPAIGN,
        "collection_run_id": "run-1",
        "revision_id": "rev-1",
        "mismatch_dimensions": ["prices"],
        "source_evidence": ["pfr:rev-1:field:prices:evidence:0"],
        "resolution": "ADAPTIVE_CORRECT",
        "adaptive_failed_closed": False,
        "actor": "operator:owner",
        "correlation_id": "c-1",
        "at": "2026-09-25T00:00:00+00:00",
    }
    artifact.update(overrides)
    return artifact


def test_a_resolution_artifact_is_content_addressed_and_named_by_its_evidence_ref(
    tmp_path: Path,
) -> None:
    reference = write_resolution(tmp_path, _artifact())
    assert re.fullmatch(rf"phase-c:{CAMPAIGN}:resolution:[0-9a-f]{{64}}", reference)
    assert read_resolution(tmp_path, reference) == _artifact()
    assert write_resolution(tmp_path, _artifact()) == reference, "the same content, the same name"
    (path,) = (tmp_path / "resolutions").iterdir()
    path.write_text(path.read_text("utf-8").replace("run-1", "run-2"), "utf-8")
    with pytest.raises(ArtifactRefused, match="does not hash"):
        read_resolution(tmp_path, reference)
    path.unlink()
    with pytest.raises(ArtifactRefused, match="missing"):
        read_resolution(tmp_path, reference)
    with pytest.raises(ArtifactRefused):
        read_resolution(tmp_path, "phase-c:other:resolution:" + "0" * 64)


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_evidence": ["https://shop.example/p/1?token=x"]},  # a URL
        {"mismatch_dimensions": ["<div class='a'>"]},  # markup
        {"source_evidence": ["cookie: sid=abc"]},  # session material
        {"actor": "operator 010-1234-5678"},  # private data
        {"resolution": "LOOKS_FINE"},  # outside the vocabulary
        {"adaptive_failed_closed": "no"},  # not an answer
        {"extra": "field"},  # not a field
    ],
)
def test_a_resolution_artifact_refuses_anything_but_its_sanitized_fields(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    with pytest.raises((ArtifactRefused, ValueError)):
        write_resolution(tmp_path, _artifact(**overrides))
    assert not (tmp_path / "resolutions").exists() or not any((tmp_path / "resolutions").iterdir())


# ---------------------------------------------------------------- synthetic V4 controls


def test_the_synthetic_controls_are_typed_hold_no_secret_and_make_v4_fail_closed() -> None:
    assert set(SYNTHETIC_NEGATIVES) == set(NegativeClass)
    for html in SYNTHETIC_NEGATIVES.values():
        assert not residual_findings(html)
        assert 'value="' not in html and "token" not in html.lower()
    run = validate(synmart_bundle(), samples(), negatives=SYNTHETIC_NEGATIVES)
    assert run.check("V4").outcome is Verdict.PASS, run.check("V4").details
    assert final_scan({"tag": "#document", "attrs": {}, "children": []}) == []


def test_a_damaged_ledger_file_is_refused_not_a_crash(ledger: CampaignLedger) -> None:
    ledger.path.write_bytes(b"not a database" * 100)
    with pytest.raises(LedgerRefused):
        ledger.campaign()
    with pytest.raises(LedgerRefused):
        ledger.intend("x", actor="op", correlation_id="k", payload={})


def test_a_write_that_crashed_midway_is_rolled_back_and_the_campaign_still_reads(
    ledger: CampaignLedger,
) -> None:
    # Self-review of 5313663701 B4: a crash mid-transaction leaves a hot journal behind. A
    # read-only open cannot roll it back and would leave the campaign unreadable for good.
    open_c1(ledger)
    before = ledger.campaign()
    crash = (
        "import os, sqlite3\n"
        f"db = sqlite3.connect({str(ledger.path)!r}, isolation_level=None)\n"
        "db.execute('PRAGMA cache_size=1')\n"
        "db.execute('BEGIN IMMEDIATE')\n"
        "for i in range(20000):\n"
        '    db.execute("INSERT INTO refusals (at, stage, class, reason, correlation_id)'
        " VALUES ('t', 'C1', 'x', 'y', 'z')\")\n"
        "os._exit(1)\n"
    )
    subprocess.run([sys.executable, "-c", crash], check=False)
    assert Path(f"{ledger.path}-journal").exists(), "the crash left a hot journal"
    assert ledger.campaign() == before
    assert ledger.refusals() == [], "the crashed write never happened"
    assert not Path(f"{ledger.path}-journal").exists()
    ledger.intend("x", actor="op", correlation_id="after-crash", payload={})


def test_an_authorization_is_an_opaque_identity_used_once(ledger: CampaignLedger) -> None:
    # Never compared by size: a smaller-looking id of either kind is as good as any other.
    small = grant("C1", "pullrequestreview-12", supplier_key="s", target_digests=TARGETS)
    check_grant(ledger.campaign(), small)
    ledger.authorize(small, actor="op", correlation_id="c1")
    assert ledger.campaign().authorized["C1"] == "pullrequestreview-12"
    with pytest.raises(LedgerRefused) as reused:
        ledger.authorize(grant("C2", "pullrequestreview-12"), actor="op", correlation_id="c2")
    assert reused.value.code == "PHASE_C_AUTHORIZATION_REUSED"
    ledger.authorize(grant("C2", "issuecomment-7"), actor="op", correlation_id="c2")
    assert ledger.campaign().current_stage == "C2"


def test_a_grant_is_recorded_as_the_exact_bytes_it_was_read_as(ledger: CampaignLedger) -> None:
    pretty = json.dumps(
        grant_of("C1", "issuecomment-5900000001", supplier_key="s", target_digests=TARGETS),
        indent=2,
    ).encode("utf-8")
    check_grant(ledger.campaign(), pretty)
    ledger.authorize(pretty, actor="op", correlation_id="c1")
    recorded = ledger.campaign().grants["C1"]
    assert recorded.digest == hashlib.sha256(pretty).hexdigest()
    with raw(ledger) as db:
        (text,) = db.execute("SELECT grant_text FROM grants WHERE stage = 'C1'").fetchone()
    assert text.encode("utf-8") == pretty


def test_a_proven_not_applied_command_releases_its_reservation(ledger: CampaignLedger) -> None:
    open_c1(ledger)
    for index, outcome in enumerate(("REQUEST_CAPTURE", "NOT_APPLIED")):
        ledger.intend(
            "request-capture",
            actor="op",
            correlation_id=f"r{index}",
            payload={},
            reservations=[("collection_submissions", TARGETS[index])],
        )
        ledger.record(outcome, actor="op", correlation_id=f"r{index}", payload={})
    assert ledger.counts("C1")["collection_submissions"] == 1
    assert len(ledger.reservations("C1")) == 2, "the released one stays recorded"
    ledger.intend(
        "request-capture",
        actor="op",
        correlation_id="r2",
        payload={},
        reservations=[("collection_submissions", TARGETS[1])],
    )
    assert ledger.counts("C1")["collection_submissions"] == 2


def test_an_ambiguous_command_puts_the_campaign_on_hold(ledger: CampaignLedger) -> None:
    ledger.intend("x", actor="op", correlation_id="k", payload={})
    ledger.hold(actor="op", correlation_id="k", reason="owner truth could not be read")
    campaign = ledger.campaign()
    assert [h["correlation_id"] for h in campaign.held()] == ["k"]
    assert campaign.unfinished() == []
    with pytest.raises(LedgerRefused):
        ledger.record("X", actor="op", correlation_id="k", payload={})
    with pytest.raises(LedgerRefused) as held:
        ledger.intend("y", actor="op", correlation_id="k2", payload={})
    assert held.value.code == "CAMPAIGN_HOLD"
    with raw(ledger) as db, pytest.raises(sqlite3.IntegrityError, match="HELD"):
        db.execute("INSERT INTO outcomes VALUES ('k', 1, 99)")


def test_a_stage_grant_never_reuses_a_correlation(ledger: CampaignLedger) -> None:
    ledger.intend("x", actor="op", correlation_id="k", payload={})
    ledger.record("X", actor="op", correlation_id="k", payload={})
    raw_grant = grant("C1", "issuecomment-5900000001", supplier_key="s", target_digests=TARGETS)
    check_grant(ledger.campaign(), raw_grant)
    with pytest.raises(LedgerRefused) as reused:
        ledger.authorize(raw_grant, actor="op", correlation_id="k")
    assert reused.value.code == "PHASE_C_CORRELATION_REUSED"
    assert ledger.campaign().current_stage == "C0"
