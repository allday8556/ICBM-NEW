"""The M3 reconnaissance harness end to end on the synthetic storefront (no supplier contact)."""

from pathlib import Path

import httpx
import pytest

from app.core.secrets import MemorySecretStore
from scripts.m3harness import cli, fake_site
from scripts.m3harness.capture import CaptureStore
from scripts.m3harness.cli import Refused
from scripts.m3harness.ledger import Ledger, Mode, State
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


def test_the_rehearsal_stays_inside_the_caps_and_leaks_nothing(tmp_path: Path) -> None:
    root = tmp_path / "recon"
    summary = cli.rehearse(root)
    assert summary["state"] == State.COMPLETED.value
    # robots + terms; two product reads 60 s apart; two images plus one revalidation.
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


def test_real_approval_and_runs_are_refused_under_pytest(tmp_path: Path) -> None:
    root = tmp_path / "real"
    Ledger.create(
        ReconPaths(root).ledger,
        campaign_id="m3-recon-01",
        mode=Mode.REAL,
        product_url="https://kmretail.co.kr/product/x/1/",
    )

    class Clean:
        def head(self) -> str:
            return "a" * 40

        def dirty(self) -> int:
            return 0

    with pytest.raises(Refused, match=r"(?i)pytest"):
        cli.approve(root, "a" * 40, confirm=lambda _: True, checkout=Clean())  # type: ignore[arg-type]
    with pytest.raises(Refused, match=r"(?i)pytest"):
        cli.run_real(root, "A", checkout=Clean())  # type: ignore[arg-type]
    assert cli.main(["run", "--dir", str(root)]) == 2  # no --real
    assert Ledger(ReconPaths(root).ledger).counts() == {
        "PRODUCT_READ": 0,
        "IMAGE_REQUEST": 0,
        "POLICY_READ": 0,
    }


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
