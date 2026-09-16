"""The M3 reconnaissance harness end to end on the synthetic storefront (no supplier contact)."""

import contextlib
import io
import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import REPO_ROOT, AppConfig, database_path, default_data_dir
from app.connect.credentials import SupplierCredentialStore
from app.connect.service import ConnectService
from app.connect.sessions import SESSIONS_DIR_NAME, SupplierSessionStore
from app.core.errors import AuthError
from app.core.ownership import acquire_data_dir
from app.core.secrets import MemorySecretStore, SecretStore
from app.db.migrate import head_revision, read_only_revision
from integrations.suppliers import kmretail
from integrations.suppliers.base import (
    Credentials,
    ProbeResponse,
    RequestKind,
    SupplierDefinition,
)
from integrations.suppliers.collection import ReadKind
from scripts.m3harness import cli, fake_site, recon
from scripts.m3harness.capture import KEY_NAME, CaptureStore
from scripts.m3harness.cli import Refused
from scripts.m3harness.ledger import GUARD_VERSION, Ledger, LedgerError, Mode, State
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


def _real_campaign(root: Path, *, owner: dict[str, str] | None = None) -> Ledger:
    ledger = Ledger.create(
        ReconPaths(root).ledger,
        campaign_id="m3-recon-01",
        mode=Mode.REAL,
        product_url="https://kmretail.co.kr/product/x/1/",
    )
    ledger.record(
        "PREFLIGHT_PASSED",
        state=State.AWAITING_APPROVAL,
        head=SHA_A,
        owner=owner or {},
        digest="0" * 64,
    )
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
    # robots, the discovered terms page and the third-party image host's own robots; two product
    # reads 60 s apart; two images + one 304.
    assert summary["requests"] == {"PRODUCT_READ": 2, "IMAGE_REQUEST": 3, "POLICY_READ": 3}
    assert summary["logins"] == 1
    phase_a = summary["phase_a"]
    assert phase_a["robots"]["product_path_disallowed"] is False
    assert phase_a["stability"]["inventory_identical"] is True
    assert phase_a["observed_image_hosts"] == sorted({fake_site.HOST, *fake_site.IMAGE_HOSTS})
    assert phase_a["terms"]["mentions"] == ["무단", "수집"]
    images = summary["phase_b"]["images"]
    assert {image["signature"] for image in images} == {"png", "jpeg"}
    assert any(image.get("revalidation_status") == 304 for image in images)
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    assert ledger.observed_hosts() == frozenset({fake_site.HOST, *fake_site.IMAGE_HOSTS})
    subjects = [r["subject"] for r in ledger.reservations() if r["kind"] == "POLICY_READ"]
    # The storefront's own robots, the policy document its product page linked, and the one
    # robots document of the third-party image host the sample reaches. The storefront's image is
    # never asked again: phase A already read its rules.
    assert subjects == [
        "/robots.txt",
        "discovered:/member/agreement.html",
        f"image-robots:{fake_site.IMAGE_HOSTS[1]}",
    ]
    findings = list(paths.findings.iterdir())
    for artifact in (*findings, paths.ledger, paths.preflight):
        data = artifact.read_bytes()
        for secret in SECRETS:
            assert secret.encode("utf-8") not in data, (artifact.name, secret)
    for artifact in findings:
        assert PRODUCT_ID.encode("utf-8") not in artifact.read_bytes(), artifact.name


def test_phase_b_spends_its_sample_on_product_images_only(tmp_path: Path) -> None:
    # Issue #52 comment 5696172833 §6 and 5696242775 §2: the storefront's layout banner sits on an
    # approved image host and is still never requested, and the findings say why.
    summary = cli.rehearse(tmp_path / "recon")
    plan = summary["phase_b"]["sample_plan"]
    assert plan["rules"] == fake_site.IMAGE_ROLES.identity
    assert plan["limit"] == recon.IMAGE_SAMPLE
    assert [entry["role"] for entry in plan["selected"]] == ["DETAIL", "THUMBNAIL"]
    assert [image["role"] for image in summary["phase_b"]["images"]] == ["DETAIL", "THUMBNAIL"]
    assert {entry["reason"] for entry in plan["not_selected"]} == {"UI_COMMON"}
    assert plan["off_approved_hosts"] == [], "every reference of this page is on an approved host"
    assert "not a publication" in str(plan["reconnaissance_only"])
    for image in summary["phase_b"]["images"]:
        assert set(image) >= {"role", "host", "order", "identity", "rule"}
        assert fake_site.SIGNATURE not in str(image), "a signed URL never reaches the findings"


def test_captures_are_encrypted_at_rest(tmp_path: Path) -> None:
    root = tmp_path / "recon"
    cli.rehearse(root)
    paths = ReconPaths(root)
    blobs = list(paths.captures.glob("*.enc"))
    assert {blob.stem for blob in blobs} == {"robots", "product-1", "product-2", "terms"}
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


# ---------------------------------------------------------------- the image host's own robots
# Issue #52 ruling 5699776908 §3: an image host is a distinct origin, so phase B reads its own
# rules before it requests anything from it, and stops rather than guessing.


class _ImageHostRobots(fake_site.FakeStorefront):
    """The rehearsal storefront, with an answer of our choosing for the image host's robots."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__()
        self.status, self.body = status, body
        self.robots_reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in fake_site.IMAGE_HOSTS and request.url.path == "/robots.txt":
            self.robots_reads += 1
            self.requests.append(request)
            return httpx.Response(self.status, text=self.body)
        return super().__call__(request)


def _phase_b(tmp_path: Path, storefront: fake_site.FakeStorefront) -> dict[str, Any]:
    return cli.rehearse(tmp_path / "recon", storefront=storefront)["phase_b"]


@pytest.mark.parametrize("status", [404, 410])
def test_a_host_that_publishes_no_robots_states_no_exclusion(tmp_path: Path, status: int) -> None:
    site = _ImageHostRobots(status)
    phase_b = _phase_b(tmp_path, site)
    assert site.robots_reads == 1, "exactly one robots document, once"
    check = next(c for c in phase_b["robots"] if c["host"] == fake_site.IMAGE_HOSTS[1])
    assert check["allowed"] is True and check["star_rules"] is None
    assert "not permission to publish" in check["note"]
    assert phase_b["images"], "the sample proceeds"


def test_a_host_that_allows_the_chosen_paths_is_read(tmp_path: Path) -> None:
    site = _ImageHostRobots(200, "User-agent: *\nDisallow: /private/\nAllow: /d/\n")
    phase_b = _phase_b(tmp_path, site)
    check = next(c for c in phase_b["robots"] if c["host"] == fake_site.IMAGE_HOSTS[1])
    assert check == {
        "host": fake_site.IMAGE_HOSTS[1],
        "source": "image-host-robots",
        "requested": True,
        "http_status": 200,
        "star_rules": 2,
        "allowed": True,
    }
    assert phase_b["images"]


def test_a_disallowed_image_path_stops_phase_b_before_any_image(tmp_path: Path) -> None:
    site = _ImageHostRobots(200, "User-agent: *\nDisallow: /d/\n")
    root = tmp_path / "recon"
    phase_b = cli.rehearse(root, storefront=site)["phase_b"]
    assert phase_b["stopped"] == "ROBOTS_DISALLOWS_IMAGE_PATH"
    assert phase_b["images"] == [], "not one image was requested"
    assert phase_b["requests"]["IMAGE_REQUEST"] == 0
    ledger = Ledger(ReconPaths(root).ledger)
    assert ledger.state() is State.STOPPED
    recorded = ledger.last_event("IMAGE_HOST_ROBOTS")
    assert recorded is not None and recorded["detail"]["allowed"] is False
    assert recorded["detail"]["disallowed_paths"] == ["/d/{n}.jpg"]


@pytest.mark.parametrize("status", [401, 403, 429, 418, 451])
def test_any_other_answer_leaves_the_rules_unknown_and_stops(tmp_path: Path, status: int) -> None:
    # Unknown is a stop: an authentication, rate-limit or refusal answer is not "no rules".
    root = tmp_path / "recon"
    phase_b = cli.rehearse(root, storefront=_ImageHostRobots(status))["phase_b"]
    assert phase_b["images"] == []
    assert phase_b["requests"]["IMAGE_REQUEST"] == 0
    assert Ledger(ReconPaths(root).ledger).state() is State.STOPPED


def test_the_storefronts_own_rules_are_reused_and_never_read_again(tmp_path: Path) -> None:
    # Phase A read them and the ledger recorded it; asking the same host twice would be a request
    # nobody authorized a second time.
    root = tmp_path / "recon"
    site = fake_site.FakeStorefront()
    phase_b = cli.rehearse(root, storefront=site)["phase_b"]
    storefront = next(c for c in phase_b["robots"] if c["host"] == fake_site.HOST)
    assert storefront == {
        "host": fake_site.HOST,
        "source": "phase-a",
        "requested": False,
        "allowed": True,
    }
    subjects = [
        r["subject"]
        for r in Ledger(ReconPaths(root).ledger).reservations()
        if r["kind"] == "POLICY_READ"
    ]
    assert subjects.count(f"image-robots:{fake_site.HOST}") == 0


def test_every_robots_answer_is_recorded_whether_it_allowed_or_refused(tmp_path: Path) -> None:
    root = tmp_path / "recon"
    cli.rehearse(root, storefront=_ImageHostRobots(404))
    ledger = Ledger(ReconPaths(root).ledger)
    kinds = [json.loads(detail) for (detail,) in _events(ledger, "IMAGE_HOST_ROBOTS")]
    # In the order the sample reaches them: the detail image's host first, then the storefront.
    assert [k["host"] for k in kinds] == [fake_site.IMAGE_HOSTS[1], fake_site.HOST]
    assert [k["requested"] for k in kinds] == [True, False]


def _events(ledger: Ledger, kind: str) -> list[tuple[str]]:
    with contextlib.closing(sqlite3.connect(ledger.path)) as db:
        return list(db.execute("SELECT detail FROM events WHERE kind = ? ORDER BY seq", (kind,)))


class _RobotsTransportFails(fake_site.FakeStorefront):
    """The image host's robots document answers with something the transport refuses outright."""

    def __init__(self, status: int = 429, third_party_thumbnail: bool = False) -> None:
        super().__init__()
        self.status = status
        self.third_party_thumbnail = third_party_thumbnail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host in fake_site.IMAGE_HOSTS and url.path == "/robots.txt":
            self.requests.append(request)
            return httpx.Response(self.status)
        if (
            self.third_party_thumbnail
            and url.host == fake_site.HOST
            and url.path == (fake_site.PRODUCT_PATH)
        ):
            answer = super().__call__(request)
            if answer.status_code == 200:
                page = answer.text.replace(
                    f"//{fake_site.HOST}/web/product/small/1234.png",
                    f"//{fake_site.IMAGE_HOSTS[0]}/p/1234.png",
                )
                return httpx.Response(200, text=page, headers={"content-type": "text/html"})
            return answer
        return super().__call__(request)


def test_a_transport_refusal_is_recorded_before_it_stops_phase_b(tmp_path: Path) -> None:
    # The blocker: a rate limit, a server failure or a network failure is refused by the transport
    # before any status is read, so without this the ledger said phase B stopped but never why.
    root = tmp_path / "recon"
    phase_b = cli.rehearse(root, storefront=_RobotsTransportFails(429))["phase_b"]
    ledger = Ledger(ReconPaths(root).ledger)
    recorded = ledger.last_event("IMAGE_HOST_ROBOTS")
    assert recorded is not None
    assert recorded["detail"] == {
        "host": fake_site.IMAGE_HOSTS[1],
        "requested": True,
        "allowed": False,
        "reason": "ROBOTS_COLLECT_RATE_LIMITED",
    }
    assert ledger.state() is State.STOPPED, "the refusal still stops the campaign"
    assert ledger.counts()["IMAGE_REQUEST"] == 0, "not one image was requested"
    assert phase_b["images"] == []


def test_a_host_after_one_that_refused_is_never_consulted(tmp_path: Path) -> None:
    # Both sampled images sit on third-party hosts here, so the second is a host that would have
    # to be reached over the network. It never is: the walk ends at the first refusal.
    root = tmp_path / "recon"
    site = _RobotsTransportFails(429, third_party_thumbnail=True)
    cli.rehearse(root, storefront=site)
    ledger = Ledger(ReconPaths(root).ledger)
    hosts = [json.loads(detail)["host"] for (detail,) in _events(ledger, "IMAGE_HOST_ROBOTS")]
    assert hosts == [fake_site.IMAGE_HOSTS[1]], "only the host that refused was ever asked"
    assert fake_site.IMAGE_HOSTS[0] not in {r.url.host for r in site.requests}
    subjects = [r["subject"] for r in ledger.reservations() if r["kind"] == "POLICY_READ"]
    assert subjects.count(f"image-robots:{fake_site.IMAGE_HOSTS[0]}") == 0
    assert ledger.counts()["IMAGE_REQUEST"] == 0


def test_an_image_approval_refuses_a_ledger_whose_guard_is_older(tmp_path: Path) -> None:
    # The approval would otherwise authorize requests the ledger's own guard cannot police.
    root = tmp_path / "real"
    ledger = _waiting_for_images(root)
    with contextlib.closing(sqlite3.connect(ledger.path)) as raw:
        raw.execute("PRAGMA user_version = 1")
        raw.commit()
    with pytest.raises(Refused, match=r"reservation guard is v1"):
        cli.approve_images(
            root, SHA_A, [HOST], confirm=_yes, checkout=Checkout(), blocker=_unblocked
        )
    assert ledger.approved_hosts() == frozenset()
    assert cli.upgrade_guard(root) == {"guard_version": GUARD_VERSION, "was": 1, "upgraded": True}
    cli.approve_images(root, SHA_A, [HOST], confirm=_yes, checkout=Checkout(), blocker=_unblocked)
    approved = ledger.last_event("APPROVED_B")
    assert approved is not None and approved["detail"]["guard_version"] == GUARD_VERSION


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


def test_image_host_approval_is_bound_to_the_operators_own_clean_head(tmp_path: Path) -> None:
    # PR #60 review 5214845204 §2, as re-bound by Issue #52 ruling 5699776908: every gate phase A's
    # approval passes is kept, and the SHA it is issued at is the one phase B will run on.
    root = tmp_path / "real"
    ledger = _waiting_for_images(root)
    for checkout, sha in (
        (Checkout(dirty=1), SHA_A),  # a dirty tree
        (Checkout(head=OTHER_SHA), SHA_A),  # a SHA that is not the checked-out HEAD
        (Checkout(dirty=1, head=OTHER_SHA), OTHER_SHA),  # the right SHA, an unclean tree
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

    # The branch has moved on since phase A read the product: the approval is issued here.
    cli.approve_images(
        root,
        OTHER_SHA,
        [HOST],
        confirm=record,
        checkout=Checkout(head=OTHER_SHA),
        blocker=_unblocked,
    )
    assert typed == [f"APPROVE-IMAGES m3-recon-01 {OTHER_SHA[:12]} {HOST}"]
    event = ledger.last_event("APPROVED_B")
    assert event is not None
    detail = event["detail"]
    assert detail["sha"] == OTHER_SHA, "phase B runs on the SHA the operator approved"
    assert detail["phase_a_sha"] == SHA_A, "and the approval says which reads it rests on"
    assert ledger.approved_hosts() == frozenset({HOST})


def test_the_image_approval_carries_the_evidence_it_rests_on(tmp_path: Path) -> None:
    # What ties the approval to phase A is the observation, not the revision, so the approval
    # records it: both digests phase A bound, and the rules that will classify the candidates.
    root = tmp_path / "real"
    ledger = _waiting_for_images(root)
    cli.approve_images(root, SHA_A, [HOST], confirm=_yes, checkout=Checkout(), blocker=_unblocked)
    bound = ledger.last_event("PHASE_A_DONE")
    approved = ledger.last_event("APPROVED_B")
    assert bound is not None and approved is not None
    assert approved["detail"]["observed_hosts_digest"] == bound["detail"]["observed_hosts_digest"]
    assert approved["detail"]["findings_digest"] == bound["detail"]["findings_digest"]
    assert approved["detail"]["hosts"] == [HOST]
    identity = approved["detail"]["extraction"]
    assert identity["rules"] == kmretail.IMAGE_ROLES.identity
    assert identity["revision"] == identity["rules"], "the rules are pinned by the manifest"
    assert len(identity["fingerprint"]) == 64


def test_an_image_approval_needs_an_observation_that_still_matches_its_digest(
    tmp_path: Path,
) -> None:
    # The evidence is what carries the approval now, so a ledger whose observation no longer
    # matches the digest phase A bound cannot be approved at any SHA.
    root = tmp_path / "real"
    ledger = _waiting_for_images(root)
    # The ledger is append-only, so the observation cannot be edited away; what it can carry is a
    # later claim that disagrees with the hosts it actually holds.
    ledger.record(
        "PHASE_A_DONE",
        observed_hosts=[HOST],
        observed_hosts_digest="0" * 64,
        findings_digest="0" * 64,
    )
    with pytest.raises(Refused, match="digest"):
        cli.approve_images(
            root, SHA_A, [HOST], confirm=_yes, checkout=Checkout(), blocker=_unblocked
        )
    assert ledger.approved_hosts() == frozenset()


def test_an_image_approval_needs_a_phase_a_approval_of_its_own(tmp_path: Path) -> None:
    root = tmp_path / "real"
    ledger = _real_campaign(root)
    # A campaign that reached the image STOP without an approval of its own ever being recorded.
    ledger.record("RUN_A_STARTED", state=State.RUNNING_A)
    ledger.finish_phase_a([HOST], findings_digest="0" * 64)
    with pytest.raises(Refused, match="phase A was never approved"):
        cli.approve_images(
            root, SHA_A, [HOST], confirm=_yes, checkout=Checkout(), blocker=_unblocked
        )
    assert ledger.approved_hosts() == frozenset()


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


def _never_built(*_args: object, **_rest: object) -> object:
    raise AssertionError("no collection gateway is built during local finalization")


def _never_installed(*_args: object, **_rest: object) -> None:
    raise AssertionError("no egress guard is installed during local finalization")


def _never_connected(*_args: object, **_rest: object) -> bytes:
    raise AssertionError("no session is opened during local finalization")


def _owner(
    tmp_path: Path, template: Path, *, login: bool = True, database: bool = True
) -> tuple[cli.ConnectionOwner, RecordingStore]:
    """An ICBM data directory standing in for the operator's, with the M1 login saved through
    ICBM's own operator action."""
    data = tmp_path / "icbm"
    database_path(data).parent.mkdir(parents=True)
    if database:
        shutil.copyfile(template, database_path(data))
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
        (
            "icbm_running",
            {
                "icbm_not_running",
                "icbm_data_dir_at_head",
                "icbm_login_saved",
                "icbm_connection_not_paused",
            },
        ),
        (
            "newer_schema",
            {"icbm_data_dir_at_head", "icbm_login_saved", "icbm_connection_not_paused"},
        ),
    ],
)
def test_real_preflight_fails_safely_without_a_usable_m1_owner(
    case: str, failing: set[str], tmp_path: Path, migrated_template: Path
) -> None:
    owner, _ = _owner(tmp_path, migrated_template, login=case != "no_login")
    if case == "paused":
        with contextlib.closing(sqlite3.connect(owner.config.database_path)) as db:
            assert db.execute("UPDATE supplier_connections SET state = 'PAUSED'").rowcount == 1
            db.commit()
    if case == "newer_schema":
        with contextlib.closing(sqlite3.connect(owner.config.database_path)) as db:
            db.execute("UPDATE alembic_version SET version_num = 'ffff_from_a_newer_build'")
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
    ledger = _real_campaign(root, owner=cli.owner_identity(owner))
    ledger.record("APPROVED_A", sha=SHA_A, nonce="n")
    with pytest.raises(Refused, match="icbm_login_saved"):
        cli.run_real(root, "A", checkout=Checkout(), blocker=_unblocked, owner=owner)
    # Nothing recorded, nothing sent: the approval stays usable once ICBM is fixed.
    approved = ledger.last_event("APPROVED_A")
    assert approved is not None and approved["seq"] == ledger.latest_seq()
    assert ledger.state() is State.AWAITING_APPROVAL
    assert ledger.counts() == ZERO


@pytest.fixture
def canonical_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The operator's machine: no ICBM_DATA_DIR in the shell, and a user profile under a temporary
    location (comment 5688854287)."""
    monkeypatch.delenv("ICBM_DATA_DIR", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    return (tmp_path / "home" / "ICBM-NEW" / "data").resolve()


def test_the_recon_owner_is_icbms_own_canonical_data_root(canonical_root: Path) -> None:
    # Issue #52 comment 5688150031: nobody names a path; ICBM-NEW and the recon resolve one root.
    resolved = cli.ConnectionOwner.operator().config.data_dir
    assert resolved == AppConfig.from_env().data_dir == default_data_dir() == canonical_root
    assert not resolved.is_relative_to(REPO_ROOT)


def test_real_preflight_reuses_the_login_icbm_already_saved(
    canonical_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #62 review 5215770649 §3: the M1 login is in ICBM's secret store, which outlives any data
    # directory, and ICBM has not created its canonical data root on this machine yet. The
    # preflight finds both by itself: no path, no prompt, no copy.
    monkeypatch.setattr(cli, "capture_key_store", _never)
    store = RecordingStore()
    SupplierCredentialStore(store).save(KM, Credentials("m1-operator", "m1-password"))
    store.writes.clear()
    owner = cli.ConnectionOwner(
        config=AppConfig.from_env(), secrets=store, gateway=NoSupplierCalls()
    )
    assert not canonical_root.exists()
    root = tmp_path / "campaign"
    ledger = _initialized(root)
    checks = cli.preflight(root, owner=owner, checkout=Checkout(), blocker=_unblocked)
    assert all(passed for _, passed in checks), checks
    assert read_only_revision(database_path(canonical_root)) == head_revision()
    passed = ledger.last_event("PREFLIGHT_PASSED")
    assert passed is not None and passed["detail"]["owner"]["data_dir"] == str(canonical_root)
    assert store.writes == []
    assert sorted(path.name for path in root.iterdir()) == ["ledger.sqlite3", "preflight.json"]
    assert ledger.counts() == ZERO


def test_a_run_against_another_owner_than_the_preflight_checked_is_refused(
    tmp_path: Path, migrated_template: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "capture_key_store", _never)
    checked, _ = _owner(tmp_path / "checked", migrated_template)
    other, _ = _owner(tmp_path / "other", migrated_template)
    root = tmp_path / "real"
    ledger = _initialized(root)
    checks = cli.preflight(root, owner=checked, checkout=Checkout(), blocker=_unblocked)
    assert all(passed for _, passed in checks), checks
    ledger.record("APPROVED_A", sha=SHA_A, nonce="n")
    with pytest.raises(Refused, match="not the one the preflight checked"):
        cli.run_real(root, "A", checkout=Checkout(), blocker=_unblocked, owner=other)
    approved = ledger.last_event("APPROVED_A")
    assert approved is not None and approved["seq"] == ledger.latest_seq()
    assert ledger.counts() == ZERO


# ---------------------------------------------------------------- local phase-A finalization
# Issue #52 comment 5689874555: provider reads and local findings work are separate steps, and
# the local half can always be finished offline.


def _dry_owner(root: Path, owner_secrets: SecretStore) -> cli.ConnectionOwner:
    return cli.ConnectionOwner(
        config=cli._dry_owner_config(ReconPaths(root)),
        secrets=owner_secrets,
        gateway=fake_site.FakeConnectGateway(),
        suppliers=(fake_site.definition(),),
    )


def _stuck_after_the_reads(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, legacy: bool
) -> tuple[SecretStore, SecretStore]:
    """A campaign whose phase-A reads are done while its findings were never written: the shape a
    current run leaves (FINALIZING_A), and the one an older harness left (RUNNING_A)."""
    owner_secrets, capture_secrets = MemorySecretStore(), MemorySecretStore()
    if legacy:
        # An older harness had no reads-done event: it went straight from the reads to the write.
        record = Ledger.record

        def without_the_event(self: Ledger, kind: str, **rest: object) -> int:
            if kind == "PHASE_A_READS_DONE":
                raise RuntimeError("a harness without local-finalization states")
            return record(self, kind, **rest)  # type: ignore[arg-type]

        monkeypatch.setattr(Ledger, "record", without_the_event)
        expected: type[Exception] = RuntimeError
    else:

        def refuse(*_args: object, **_rest: object) -> Path:
            raise ValueError("findings would disclose a secret value; nothing was written")

        monkeypatch.setattr(recon, "write_findings", refuse)
        expected = ValueError
    with pytest.raises(expected):
        cli.rehearse(root, owner_secrets=owner_secrets, capture_secrets=capture_secrets)
    monkeypatch.undo()
    return owner_secrets, capture_secrets


@pytest.mark.parametrize("legacy", [False, True])
def test_phase_a_is_finished_offline_from_the_ledger_and_the_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    root = tmp_path / "recon"
    owner_secrets, capture_secrets = _stuck_after_the_reads(root, monkeypatch, legacy=legacy)
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    assert ledger.state() is (State.RUNNING_A if legacy else State.FINALIZING_A)
    # A local failure after the reads leaves its own trace; the legacy shape crashed earlier.
    assert (ledger.last_event("LOCAL_FINALIZATION_FAILED") is not None) is not legacy
    assert not (paths.findings / "phase-a.json").exists()
    assert ledger.observed_hosts() == frozenset()
    reads = ledger.counts()

    owner = _dry_owner(root, owner_secrets)
    # Nothing in this path may reach the supplier: no gateway, no egress, no session.
    monkeypatch.setattr(cli, "PolicedCollectionGateway", _never_built)
    monkeypatch.setattr(cli.EGRESS, "install", _never_installed)
    monkeypatch.setattr(ConnectService, "collection_session", _never_connected)
    findings = cli.finalize(root, owner=owner, capture_secrets=lambda: capture_secrets)

    assert ledger.state() is State.AWAITING_IMAGE_HOST_APPROVAL
    assert ledger.counts() == reads, "finalization reserves nothing"
    assert findings["observed_image_hosts"] == sorted({fake_site.HOST, *fake_site.IMAGE_HOSTS})
    assert ledger.observed_hosts() == frozenset({fake_site.HOST, *fake_site.IMAGE_HOSTS})
    assert findings["secret_scan"]["excluded_cookies"] == []
    assert json.loads((paths.findings / "phase-a.json").read_text("utf-8")) == findings
    started = ledger.last_event("OFFLINE_FINALIZATION_STARTED")
    assert started is not None and started["detail"]["source"] == "ledger+captures"
    assert findings["robots"]["star_rules"] is not None, "the robots capture was kept"
    with pytest.raises(Refused, match="already bound"):
        cli.finalize(root, owner=owner, capture_secrets=lambda: capture_secrets)


def test_offline_finalization_without_the_robots_capture_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pre-fix harness kept no robots body: the rules are recorded as unavailable, never guessed.
    root = tmp_path / "recon"
    owner_secrets, capture_secrets = _stuck_after_the_reads(root, monkeypatch, legacy=True)
    ReconPaths(root).captures.joinpath("robots.enc").unlink()
    findings = cli.finalize(
        root, owner=_dry_owner(root, owner_secrets), capture_secrets=lambda: capture_secrets
    )
    assert findings["robots"]["star_rules"] is None
    assert "not retained" in findings["robots"]["note"]
    assert findings["robots"]["product_path_disallowed"] is False
    assert findings["observed_image_hosts"] == sorted({fake_site.HOST, *fake_site.IMAGE_HOSTS})


def test_a_campaign_waiting_for_local_finalization_never_re_runs_phase_a(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated_template: Path
) -> None:
    # The reads are done, so the approval is no longer the latest event and the state machine has
    # no way back to RUNNING_A: a normal run is refused before it can send anything.
    owner, _ = _owner(tmp_path / "icbm-owner", migrated_template)
    root = tmp_path / "real"
    ledger = _real_campaign(root, owner=cli.owner_identity(owner))
    ledger.record("APPROVED_A", sha=SHA_A, nonce="n")
    ledger.record("RUN_A_STARTED", state=State.RUNNING_A)
    reservation = ledger.reserve(ReadKind.PRODUCT_READ, "https://kmretail.co.kr/product/x/1/")
    ledger.complete(reservation, http_status=200, outcome="OK")
    ledger.record("PHASE_A_READS_DONE", state=State.FINALIZING_A)
    monkeypatch.setattr(cli, "PolicedCollectionGateway", _never_built)
    with pytest.raises(Refused, match="latest event"):
        cli.run_real(root, "A", checkout=Checkout(), blocker=_unblocked, owner=owner)
    assert ledger.state() is State.FINALIZING_A
    assert ledger.counts() == {"PRODUCT_READ": 1, "IMAGE_REQUEST": 0, "POLICY_READ": 0}


def test_a_failure_rebuilding_the_evidence_is_recorded_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR #64 review 5217542767 §2: the provenance goes in first, and every later local failure
    # leaves its own trace with a class name, never a value.
    root = tmp_path / "recon"
    owner_secrets, capture_secrets = _stuck_after_the_reads(root, monkeypatch, legacy=False)
    ReconPaths(root).captures.joinpath("product-2.enc").unlink()
    ledger = Ledger(ReconPaths(root).ledger)
    with pytest.raises(Refused, match="cannot support finalization"):
        cli.finalize(
            root, owner=_dry_owner(root, owner_secrets), capture_secrets=lambda: capture_secrets
        )
    started = ledger.last_event("OFFLINE_FINALIZATION_STARTED")
    failed = ledger.last_event("LOCAL_FINALIZATION_FAILED")
    assert started is not None and failed is not None
    assert failed["seq"] > started["seq"]
    assert failed["detail"] == {"reason": "ReconStop"}
    assert ledger.state() is State.FINALIZING_A
    assert ledger.counts()["PRODUCT_READ"] == 2, "no request, no reservation"


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


def _truncated(sentinel: str) -> bytes:
    """Not parseable at all: the decoder's own failure holds the whole document."""
    return json.dumps({"v": 1, "cookies": sentinel}).encode("utf-8")[:-1]


def _malformed_entry(sentinel: str) -> bytes:
    """Parseable, of the stored version, and tolerated entry by entry by the shared decoder, but
    without the pair a scan needs (PR #64 review 5219631112)."""
    cookies = [{"name": "SID", "domain": sentinel}]
    return json.dumps({"v": 1, "cookies": cookies, "user_agent": "x"}).encode("utf-8")


@pytest.mark.parametrize("corrupt", [_truncated, _malformed_entry])
def test_an_unreadable_session_stops_finalization_without_disclosing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    corrupt: Callable[[str], bytes],
) -> None:
    # PR #64 comment 5690832285: the scan boundary fails closed, the campaign keeps a local failure
    # trace, and nothing of the payload reaches the error, the ledger, the findings or the output.
    sentinel = "SENTINEL-4d9f2a-SESSION-MATERIAL"
    root = tmp_path / "recon"
    owner_secrets, capture_secrets = _stuck_after_the_reads(root, monkeypatch, legacy=False)
    owner = _dry_owner(root, owner_secrets)
    sessions = SupplierSessionStore(owner.config.runtime_dir / SESSIONS_DIR_NAME, owner_secrets)
    sessions.save(fake_site.definition().profile.supplier_key, corrupt(sentinel))
    ledger = Ledger(ReconPaths(root).ledger)
    reads = ledger.counts()
    monkeypatch.setattr(cli, "PolicedCollectionGateway", _never_built)
    monkeypatch.setattr(cli.EGRESS, "install", _never_installed)
    monkeypatch.setattr(ConnectService, "collection_session", _never_connected)

    with pytest.raises(Refused) as refused:
        cli.finalize(root, owner=owner, capture_secrets=lambda: capture_secrets)

    assert "SUPPLIER_SESSION_UNREADABLE" in str(refused.value)
    failed = ledger.last_event("LOCAL_FINALIZATION_FAILED")
    assert failed is not None and failed["detail"] == {"reason": "AuthError"}
    assert ledger.state() is State.FINALIZING_A, "network-closed and retryable locally"
    assert ledger.counts() == reads, "no request, no reservation"
    captured = capsys.readouterr()  # one read: the first call drains both streams
    assert not any(
        sentinel.encode("utf-8") in surface
        for surface in _surfaces(ReconPaths(root), str(refused.value), captured.out, captured.err)
    )


def _surfaces(paths: ReconPaths, error: str, out: str, err: str) -> list[bytes]:
    """Everywhere a local failure could carry material it could not read."""
    written = [path.read_bytes() for path in paths.findings.rglob("*.json")]
    return [
        error.encode("utf-8"),
        paths.ledger.read_bytes(),
        out.encode("utf-8"),
        err.encode("utf-8"),
        *written,
    ]


class _RefusesTheSecondProductRead(fake_site.FakeStorefront):
    """A storefront that stops phase A after the login, with both reads already reserved."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == fake_site.HOST and request.url.path == fake_site.PRODUCT_PATH:
            self.reads += 1
            if self.reads == 2:
                self.requests.append(request)
                return httpx.Response(403)
        return super().__call__(request)


class _UnreachableImages(fake_site.FakeStorefront):
    """A storefront whose images cannot be reached: phase B stops on a provider error."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host in fake_site.IMAGE_HOSTS:
            self.requests.append(request)
            raise httpx.ConnectError("the image host is unreachable", request=request)
        return super().__call__(request)


def test_a_phase_a_stop_is_terminal_before_its_findings_are_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # PR #64 review 5217847727 §1: local work never holds a stopped campaign open, and the live
    # session decode fails neutrally. The planted sentinel stands for the payload the decoder saw.
    sentinel = "SENTINEL-7c31e8-LIVE-SESSION-MATERIAL"

    def unreadable(payload: bytes) -> tuple[list[dict[str, str]], str]:
        raise ValueError(f"cannot decode {sentinel}")

    monkeypatch.setattr(cli, "decode_session", unreadable)
    # The live session accessor keeps the boundary's own rule: the refusal is raised outside the
    # handler, so the failure that saw the payload is not kept as its context.
    with pytest.raises(AuthError) as neutral:
        cli.Session(payload=b"a session this process cannot read").cookies()
    assert neutral.value.code == "SUPPLIER_SESSION_UNREADABLE"
    assert neutral.value.__cause__ is None and neutral.value.__context__ is None
    assert sentinel not in repr(neutral.value)
    root = tmp_path / "recon"
    with pytest.raises(Refused) as refused:
        cli.rehearse(root, storefront=_RefusesTheSecondProductRead())

    assert "SUPPLIER_SESSION_UNREADABLE" in str(refused.value)
    ledger = Ledger(ReconPaths(root).ledger)
    assert ledger.state() is State.STOPPED, "the provider stop is durable on its own"
    stopped = ledger.last_event("STOPPED")
    assert stopped is not None and stopped["detail"]["reason"] == "PRODUCT_HTTP_403"
    failed = ledger.last_event("LOCAL_FINALIZATION_FAILED")
    assert failed is not None and failed["detail"] == {"reason": "AuthError"}
    assert ledger.counts() == {"PRODUCT_READ": 2, "IMAGE_REQUEST": 0, "POLICY_READ": 1}
    assert not (ReconPaths(root).findings / "phase-a.json").exists(), "nothing half-scanned"
    with pytest.raises(LedgerError, match="no transition"):
        ledger.record("RUN_A_STARTED", state=State.RUNNING_A)  # no way back into collection
    captured = capsys.readouterr()
    assert not any(
        sentinel.encode("utf-8") in surface
        for surface in _surfaces(ReconPaths(root), str(refused.value), captured.out, captured.err)
    )


def test_a_phase_b_stop_is_terminal_before_its_findings_are_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same rule on the phase-B stop, with a write failure after it and the session's own
    # cookie value planted as the material that may never surface.
    sentinel = "SENTINEL-be04f5-SESSION-COOKIE"
    monkeypatch.setattr(fake_site, "SESSION_COOKIE", sentinel)
    original = recon.write_findings

    def refusing(findings_dir: Path, name: str, findings: dict, secrets: object) -> Path:
        if name == "phase-b":
            raise ValueError("the findings cannot be written")
        return original(findings_dir, name, findings, secrets)  # type: ignore[arg-type]

    monkeypatch.setattr(recon, "write_findings", refusing)
    root = tmp_path / "recon"
    with pytest.raises(ValueError, match="cannot be written"):
        cli.rehearse(root, storefront=_UnreachableImages())

    ledger = Ledger(ReconPaths(root).ledger)
    assert ledger.state() is State.STOPPED
    stopped = ledger.last_event("STOPPED")
    assert stopped is not None and stopped["detail"]["reason"] == "COLLECT_NETWORK_ERROR"
    failed = ledger.last_event("LOCAL_FINALIZATION_FAILED")
    assert failed is not None and failed["detail"] == {"reason": "ValueError"}
    # The host could not be reached for its own robots document, so no image was ever requested.
    assert ledger.counts() == {"PRODUCT_READ": 2, "IMAGE_REQUEST": 0, "POLICY_READ": 3}
    paths = ReconPaths(root)
    assert (paths.findings / "phase-a.json").exists()
    assert not (paths.findings / "phase-b.json").exists(), "nothing half-scanned"
    with pytest.raises(LedgerError, match="no transition"):
        ledger.record("RUN_B_STARTED", state=State.RUNNING_B)
    captured = capsys.readouterr()
    assert not any(
        sentinel.encode("utf-8") in surface
        for surface in _surfaces(paths, "", captured.out, captured.err)
    )
