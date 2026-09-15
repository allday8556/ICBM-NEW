"""The M3 reconnaissance harness end to end on the synthetic storefront (no supplier contact)."""

import contextlib
import io
import json
import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest

from app.config import AppConfig
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore, SecretStore
from integrations.suppliers import kmretail
from integrations.suppliers.base import (
    Credentials,
    ProbeResponse,
    RequestKind,
    SupplierDefinition,
)
from scripts.m3harness import cli, fake_site
from scripts.m3harness.capture import KEY_NAME, CaptureStore
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


# ---------------------------------------------------------------- the M1 connection owner
# Issue #52 comment 5687814715: M1/CONNECT is the only owner of the supplier login and session.

KM = kmretail.PROFILE.supplier_key
ZERO = {"PRODUCT_READ": 0, "IMAGE_REQUEST": 0, "POLICY_READ": 0}


class NoSupplierCalls:
    """The owner's CONNECT transport in these tests: any supplier request fails the test."""

    def fetch(
        self, definition: SupplierDefinition, *, kind: RequestKind, session: bytes | None
    ) -> ProbeResponse:
        raise AssertionError("a supplier request was made")

    def login(self, definition: SupplierDefinition, credentials: Credentials) -> bytes:
        raise AssertionError("a supplier login was made")


class RecordingStore(MemorySecretStore):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def set(self, key: str, value: str) -> None:
        self.writes.append(key)
        super().set(key, value)


def _never(campaign_id: str) -> SecretStore:
    raise AssertionError("the campaign-scoped store is never used here")


def _owner(
    tmp_path: Path, template: Path, *, login: bool = True, database: bool = True
) -> tuple[cli.ConnectionOwner, RecordingStore]:
    """An ICBM data directory standing in for the operator's, with the M1 login saved through
    ICBM's own operator action."""
    data = tmp_path / "icbm"
    data.mkdir()
    if database:
        shutil.copyfile(template, data / "icbm.db")
    store = RecordingStore()
    owner = cli.ConnectionOwner(
        config=AppConfig(data_dir=data, secret_backend="memory", log_to_file=False),
        secrets=store,
        gateway=NoSupplierCalls(),
    )
    if login:
        with cli._owner_container(owner) as container:
            container.connect.save_credentials(
                KM, username="m1-operator", password="m1-password", actor=cli.OPERATOR
            )
    store.writes.clear()
    return owner, store


def _initialized(root: Path) -> Ledger:
    return cli.init(
        root,
        campaign_id="m3-recon-01",
        product_url="https://kmretail.co.kr/product/x/1/",
        mode=Mode.REAL,
    )


def test_real_preflight_uses_the_m1_login_and_copies_nothing(
    tmp_path: Path, migrated_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "capture_key_store", _never)
    owner, store = _owner(tmp_path, migrated_template)
    root = tmp_path / "campaign"
    ledger = _initialized(root)
    checks = cli.preflight(root, owner=owner, checkout=Checkout(), blocker=_unblocked)
    assert all(passed for _, passed in checks), checks
    assert dict(checks)["icbm_login_saved"] is True
    assert ledger.state() is State.AWAITING_APPROVAL
    # No login prompt, no copy: the campaign holds its ledger and preflight record only, and
    # nothing was written to the owner's secret store.
    assert sorted(path.name for path in root.iterdir()) == ["ledger.sqlite3", "preflight.json"]
    assert store.writes == []
    assert ledger.counts() == ZERO
    with pytest.raises(SystemExit):
        cli.main(["credentials", "--dir", str(root)])


@pytest.mark.parametrize(
    ("case", "failing"),
    [
        ("no_login", {"icbm_login_saved"}),
        ("paused", {"icbm_connection_not_paused"}),
        ("icbm_running", {"icbm_not_running", "icbm_login_saved", "icbm_connection_not_paused"}),
        (
            "no_database",
            {
                "icbm_data_dir_at_head",
                "icbm_not_running",
                "icbm_login_saved",
                "icbm_connection_not_paused",
            },
        ),
    ],
)
def test_real_preflight_fails_safely_without_a_usable_m1_owner(
    case: str, failing: set[str], tmp_path: Path, migrated_template: Path
) -> None:
    owner, _ = _owner(
        tmp_path,
        migrated_template,
        login=case not in ("no_login", "no_database"),
        database=case != "no_database",
    )
    if case == "paused":
        with contextlib.closing(sqlite3.connect(owner.config.database_path)) as db:
            assert db.execute("UPDATE supplier_connections SET state = 'PAUSED'").rowcount == 1
            db.commit()
    root = tmp_path / "campaign"
    ledger = _initialized(root)
    with contextlib.ExitStack() as running:
        if case == "icbm_running":
            running.enter_context(acquire_data_dir(owner.config.data_dir, app_version="icbm"))
        checks = cli.preflight(root, owner=owner, checkout=Checkout(), blocker=_unblocked)
    assert {name for name, passed in checks if not passed} == failing
    assert ledger.state() is State.INITIALIZED
    assert not ReconPaths(root).preflight.exists()
    assert ledger.counts() == ZERO


def test_a_real_run_refuses_before_recording_or_sending_without_an_m1_login(
    tmp_path: Path, migrated_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "capture_key_store", _never)
    owner, _ = _owner(tmp_path, migrated_template, login=False)
    root = tmp_path / "real"
    ledger = _real_campaign(root)
    ledger.record("APPROVED_A", sha=SHA_A, nonce="n")
    with pytest.raises(Refused, match="icbm_login_saved"):
        cli.run_real(root, "A", checkout=Checkout(), blocker=_unblocked, owner=owner)
    # Nothing recorded, nothing sent: the approval stays usable once ICBM is fixed.
    approved = ledger.last_event("APPROVED_A")
    assert approved is not None and approved["seq"] == ledger.latest_seq()
    assert ledger.state() is State.AWAITING_APPROVAL
    assert ledger.counts() == ZERO


def test_the_rehearsal_keeps_the_login_with_the_connection_owner(tmp_path: Path) -> None:
    owner_store, capture_store = RecordingStore(), RecordingStore()
    summary = cli.rehearse(
        tmp_path / "recon", owner_secrets=owner_store, capture_secrets=capture_store
    )
    assert summary["state"] == State.COMPLETED.value
    assert capture_store.writes == [KEY_NAME]
    assert f"supplier:{fake_site.definition().profile.supplier_key}:credentials" in (
        owner_store.writes
    )
