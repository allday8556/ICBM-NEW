"""Command line of the M3 reconnaissance harness (ADR-0010 §4, §5; Issue #52 §3).

    python scripts/m3_recon.py init --campaign-id m3-recon-01 --dir <dir> --product-url <url>
    python scripts/m3_recon.py credentials --dir <dir>
    python scripts/m3_recon.py preflight --dir <dir>
    python scripts/m3_recon.py approve --dir <dir> --sha <HEAD>
    python scripts/m3_recon.py run --dir <dir> --real
    python scripts/m3_recon.py approve-images --dir <dir> --hosts <host,host>
    python scripts/m3_recon.py run-images --dir <dir> --real
    python scripts/m3_recon.py report --dir <dir>
    python scripts/m3_recon.py rehearse --dir <dir>

* ``credentials`` stores the KM통상 login in the campaign's scoped OS credential store. The login
  is typed by the operator and never echoed.
* ``preflight`` makes zero supplier requests and ends at the approval STOP.
* ``approve`` and ``approve-images`` are the user's explicit go-ahead. Each is typed in an
  interactive terminal, bound to the campaign and the exact checked-out SHA, and refused under
  CI or pytest.
* A REAL run proceeds only right after its approval: the approval is the ledger's latest event,
  no older than two hours, at the same SHA and on a clean tree.
* ``rehearse`` runs the whole flow on the synthetic storefront. It makes no supplier contact and
  uses no OS credential store.
"""

import argparse
import getpass
import json
import os
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app import __version__
from app.config import AppConfig
from app.connect.credentials import SupplierCredentialStore
from app.container import build_container
from app.core.egress import EGRESS
from app.core.ownership import acquire_data_dir
from app.core.secrets import SERVICE_NAME, KeyringSecretStore, MemorySecretStore, SecretStore
from app.db.migrate import head_revision, read_only_revision, upgrade_to_head
from integrations.suppliers import kmretail
from integrations.suppliers.base import Credentials, SupplierDefinition, SupplierGateway
from integrations.suppliers.transport.collection import PolicedCollectionGateway, ci_or_test
from integrations.suppliers.transport.session_payload import decode_session
from scripts.m2harness.gates import GitCheckout, dedicated_problems, digest
from scripts.m2harness.keyrings import os_backend
from scripts.m3harness import fake_site
from scripts.m3harness.capture import CaptureStore
from scripts.m3harness.inventory import assert_sanitized
from scripts.m3harness.ledger import Ledger, Mode, State
from scripts.m3harness.paths import ReconPaths
from scripts.m3harness.recon import Recon, product_url_problems

APPROVAL_MAX_AGE = timedelta(hours=2)


class Refused(RuntimeError):
    """A gate refused the command; nothing was sent."""


def scoped_store(campaign_id: str) -> SecretStore:
    """The campaign's own OS credential store service; never the operator's ordinary entries."""
    return KeyringSecretStore(
        service=f"{SERVICE_NAME}/m3-recon/{campaign_id}", backend=os_backend()
    )


def _definition(mode: Mode) -> SupplierDefinition:
    return kmretail.DEFINITION if mode is Mode.REAL else fake_site.definition()


def _config(paths: ReconPaths) -> AppConfig:
    return AppConfig(data_dir=paths.data_dir, secret_backend="memory", log_to_file=False)


# ---------------------------------------------------------------- init / credentials / preflight


def init(root: Path, *, campaign_id: str, product_url: str, mode: Mode) -> Ledger:
    paths = ReconPaths(root)
    definition = _definition(mode)
    problems = product_url_problems(product_url, definition.profile)
    if mode is Mode.REAL:
        problems += dedicated_problems(root, os.environ)
    if problems:
        raise Refused("; ".join(problems))
    ledger = Ledger.create(
        paths.ledger, campaign_id=campaign_id, mode=mode, product_url=product_url
    )
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    with acquire_data_dir(paths.data_dir, app_version=__version__) as lease:
        upgrade_to_head(_config(paths).database_url, ownership=lease)
    return ledger


def store_credentials(root: Path, *, read: Callable[[str], str] = getpass.getpass) -> None:
    ledger = Ledger(ReconPaths(root).ledger)
    campaign = ledger.campaign()
    if campaign.mode is not Mode.REAL or ci_or_test() is not None:
        raise Refused("credentials are typed by the operator for a REAL campaign only")
    username = read("KM통상 login ID (not echoed): ")
    password = read("KM통상 password (not echoed): ")
    if not username or not password:
        raise Refused("both the login ID and the password are needed")
    SupplierCredentialStore(scoped_store(campaign.campaign_id)).save(
        kmretail.PROFILE.supplier_key, Credentials(username, password)
    )


def preflight(root: Path, *, checkout: GitCheckout | None = None) -> list[tuple[str, bool]]:
    """Zero supplier requests. Every check must pass before the approval STOP is reached."""
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    campaign = ledger.campaign()
    checks = [
        ("ledger_waiting", campaign.state in (State.INITIALIZED, State.AWAITING_APPROVAL)),
        ("zero_requests", not any(ledger.counts().values())),
        ("data_dir_at_head", read_only_revision(_config(paths).database_path) == head_revision()),
    ]
    head = ""
    if campaign.mode is Mode.REAL:
        checkout = checkout or GitCheckout()
        head = checkout.head()
        checks += [
            ("not_ci_or_test", ci_or_test() is None),
            ("dedicated_directory", not dedicated_problems(root, os.environ)),
            ("clean_tree", checkout.dirty() == 0),
            (
                "credentials_stored",
                SupplierCredentialStore(scoped_store(campaign.campaign_id)).stored(
                    kmretail.PROFILE.supplier_key
                ),
            ),
        ]
    if all(passed for _, passed in checks):
        document = {
            "campaign_id": campaign.campaign_id,
            "head": head,
            "checks": dict(checks),
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        paths.preflight.write_text(json.dumps(document, indent=2) + "\n", "utf-8")
        ledger.record(
            "PREFLIGHT_PASSED",
            state=State.AWAITING_APPROVAL,
            head=head,
            digest=digest(paths.preflight),
        )
    return checks


# ---------------------------------------------------------------- approvals


def approval_phrase(campaign_id: str, sha: str) -> str:
    return f"APPROVE {campaign_id} {sha[:12]} PHASE-A"


def images_phrase(campaign_id: str, hosts: Sequence[str]) -> str:
    return f"APPROVE-IMAGES {campaign_id} {','.join(sorted(hosts))}"


def approve(root: Path, sha: str, *, confirm: Callable[[str], bool], checkout: GitCheckout) -> None:
    ledger = Ledger(ReconPaths(root).ledger)
    campaign = ledger.campaign()
    problems = []
    if (blocker := ci_or_test()) is not None:
        problems.append(f"approval is never issued under {blocker}")
    if campaign.mode is not Mode.REAL:
        problems.append("only a REAL campaign takes approval")
    if campaign.state is not State.AWAITING_APPROVAL:
        problems.append(f"the campaign is {campaign.state}, not waiting for approval")
    if checkout.head() != sha or checkout.dirty():
        problems.append("the approved SHA must be the clean, checked-out HEAD")
    passed = ledger.last_event("PREFLIGHT_PASSED")
    if passed is None or passed["detail"].get("head") != sha:
        problems.append("the preflight did not pass at this SHA")
    if problems:
        raise Refused("; ".join(problems))
    if not confirm(approval_phrase(campaign.campaign_id, sha)):
        raise Refused("the operator did not type the approval phrase")
    ledger.record("APPROVED_A", sha=sha, nonce=secrets.token_hex(8))


def approve_images(root: Path, hosts: Sequence[str], *, confirm: Callable[[str], bool]) -> None:
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    campaign = ledger.campaign()
    if campaign.mode is Mode.REAL and (blocker := ci_or_test()) is not None:
        raise Refused(f"approval is never issued under {blocker}")
    if campaign.state is not State.AWAITING_IMAGE_HOST_APPROVAL:
        raise Refused(f"the campaign is {campaign.state}, not waiting for image hosts")
    observed = set(_findings(paths, "phase-a").get("observed_image_hosts", []))
    if not hosts or not set(hosts) <= observed:
        raise Refused("only image hosts observed in phase A can be approved")
    if not confirm(images_phrase(campaign.campaign_id, hosts)):
        raise Refused("the operator did not type the image-host phrase")
    ledger.approve_hosts(hosts)
    ledger.record("APPROVED_B", hosts=sorted(hosts), nonce=secrets.token_hex(8))


def _consume(ledger: Ledger, kind: str, checkout: GitCheckout | None) -> None:
    """A REAL run proceeds only right after its own approval (latest event, fresh, same SHA)."""
    campaign = ledger.campaign()
    event = ledger.last_event(kind)
    problems = []
    if event is None or event["seq"] != ledger.latest_seq():
        problems.append("the approval is not the ledger's latest event")
    elif datetime.now(UTC) - datetime.fromisoformat(event["at"]) > APPROVAL_MAX_AGE:
        problems.append("the approval is older than two hours")
    if campaign.mode is Mode.REAL:
        if (blocker := ci_or_test()) is not None:
            problems.append(f"a REAL run never happens under {blocker}")
        approved = ledger.last_event("APPROVED_A")
        sha = approved["detail"].get("sha") if approved else None
        assert checkout is not None
        if checkout.head() != sha or checkout.dirty():
            problems.append("the checkout is not the clean approved SHA")
    if problems:
        raise Refused("; ".join(problems))


# ---------------------------------------------------------------- runs


@dataclass
class Session:
    """The authenticated session of this process, kept in memory only."""

    payload: bytes | None = None

    def cookie_values(self) -> list[str]:
        if self.payload is None:
            return []
        cookies, _ = decode_session(self.payload)
        return [cookie["value"] for cookie in cookies]


def _run(
    root: Path,
    phase: str,
    *,
    store: SecretStore,
    connect_gateway: SupplierGateway | None,
    collection: PolicedCollectionGateway,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    campaign = ledger.campaign()
    definition = _definition(campaign.mode)
    key = definition.profile.supplier_key
    session = Session()
    config = _config(paths)
    with acquire_data_dir(paths.data_dir, app_version=__version__) as lease:
        container = build_container(
            config,
            ownership=lease,
            secret_store=store,
            supplier_gateway=connect_gateway,
            suppliers=(definition,),
        )
        try:

            def connect() -> bytes:
                session.payload = container.connect.collection_session(key, operator_initiated=True)
                return session.payload

            def secret_values() -> list[str]:
                stored = SupplierCredentialStore(store).load(key)
                login = [stored.username, stored.password] if stored else []
                return [*login, *session.cookie_values()]

            recon = Recon(
                ledger=ledger,
                gateway=collection,
                supplier=definition.profile,
                captures=CaptureStore(paths.captures, store, campaign_id=campaign.campaign_id),
                findings_dir=paths.findings,
                session=connect,
                secrets=secret_values,
            )
            if clock is not None:
                recon.clock = clock
                recon.budget.clock = clock
            if sleep is not None:
                recon.sleep = sleep
            return recon.phase_a() if phase == "A" else recon.phase_b()
        finally:
            container.db.dispose()


def run_real(root: Path, phase: str, *, checkout: GitCheckout) -> dict[str, Any]:
    ledger = Ledger(ReconPaths(root).ledger)
    campaign = ledger.campaign()
    if campaign.mode is not Mode.REAL:
        raise Refused("a REAL run needs a REAL campaign")
    if (blocker := ci_or_test()) is not None:
        raise Refused(f"a REAL run never happens under {blocker}")
    _consume(ledger, "APPROVED_A" if phase == "A" else "APPROVED_B", checkout)
    ledger.record(
        f"RUN_{phase}_STARTED", state=State.RUNNING_A if phase == "A" else State.RUNNING_B
    )
    EGRESS.install()
    return _run(
        root,
        phase,
        store=scoped_store(campaign.campaign_id),
        connect_gateway=None,  # the product's own CONNECT transport (M1)
        collection=PolicedCollectionGateway(),
    )


# ---------------------------------------------------------------- report


def _findings(paths: ReconPaths, name: str) -> dict[str, Any]:
    path = paths.findings / f"{name}.json"
    return json.loads(path.read_text("utf-8")) if path.is_file() else {}


def report(root: Path, *, secret_values: Sequence[str] = ()) -> Path:
    """The sanitized reconnaissance record for review on Issue #52 (ruling on Q5)."""
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    campaign = ledger.campaign()
    document = {
        "campaign_id": campaign.campaign_id,
        "mode": campaign.mode.value,
        "state": campaign.state.value,
        "requests": ledger.counts(),
        "phase_a": _findings(paths, "phase-a"),
        "phase_b": _findings(paths, "phase-b"),
    }
    lines = [
        f"# M3 reconnaissance — `{campaign.campaign_id}` ({campaign.mode.value})",
        "",
        f"- State: `{campaign.state.value}`",
        f"- Requests: {json.dumps(ledger.counts())} (caps: product 4, image 30, policy 3)",
        "- Raw pages stay encrypted in the campaign directory; this record holds structure only.",
        "",
        "```json",
        json.dumps(document, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    text = "\n".join(lines)
    assert_sanitized(text, secret_values)
    path = paths.findings / "reconnaissance.md"
    paths.findings.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


# ---------------------------------------------------------------- DRY rehearsal


def rehearse(root: Path, *, storefront: fake_site.FakeStorefront | None = None) -> dict[str, Any]:
    """The whole reconnaissance on the synthetic storefront: no supplier, no OS keyring."""
    ledger = init(
        root, campaign_id="m3-recon-dry", product_url=fake_site.PRODUCT_URL, mode=Mode.DRY
    )
    store = MemorySecretStore()
    SupplierCredentialStore(store).save(
        fake_site.definition().profile.supplier_key,
        Credentials(fake_site.USERNAME, fake_site.PASSWORD),
    )
    if not all(passed for _, passed in preflight(root)):
        raise Refused("the DRY preflight failed")
    ledger.record("APPROVED_A", sha="dry", nonce="dry")
    ledger.record("RUN_A_STARTED", state=State.RUNNING_A)
    now = [1_000_000.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    site = storefront or fake_site.FakeStorefront()
    collection = PolicedCollectionGateway(http_transport=site.transport())
    connect_gateway = fake_site.FakeConnectGateway()
    common = {"store": store, "connect_gateway": connect_gateway, "collection": collection}
    phase_a = _run(root, "A", clock=clock, sleep=sleep, **common)  # type: ignore[arg-type]
    phase_b: dict[str, Any] = {}
    if ledger.state() is State.AWAITING_IMAGE_HOST_APPROVAL:
        approve_images(root, phase_a["observed_image_hosts"], confirm=lambda _: True)
        ledger.record("RUN_B_STARTED", state=State.RUNNING_B)
        phase_b = _run(root, "B", clock=clock, sleep=sleep, **common)  # type: ignore[arg-type]
    secret_values = [fake_site.USERNAME, fake_site.PASSWORD, fake_site.SESSION_COOKIE]
    path = report(root, secret_values=secret_values)
    return {
        "state": ledger.state().value,
        "requests": ledger.counts(),
        "logins": connect_gateway.logins,
        "phase_a": phase_a,
        "phase_b": phase_b,
        "report": str(path),
        "storefront_requests": len(site.requests),
    }


# ---------------------------------------------------------------- entry point


def _typed(phrase: str) -> bool:
    if not sys.stdin.isatty():
        return False
    print(f"Type exactly: {phrase}")
    return input("> ").strip() == phrase


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m3_recon", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "init",
        "credentials",
        "preflight",
        "approve",
        "run",
        "approve-images",
        "run-images",
        "report",
        "rehearse",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("--dir", type=Path, required=True)
        if name == "init":
            sub.add_argument("--campaign-id", required=True)
            sub.add_argument("--product-url", required=True)
        if name == "approve":
            sub.add_argument("--sha", required=True)
        if name == "approve-images":
            sub.add_argument("--hosts", required=True)
        if name in ("run", "run-images"):
            sub.add_argument("--real", action="store_true")
    args = parser.parse_args(argv)
    root = args.dir.resolve()
    try:
        if args.command == "init":
            init(root, campaign_id=args.campaign_id, product_url=args.product_url, mode=Mode.REAL)
            print(f"initialized {args.campaign_id}: zero supplier requests")
        elif args.command == "credentials":
            store_credentials(root)
            print("stored in the campaign's scoped credential store (not shown)")
        elif args.command == "preflight":
            checks = preflight(root)
            for name, passed in checks:
                print(f"[{'PASS' if passed else 'FAIL'}] {name}")
            if not all(passed for _, passed in checks):
                return 1
            print("preflight PASSED: waiting at the approval STOP (zero supplier requests)")
        elif args.command == "approve":
            approve(root, args.sha, confirm=_typed, checkout=GitCheckout())
            print("approved: run phase A with `run --real` within two hours")
        elif args.command == "approve-images":
            hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
            approve_images(root, hosts, confirm=_typed)
            print("image hosts approved: run phase B with `run-images --real`")
        elif args.command in ("run", "run-images"):
            if not args.real:
                raise Refused("a real reconnaissance run needs --real")
            phase = "A" if args.command == "run" else "B"
            findings = run_real(root, phase, checkout=GitCheckout())
            print(
                json.dumps(
                    {"stopped": findings.get("stopped"), "requests": findings.get("requests")}
                )
            )
        elif args.command == "report":
            print(report(root))
        elif args.command == "rehearse":
            summary = rehearse(root)
            print(json.dumps({k: summary[k] for k in ("state", "requests", "report")}))
    except Refused as refused:
        print(f"REFUSED: {refused}", file=sys.stderr)
        return 2
    return 0
