"""The M3 reconnaissance harness end to end on the synthetic storefront (no supplier contact)."""

import contextlib
import io
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.core.secrets import MemorySecretStore
from scripts.m3harness import cli, fake_site
from scripts.m3harness.capture import CaptureStore
from scripts.m3harness.cli import Refused
from scripts.m3harness.ledger import Ledger, LedgerError, Mode, State
from scripts.m3harness.paths import ReconPaths

pytestmark = pytest.mark.integration

# Never anywhere in the campaign directory outside the encrypted captures.
SECRETS = (
    fake_site.MEMBER_NAME,
    fake_site.PRICE_TEXT,
    fake_site.HIDDEN_TOKEN,
    fake_site.SIGNATURE,
    fake_site.SESSION_COOKIE,
    fake_site.USERNAME,
    fake_site.PASSWORD,
)
# The local ledger keeps the canonical product URL; the findings for review mask its digits.
PRODUCT_ID = "1234"
SHA_A = "a" * 40
OTHER_SHA = "b" * 40
HOST = "img.kmretail.invalid"


class Checkout:
    def __init__(self, head: str = SHA_A, dirty: int = 0) -> None:
        self._head, self._dirty = head, dirty

    def head(self) -> str:
        return self._head

    def dirty(self) -> int:
        return self._dirty


def _unblocked() -> None:
    """Stands in for the CI/pytest refusal so the other gates can be exercised."""
    return None


def _yes(phrase: str) -> bool:
    return True


def _real_campaign(root: Path) -> Ledger:
    ledger = Ledger.create(
        ReconPaths(root).ledger,
        campaign_id="m3-recon-01",
        mode=Mode.REAL,
        product_url="https://kmretail.co.kr/product/x/1/",
    )
    ledger.record("PREFLIGHT_PASSED", state=State.AWAITING_APPROVAL, head=SHA_A, digest="0" * 64)
    return ledger


def _waiting_for_images(root: Path) -> Ledger:
    ledger = _real_campaign(root)
    ledger.record("APPROVED_A", sha=SHA_A, nonce="n")
    ledger.record("RUN_A_STARTED", state=State.RUNNING_A)
    ledger.finish_phase_a([HOST], findings_digest="0" * 64)
    return ledger


# ---------------------------------------------------------------- the DRY rehearsal


def test_the_rehearsal_stays_inside_the_caps_and_leaks_nothing(tmp_path: Path) -> None:
    root = tmp_path / "recon"
    summary = cli.rehearse(root)
    assert summary["state"] == State.COMPLETED.value
    # robots + the discovered terms page; two product reads 60 s apart; two images + one 304.
    assert summary["requests"] == {"PRODUCT_READ": 2, "IMAGE_REQUEST": 3, "POLICY_READ": 2}
    assert summary["logins"] == 1
    phase_a = summary["phase_a"]
    assert phase_a["robots"]["product_path_disallowed"] is False
    assert phase_a["stability"]["inventory_identical"] is True
    assert phase_a["observed_image_hosts"] == sorted(fake_site.IMAGE_HOSTS)
    assert phase_a["terms"]["mentions"] == ["무단", "수집"]
    images = summary["phase_b"]["images"]
    assert {image["signature"] for image in images} == {"png", "jpeg"}
    assert any(image.get("revalidation_status") == 304 for image in images)
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    assert ledger.observed_hosts() == frozenset(fake_site.IMAGE_HOSTS)
    subjects = [r["subject"] for r in ledger.reservations() if r["kind"] == "POLICY_READ"]
    assert subjects == ["/robots.txt", "discovered:/member/agreement.html"]
    findings = list(paths.findings.iterdir())
    for artifact in (*findings, paths.ledger, paths.preflight):
        data = artifact.read_bytes()
        for secret in SECRETS:
            assert secret.encode("utf-8") not in data, (artifact.name, secret)
    for artifact in findings:
        assert PRODUCT_ID.encode("utf-8") not in artifact.read_bytes(), artifact.name


def test_captures_are_encrypted_at_rest(tmp_path: Path) -> None:
    root = tmp_path / "recon"
    cli.rehearse(root)
    paths = ReconPaths(root)
    blobs = list(paths.captures.glob("*.enc"))
    assert {blob.stem for blob in blobs} == {"product-1", "product-2", "terms"}
    for blob in blobs:
        assert fake_site.MEMBER_NAME.encode("utf-8") not in blob.read_bytes()
    # Another store (another key) cannot read them.
    with pytest.raises(Exception, match=r"authenticate|gone"):
        CaptureStore(paths.captures, MemorySecretStore(), campaign_id="m3-recon-dry").load(
            "product-1"
        )


def test_robots_disallowing_the_product_path_stops_before_any_product_read(
    tmp_path: Path,
) -> None:
    class Disallowing(fake_site.FakeStorefront):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                self.requests.append(request)
                return httpx.Response(200, text="User-agent: *\nDisallow: /product/\n")
            return super().__call__(request)

    summary = cli.rehearse(tmp_path / "recon", storefront=Disallowing())
    assert summary["state"] == State.STOPPED.value
    assert summary["phase_a"]["stopped"] == "ROBOTS_DISALLOWS_PRODUCT_PATH"
    assert summary["requests"] == {"PRODUCT_READ": 0, "IMAGE_REQUEST": 0, "POLICY_READ": 1}
    assert summary["logins"] == 0


# ---------------------------------------------------------------- REAL gates


def test_real_approval_and_runs_are_refused_under_pytest(tmp_path: Path) -> None:
    root = tmp_path / "real"
    _real_campaign(root)
    with pytest.raises(Refused, match=r"never (issued|happens) under"):
        cli.approve(root, SHA_A, confirm=_yes, checkout=Checkout())
    with pytest.raises(Refused, match=r"never (issued|happens) under"):
        cli.run_real(root, "A", checkout=Checkout())
    assert cli.main(["run", "--dir", str(root)]) == 2  # no --real
    assert Ledger(ReconPaths(root).ledger).counts() == {
        "PRODUCT_READ": 0,
        "IMAGE_REQUEST": 0,
        "POLICY_READ": 0,
    }


def test_image_host_approval_is_bound_to_the_clean_phase_a_sha(tmp_path: Path) -> None:
    # PR #60 review 5214845204 §2: the same strength as phase A, at the same SHA.
    root = tmp_path / "real"
    ledger = _waiting_for_images(root)
    for checkout, sha in (
        (Checkout(dirty=1), SHA_A),  # a dirty tree
        (Checkout(head=OTHER_SHA), SHA_A),  # a moved HEAD
        (Checkout(head=OTHER_SHA), OTHER_SHA),  # clean, but not phase A's SHA
    ):
        with pytest.raises(Refused, match="SHA"):
            cli.approve_images(
                root, sha, [HOST], confirm=_yes, checkout=checkout, blocker=_unblocked
            )
    with pytest.raises(Refused, match=r"never issued under"):
        cli.approve_images(root, SHA_A, [HOST], confirm=_yes, checkout=Checkout())
    assert ledger.approved_hosts() == frozenset()
    typed: list[str] = []

    def record(phrase: str) -> bool:
        typed.append(phrase)
        return True

    cli.approve_images(root, SHA_A, [HOST], confirm=record, checkout=Checkout(), blocker=_unblocked)
    assert typed == [f"APPROVE-IMAGES m3-recon-01 {SHA_A[:12]} {HOST}"]
    event = ledger.last_event("APPROVED_B")
    assert event is not None and event["detail"]["sha"] == SHA_A
    assert ledger.approved_hosts() == frozenset({HOST})


def test_only_hosts_the_ledger_observed_can_be_approved(tmp_path: Path) -> None:
    # PR #60 review 5214845204 §1: a mutable findings file is never the authority.
    root = tmp_path / "real"
    ledger = _waiting_for_images(root)
    paths = ReconPaths(root)
    paths.findings.mkdir(parents=True, exist_ok=True)
    tampered = {"observed_image_hosts": [HOST, "evil.invalid"]}
    (paths.findings / "phase-a.json").write_text(json.dumps(tampered), "utf-8")
    with pytest.raises(Refused, match="observed"):
        cli.approve_images(
            root, SHA_A, ["evil.invalid"], confirm=_yes, checkout=Checkout(), blocker=_unblocked
        )
    with pytest.raises(LedgerError, match="HOST_NOT_OBSERVED"):
        ledger.approve_hosts(["evil.invalid"])
    with (
        contextlib.closing(sqlite3.connect(ledger.path)) as raw,
        pytest.raises(sqlite3.IntegrityError, match="OBSERVATION_CLOSED"),
    ):
        raw.execute("INSERT INTO observed_hosts VALUES ('evil.invalid', 't')")
    assert ledger.approved_hosts() == frozenset()


def test_phase_b_is_refused_when_the_checkout_moves_after_approval(tmp_path: Path) -> None:
    root = tmp_path / "real"
    ledger = _waiting_for_images(root)
    cli.approve_images(root, SHA_A, [HOST], confirm=_yes, checkout=Checkout(), blocker=_unblocked)
    for checkout in (Checkout(head=OTHER_SHA), Checkout(dirty=2)):
        with pytest.raises(Refused, match="clean SHA"):
            cli.run_real(root, "B", checkout=checkout, blocker=_unblocked)
    assert ledger.state() is State.AWAITING_IMAGE_HOST_APPROVAL
    assert ledger.counts()["IMAGE_REQUEST"] == 0


def test_approvals_refuse_a_non_interactive_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #60 follow-up 5686950430 §1: a non-TTY stdin never approves, even past the CI gate.
    def no_input(prompt: str = "") -> str:
        raise AssertionError("input() is never reached without a terminal")

    monkeypatch.setattr("sys.stdin", io.StringIO(f"APPROVE m3-recon-01 {SHA_A[:12]} PHASE-A\n"))
    monkeypatch.setattr("builtins.input", no_input)
    assert cli._typed(f"APPROVE m3-recon-01 {SHA_A[:12]} PHASE-A") is False
    phase_a = tmp_path / "a"
    _real_campaign(phase_a)
    with pytest.raises(Refused, match="did not type"):
        cli.approve(phase_a, SHA_A, confirm=cli._typed, checkout=Checkout(), blocker=_unblocked)
    phase_b = tmp_path / "b"
    _waiting_for_images(phase_b)
    with pytest.raises(Refused, match="did not type"):
        cli.approve_images(
            phase_b, SHA_A, [HOST], confirm=cli._typed, checkout=Checkout(), blocker=_unblocked
        )
    assert Ledger(ReconPaths(phase_a).ledger).last_event("APPROVED_A") is None
    assert Ledger(ReconPaths(phase_b).ledger).last_event("APPROVED_B") is None


def test_a_product_url_off_the_storefront_is_refused_at_init(tmp_path: Path) -> None:
    for url in (
        "https://other.invalid/product/1/",
        f"https://{fake_site.HOST}/product/1/?token=abc",
        f"http://{fake_site.HOST}/product/1/",
    ):
        with pytest.raises(Refused):
            cli.init(
                tmp_path / url.split("/")[2],
                campaign_id="m3-recon-x",
                product_url=url,
                mode=Mode.DRY,
            )
