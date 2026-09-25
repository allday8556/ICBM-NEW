"""The Phase C harness building blocks, offline (Issue #110 C0 `5826469852`).

The campaign ledger is append-only and hash-chained; the two roots stay apart; resolution
artifacts are immutable, sanitized and content-addressed; the stage ceilings are frozen; and the
synthetic V4 controls prove fail-closed behaviour without a supplier read.
"""

import json
import re
from pathlib import Path

import pytest

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
from scripts.phasec.ledger import CampaignLedger, LedgerRefused, approval_phrase
from scripts.phasec.roots import REPO_ROOT, RootsRefused, require_roots
from tests.adaptive_support import samples, synmart_bundle

SHA = "a" * 40
CAMPAIGN = "phase-c-synthetic-one"


@pytest.fixture
def ledger(tmp_path: Path) -> CampaignLedger:
    ledger = CampaignLedger(tmp_path / "campaign")
    ledger.create(campaign_id=CAMPAIGN, code_sha=SHA, authorization="5826469852", actor="op")
    return ledger


# ---------------------------------------------------------------- the campaign ledger


def test_a_campaign_freezes_its_identity_code_and_ceilings_and_authorizes_only_c0(
    ledger: CampaignLedger,
) -> None:
    campaign = ledger.campaign()
    assert (campaign.campaign_id, campaign.code_sha) == (CAMPAIGN, SHA)
    assert campaign.authorized == {"C0": "5826469852"}
    assert campaign.ceilings["C1"]["product_reads"] == 4
    assert campaign.ceilings["C3"]["collection_submissions"] == 3
    assert all(campaign.ceilings[stage]["product_reads"] == 0 for stage in ("C0", "C2", "C4"))
    assert approval_phrase(campaign, "enable") == f"I APPROVE enable FOR {CAMPAIGN} AT {SHA[:12]}"
    with pytest.raises(LedgerRefused):
        ledger.create(campaign_id=CAMPAIGN, code_sha=SHA, authorization="5826469852", actor="op")


def test_a_stage_is_authorized_once_by_a_numeric_comment_id(ledger: CampaignLedger) -> None:
    ledger.append("STAGE_AUTHORIZED", "op", "c", {"stage": "C1", "authorization": "1234567"})
    assert ledger.campaign().authorized["C1"] == "1234567"
    for payload in (
        {"stage": "C1", "authorization": "7654321"},  # twice
        {"stage": "C9", "authorization": "7654321"},  # no such stage
        {"stage": "C2", "authorization": "see the thread"},  # not an id
    ):
        with pytest.raises(LedgerRefused):
            ledger.append("STAGE_AUTHORIZED", "op", "c", payload)


@pytest.mark.parametrize("change", ["edit", "delete", "reorder", "truncate_hash"])
def test_any_change_to_an_earlier_event_breaks_the_chain(
    ledger: CampaignLedger, change: str
) -> None:
    ledger.append("NOTE", "op", "c", {"n": 1})
    ledger.append("NOTE", "op", "c", {"n": 2})
    lines = ledger.path.read_text("utf-8").splitlines()
    if change == "edit":
        event = json.loads(lines[2])
        event["payload"]["n"] = 99
        lines[2] = json.dumps(event)
    elif change == "delete":
        del lines[1]
    elif change == "reorder":
        lines[2], lines[3] = lines[3], lines[2]
    else:
        event = json.loads(lines[3])
        event["hash"] = "0" * 64
        lines[3] = json.dumps(event)
    ledger.path.write_text("\n".join(lines) + "\n", "utf-8")
    with pytest.raises(LedgerRefused):
        ledger.events()
    with pytest.raises(LedgerRefused):
        ledger.append("NOTE", "op", "c", {"n": 3})


def test_a_campaign_created_with_other_ceilings_is_refused(ledger: CampaignLedger) -> None:
    lines = ledger.path.read_text("utf-8").splitlines()
    first = json.loads(lines[0])
    first["payload"]["ceilings"]["C1"]["product_reads"] = 40
    import hashlib

    body = {k: v for k, v in first.items() if k != "hash"}
    first["hash"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    lines[0] = json.dumps(first)
    ledger.path.write_text("\n".join(lines) + "\n", "utf-8")
    with pytest.raises(LedgerRefused):
        ledger.campaign()


def test_the_ceilings_are_those_review_5312911203_froze() -> None:
    c1, c3 = CEILINGS["C1"], CEILINGS["C3"]
    assert (c1["collection_submissions"], c1["target_identities"], c1["product_reads"]) == (2, 2, 4)
    assert (c1["connect_control_reads"], c1["connect_protected_reads"]) == (4, 4)
    assert (c1["connect_authenticate"], c1["policy_reads"]) == (0, 0)
    assert (c1["image_requests_per_attempt"], c1["image_bytes_max"]) == (13, 2 * 1024 * 1024)
    assert c1["new_image_bytes_per_attempt"] == 24 * 1024 * 1024
    assert (c3["collection_submissions"], c3["product_reads"], c3["window_min_size"]) == (3, 6, 3)


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
