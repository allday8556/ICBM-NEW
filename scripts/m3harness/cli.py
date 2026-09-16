"""Command line of the M3 reconnaissance harness (ADR-0010 §4, §5; Issue #52 §3).

    python scripts/m3_recon.py init --campaign-id m3-recon-01 --dir <dir> --product-url <url>
    python scripts/m3_recon.py preflight --dir <dir>
    python scripts/m3_recon.py approve --dir <dir> --sha <HEAD>
    python scripts/m3_recon.py run --dir <dir> --real
    python scripts/m3_recon.py finalize --dir <dir>
    python scripts/m3_recon.py approve-images --dir <dir> --sha <HEAD> --hosts <host,host>
    python scripts/m3_recon.py run-images --dir <dir> --real
    python scripts/m3_recon.py report --dir <dir>
    python scripts/m3_recon.py rehearse --dir <dir>

* The supplier login, the connection state and the session belong to the M1 connection owner:
  the operator's own ICBM data directory and OS secret store, read exactly as ``icbm serve`` reads
  them (Issue #52 comment 5687814715). The harness never asks for, copies or stores a login, a
  session cookie or an auth header. A REAL run borrows the session through
  ``ConnectService.collection_session()`` and keeps it in memory only. The campaign directory
  holds the ledger, the encrypted captures and the sanitized findings. The campaign-scoped OS
  secret-store service holds only the capture key.
* The owner is found, never named. ``AppConfig.from_env()`` resolves ICBM-NEW's canonical data
  root (``app.config.default_data_dir``) exactly as ``icbm serve`` does, and the harness has no
  option for another one (Issue #52 comment 5688150031).
* ``preflight`` makes zero supplier requests and ends at the approval STOP. For the connection
  owner it checks, without contacting the supplier:
  - ICBM is not running on its data directory (one process per data directory, ADR-0006);
  - its database is at the schema head, brought there as ``icbm db upgrade`` would;
  - the supplier login is saved in ICBM;
  - the connection is not paused.

  It records the owner it checked, and a run against any other owner is refused.
* ``approve`` and ``approve-images`` are the user's explicit go-ahead, and they share one gate
  owner (PR #60 review 5214845204 §2). Each needs:
  - an interactive operator (a non-TTY stdin never approves);
  - no CI or pytest run;
  - a REAL campaign at the right STOP;
  - the exact clean checked-out HEAD, typed into the phrase.

  Image hosts are approved at the same SHA as phase A, and only among the hosts the ledger
  recorded as observed in phase A. The findings file is never the authority.
* A REAL run proceeds only right after its own approval. That approval must be the ledger's
  latest event, no older than two hours, and the checkout must still be the clean SHA it was
  issued at. The connection owner is checked again before anything is recorded or sent.
* ``finalize`` finishes phase A locally once its reads are done (Issue #52 comment 5689874555).
  It rebuilds the findings from the append-only ledger and the encrypted captures, scans them,
  writes them and binds the observation. It builds no collection gateway, installs no egress,
  opens no session and reserves nothing, so it cannot reach the supplier. It runs only while the
  campaign waits for that local work, and never twice.
* ``rehearse`` runs the whole flow on the synthetic storefront, with a stand-in connection owner.
  It makes no supplier contact and uses no OS credential store.
"""

import argparse
import json
import os
import re
import secrets
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.config import AppConfig, ConfigError
from app.connect.credentials import SupplierCredentialStore
from app.connect.state import ConnectionState
from app.container import Container, build_container
from app.core.egress import EGRESS
from app.core.ownership import DataDirOwnershipError, acquire_data_dir
from app.core.secrets import SERVICE_NAME, KeyringSecretStore, MemorySecretStore, SecretStore
from app.db.migrate import upgrade_to_head
from integrations.suppliers import kmretail
from integrations.suppliers.base import SupplierDefinition, SupplierGateway
from integrations.suppliers.registry import SUPPLIERS
from integrations.suppliers.transport.collection import PolicedCollectionGateway, ci_or_test
from integrations.suppliers.transport.session_payload import decode_session
from scripts.m2harness.gates import Checkout, GitCheckout, dedicated_problems, digest
from scripts.m2harness.keyrings import os_backend
from scripts.m3harness import fake_site
from scripts.m3harness.capture import CaptureStore
from scripts.m3harness.inventory import FindingsSecrets, assert_sanitized, findings_secrets
from scripts.m3harness.ledger import Ledger, Mode, State
from scripts.m3harness.paths import ReconPaths
from scripts.m3harness.recon import (
    Recon,
    ReconStop,
    evidence_from_captures,
    finalize_phase_a,
    product_url_problems,
)

APPROVAL_MAX_AGE = timedelta(hours=2)
SHA = re.compile(r"^[0-9a-f]{40}$")
# Why this process may not approve or run for real (CI or a test run); None when it may. The
# CLI always uses ``ci_or_test``; tests substitute it to reach the other gates.
Blocker = Callable[[], str | None]
OPERATOR = "operator:local"


class Refused(RuntimeError):
    """A gate refused the command; nothing was sent."""


def capture_key_store(campaign_id: str) -> SecretStore:
    """The campaign's own OS secret-store service. It holds only the capture key: never a
    supplier login, session or auth header, which stay with the M1 connection owner."""
    return KeyringSecretStore(
        service=f"{SERVICE_NAME}/m3-recon/{campaign_id}", backend=os_backend()
    )


def _definition(mode: Mode) -> SupplierDefinition:
    return kmretail.DEFINITION if mode is Mode.REAL else fake_site.definition()


# ---------------------------------------------------------------- the M1 connection owner


@dataclass(frozen=True)
class ConnectionOwner:
    """The M1 connection owner whose session the reconnaissance borrows (ADR-0010 §3, §11). It is
    the ICBM data directory and secret store that hold the supplier login, the connection state
    and the session. A REAL campaign uses the operator's own ICBM configuration; the DRY
    rehearsal and the tests stand one in."""

    config: AppConfig
    # None: the store the configuration names (the OS secret store for the operator).
    secrets: SecretStore | None = None
    # None: the product's own CONNECT transport (M1).
    gateway: SupplierGateway | None = None
    suppliers: tuple[SupplierDefinition, ...] = SUPPLIERS

    @classmethod
    def operator(cls) -> "ConnectionOwner":
        """The operator's ICBM configuration, read exactly as ``icbm serve`` reads it."""
        try:
            return cls(config=AppConfig.from_env())
        except ConfigError as exc:
            raise Refused(f"the ICBM configuration is invalid ({exc})") from None


class OwnerUnavailable(Refused):
    """The connection owner cannot be opened: ICBM is running on its data directory, or its
    database cannot be brought to the schema head."""


class OwnerBusy(OwnerUnavailable):
    """ICBM is running on the owner's data directory (one process per data directory)."""


def owner_identity(owner: ConnectionOwner) -> dict[str, str]:
    """What binds a campaign to its connection owner: the resolved data directory and the secret
    store the login is read from. The preflight records it, and a run against any other owner is
    refused, so a campaign can never move to a different owner after its checks."""
    return {
        "data_dir": str(owner.config.data_dir.resolve()),
        "secret_backend": owner.config.secret_backend,
    }


@contextmanager
def _owner_container(owner: ConnectionOwner) -> Iterator[Container]:
    """The connection owner's own services, under its data-directory lease (ADR-0006). Its
    database is brought to the schema head exactly as ``icbm db upgrade`` does, so a canonical
    data root that ICBM has not created yet is created here. No supplier is contacted."""
    try:
        lease = acquire_data_dir(owner.config.data_dir, app_version=__version__)
    except DataDirOwnershipError:
        raise OwnerBusy("ICBM is running on its data directory; stop it first") from None
    with lease:
        try:
            upgrade_to_head(owner.config.database_url, ownership=lease)
        except (CommandError, SQLAlchemyError) as exc:
            raise OwnerUnavailable(
                f"the ICBM database cannot be brought to the schema head ({type(exc).__name__})"
            ) from None
        container = build_container(
            owner.config,
            ownership=lease,
            secret_store=owner.secrets,
            supplier_gateway=owner.gateway,
            suppliers=owner.suppliers,
        )
        try:
            yield container
        finally:
            container.db.dispose()


def _owner_state(container: Container, supplier_key: str) -> list[tuple[str, bool]]:
    """What the owner itself reports, without a supplier request: its login and its state."""
    summary = container.connect.supplier_connection(supplier_key)
    return [
        ("icbm_login_saved", summary.credentials_stored),
        ("icbm_connection_not_paused", summary.state is not ConnectionState.PAUSED),
    ]


def owner_checks(owner: ConnectionOwner, supplier_key: str) -> list[tuple[str, bool]]:
    """Whether the connection owner can provide the session, with zero supplier requests. A check
    that cannot be made fails."""
    unknown = [("icbm_login_saved", False), ("icbm_connection_not_paused", False)]
    try:
        with _owner_container(owner) as container:
            state = _owner_state(container, supplier_key)
    except OwnerBusy:
        return [("icbm_not_running", False), ("icbm_data_dir_at_head", False), *unknown]
    except OwnerUnavailable:
        return [("icbm_not_running", True), ("icbm_data_dir_at_head", False), *unknown]
    return [("icbm_not_running", True), ("icbm_data_dir_at_head", True), *state]


# ---------------------------------------------------------------- init / preflight


def _dry_owner_config(paths: ReconPaths) -> AppConfig:
    return AppConfig(data_dir=paths.data_dir, secret_backend="memory", log_to_file=False)


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
    if mode is Mode.DRY:
        # Only the rehearsal has a data directory of its own, for its stand-in connection owner.
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        with acquire_data_dir(paths.data_dir, app_version=__version__) as lease:
            upgrade_to_head(_dry_owner_config(paths).database_url, ownership=lease)
    return ledger


def preflight(
    root: Path,
    *,
    owner: ConnectionOwner | None = None,
    checkout: Checkout | None = None,
    blocker: Blocker = ci_or_test,
) -> list[tuple[str, bool]]:
    """Zero supplier requests. Every check must pass before the approval STOP is reached."""
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    campaign = ledger.campaign()
    if owner is None:
        if campaign.mode is not Mode.REAL:
            raise Refused("a DRY preflight names its stand-in connection owner")
        owner = ConnectionOwner.operator()
    checks = [
        ("ledger_waiting", campaign.state in (State.INITIALIZED, State.AWAITING_APPROVAL)),
        ("zero_requests", not any(ledger.counts().values())),
    ]
    head = ""
    if campaign.mode is Mode.REAL:
        checkout = checkout or GitCheckout()
        head = checkout.head()
        checks += [
            ("not_ci_or_test", blocker() is None),
            ("dedicated_directory", not dedicated_problems(root, os.environ)),
            ("clean_tree", checkout.dirty() == 0),
        ]
    checks += owner_checks(owner, _definition(campaign.mode).profile.supplier_key)
    if all(passed for _, passed in checks):
        document = {
            "campaign_id": campaign.campaign_id,
            "head": head,
            "owner": owner_identity(owner),
            "checks": dict(checks),
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        paths.preflight.write_text(json.dumps(document, indent=2) + "\n", "utf-8")
        ledger.record(
            "PREFLIGHT_PASSED",
            state=State.AWAITING_APPROVAL,
            head=head,
            owner=owner_identity(owner),
            digest=digest(paths.preflight),
        )
    return checks


# ---------------------------------------------------------------- approvals (one gate owner)


def approval_phrase(campaign_id: str, sha: str) -> str:
    return f"APPROVE {campaign_id} {sha[:12]} PHASE-A"


def images_phrase(campaign_id: str, sha: str, hosts: Sequence[str]) -> str:
    return f"APPROVE-IMAGES {campaign_id} {sha[:12]} {','.join(sorted(hosts))}"


def _approval_problems(
    ledger: Ledger, *, sha: str, checkout: Checkout, expected: State, blocker: Blocker
) -> list[str]:
    """The checks every approval shares: no CI or test run, a REAL campaign at the right STOP,
    and the exact clean checked-out HEAD."""
    campaign = ledger.campaign()
    problems = []
    if (reason := blocker()) is not None:
        problems.append(f"approval is never issued under {reason}")
    if campaign.mode is not Mode.REAL:
        problems.append("only a REAL campaign takes approval")
    if campaign.state is not expected:
        problems.append(f"the campaign is {campaign.state}, not {expected}")
    if not SHA.fullmatch(sha) or checkout.head() != sha or checkout.dirty():
        problems.append("the approved SHA must be the clean, checked-out HEAD")
    return problems


def approve(
    root: Path,
    sha: str,
    *,
    confirm: Callable[[str], bool],
    checkout: Checkout,
    blocker: Blocker = ci_or_test,
) -> None:
    ledger = Ledger(ReconPaths(root).ledger)
    problems = _approval_problems(
        ledger, sha=sha, checkout=checkout, expected=State.AWAITING_APPROVAL, blocker=blocker
    )
    passed = ledger.last_event("PREFLIGHT_PASSED")
    if passed is None or passed["detail"].get("head") != sha:
        problems.append("the preflight did not pass at this SHA")
    if problems:
        raise Refused("; ".join(problems))
    if not confirm(approval_phrase(ledger.campaign().campaign_id, sha)):
        raise Refused("the operator did not type the approval phrase")
    ledger.record("APPROVED_A", sha=sha, nonce=secrets.token_hex(8))


def approve_images(
    root: Path,
    sha: str,
    hosts: Sequence[str],
    *,
    confirm: Callable[[str], bool],
    checkout: Checkout,
    blocker: Blocker = ci_or_test,
) -> None:
    """Approve image hosts with the same strength as phase A: the same clean SHA, and only hosts
    the ledger recorded as observed (never a findings file)."""
    ledger = Ledger(ReconPaths(root).ledger)
    problems = _approval_problems(
        ledger,
        sha=sha,
        checkout=checkout,
        expected=State.AWAITING_IMAGE_HOST_APPROVAL,
        blocker=blocker,
    )
    approved = ledger.last_event("APPROVED_A")
    if approved is None or approved["detail"].get("sha") != sha:
        problems.append("image hosts are approved at the same SHA as phase A")
    problems += ledger.observation_problems()
    chosen = sorted(set(hosts))
    if not chosen or not set(chosen) <= ledger.observed_hosts():
        problems.append("only image hosts the ledger recorded as observed in phase A")
    if problems:
        raise Refused("; ".join(problems))
    if not confirm(images_phrase(ledger.campaign().campaign_id, sha, chosen)):
        raise Refused("the operator did not type the image-host phrase")
    ledger.approve_hosts(chosen)
    ledger.record("APPROVED_B", sha=sha, hosts=chosen, nonce=secrets.token_hex(8))


def _consume(ledger: Ledger, kind: str, checkout: Checkout, blocker: Blocker) -> None:
    """A REAL run proceeds only right after its own approval: the latest event, fresh, and the
    checkout still the clean SHA that approval was issued at."""
    event = ledger.last_event(kind)
    problems = []
    if event is None or event["seq"] != ledger.latest_seq():
        problems.append("the approval is not the ledger's latest event")
    elif datetime.now(UTC) - datetime.fromisoformat(event["at"]) > APPROVAL_MAX_AGE:
        problems.append("the approval is older than two hours")
    if (reason := blocker()) is not None:
        problems.append(f"a REAL run never happens under {reason}")
    sha = event["detail"].get("sha") if event else None
    if sha is None or checkout.head() != sha or checkout.dirty():
        problems.append("the checkout is not the clean SHA this approval was issued at")
    if problems:
        raise Refused("; ".join(problems))


# ---------------------------------------------------------------- runs


@dataclass
class Session:
    """The authenticated session of this process, kept in memory only."""

    payload: bytes | None = None

    def cookies(self) -> list[dict[str, str]]:
        if self.payload is None:
            return []
        cookies, _ = decode_session(self.payload)
        return cookies


def _findings_secrets(
    container: Container, supplier_key: str, cookies: Sequence[Mapping[str, str]]
) -> FindingsSecrets:
    """What the findings are scanned against: the owner's login, always, and the session's cookie
    values, except a one-character flag cookie that would collide with ordinary structure."""
    stored = SupplierCredentialStore(container.secrets).load(supplier_key)
    login = [stored.username, stored.password] if stored else []
    return findings_secrets(login, cookies)


def _run(
    root: Path,
    phase: str,
    *,
    owner: ConnectionOwner,
    capture_secrets: Callable[[], SecretStore],
    collection: Callable[[], PolicedCollectionGateway],
    start: Callable[[], None],
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Run one phase with the connection owner's services. The owner is checked first, so a
    refusal records and sends nothing and leaves the approval to be used once it is fixed."""
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    campaign = ledger.campaign()
    definition = _definition(campaign.mode)
    key = definition.profile.supplier_key
    session = Session()
    passed = ledger.last_event("PREFLIGHT_PASSED")
    if passed is None or passed["detail"].get("owner") != owner_identity(owner):
        raise Refused(
            "the connection owner is not the one the preflight checked; run the preflight again"
        )
    with _owner_container(owner) as container:
        failed = [name for name, passed in _owner_state(container, key) if not passed]
        if failed:
            raise Refused(f"the ICBM connection owner cannot provide the session: {failed}")
        gateway = collection()
        start()

        def connect() -> bytes:
            session.payload = container.connect.collection_session(key, operator_initiated=True)
            return session.payload

        def secret_values() -> FindingsSecrets:
            # Read from the owner only to refuse findings that would carry them; never copied.
            return _findings_secrets(container, key, session.cookies())

        recon = Recon(
            ledger=ledger,
            gateway=gateway,
            supplier=definition.profile,
            captures=CaptureStore(
                paths.captures, capture_secrets(), campaign_id=campaign.campaign_id
            ),
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


def run_real(
    root: Path,
    phase: str,
    *,
    checkout: Checkout,
    blocker: Blocker = ci_or_test,
    owner: ConnectionOwner | None = None,
) -> dict[str, Any]:
    ledger = Ledger(ReconPaths(root).ledger)
    campaign = ledger.campaign()
    if campaign.mode is not Mode.REAL:
        raise Refused("a REAL run needs a REAL campaign")
    if (reason := blocker()) is not None:
        raise Refused(f"a REAL run never happens under {reason}")
    _consume(ledger, "APPROVED_A" if phase == "A" else "APPROVED_B", checkout, blocker)

    def start() -> None:
        ledger.record(
            f"RUN_{phase}_STARTED", state=State.RUNNING_A if phase == "A" else State.RUNNING_B
        )
        EGRESS.install()

    return _run(
        root,
        phase,
        owner=owner or ConnectionOwner.operator(),
        capture_secrets=lambda: capture_key_store(campaign.campaign_id),
        collection=PolicedCollectionGateway,
        start=start,
    )


# ---------------------------------------------------------------- offline phase-A finalization


def finalize(
    root: Path,
    *,
    owner: ConnectionOwner | None = None,
    capture_secrets: Callable[[], SecretStore] | None = None,
) -> dict[str, Any]:
    """Finish phase A locally, from the append-only ledger and the encrypted captures.

    It builds no collection gateway, installs no egress, opens no session and reserves nothing,
    so it cannot reach the supplier (Issue #52 comment 5689874555 §5). It runs only while every
    phase-A read is completed, no image was requested and the observation is not yet bound, and
    it refuses a second run because the campaign has left that state.
    """
    paths = ReconPaths(root)
    ledger = Ledger(paths.ledger)
    campaign = ledger.campaign()
    if problems := ledger.offline_finalization_problems():
        raise Refused("; ".join(problems))
    key = _definition(campaign.mode).profile.supplier_key
    if owner is None:
        if campaign.mode is not Mode.REAL:
            raise Refused("a DRY finalization names its stand-in connection owner")
        owner = ConnectionOwner.operator()
    passed = ledger.last_event("PREFLIGHT_PASSED")
    if passed is None or passed["detail"].get("owner") != owner_identity(owner):
        raise Refused("the connection owner is not the one the preflight checked")
    store = capture_secrets() if capture_secrets else capture_key_store(campaign.campaign_id)
    captures = CaptureStore(paths.captures, store, campaign_id=campaign.campaign_id)
    with _owner_container(owner) as container:
        # The provenance goes in before any local work, so every later failure, rebuilding the
        # evidence included, sits after it in the ledger (review 5217542767 §2).
        ledger.record(
            "OFFLINE_FINALIZATION_STARTED",
            source="ledger+captures",
            captures=sorted(captures.labels()),
        )
        try:
            return finalize_phase_a(
                ledger=ledger,
                findings_dir=paths.findings,
                evidence=lambda: evidence_from_captures(ledger, captures),
                secrets=lambda: _findings_secrets(
                    container, key, container.connect.session_cookies_for_scan(key)
                ),
            )
        except ReconStop as stop:
            raise Refused(f"the captures cannot support finalization ({stop.reason})") from None


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
        "observed_hosts": sorted(ledger.observed_hosts()),
        "approved_hosts": sorted(ledger.approved_hosts()),
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


def rehearse(
    root: Path,
    *,
    storefront: fake_site.FakeStorefront | None = None,
    owner_secrets: SecretStore | None = None,
    capture_secrets: SecretStore | None = None,
) -> dict[str, Any]:
    """The whole reconnaissance on the synthetic storefront: no supplier, no OS keyring. The
    stand-in connection owner is an ICBM data directory inside the campaign with an in-memory
    secret store, where the login is saved through ICBM's own operator action. DRY has no
    operator and no SHA, so it records the approvals itself. The ledger still enforces the
    phases, the caps and the observed-host authority."""
    ledger = init(
        root, campaign_id="m3-recon-dry", product_url=fake_site.PRODUCT_URL, mode=Mode.DRY
    )
    connect_gateway = fake_site.FakeConnectGateway()
    definition = fake_site.definition()
    owner = ConnectionOwner(
        config=_dry_owner_config(ReconPaths(root)),
        secrets=owner_secrets or MemorySecretStore(),
        gateway=connect_gateway,
        suppliers=(definition,),
    )
    captures = capture_secrets or MemorySecretStore()
    with _owner_container(owner) as container:
        container.connect.save_credentials(
            definition.profile.supplier_key,
            username=fake_site.USERNAME,
            password=fake_site.PASSWORD,
            actor=OPERATOR,
        )
    if not all(passed for _, passed in preflight(root, owner=owner)):
        raise Refused("the DRY preflight failed")
    ledger.record("APPROVED_A", sha="dry", nonce="dry")
    now = [1_000_000.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    site = storefront or fake_site.FakeStorefront()
    collection = PolicedCollectionGateway(http_transport=site.transport())

    def phase(name: str, state: State) -> dict[str, Any]:
        def start() -> None:
            ledger.record(f"RUN_{name}_STARTED", state=state)

        return _run(
            root,
            name,
            owner=owner,
            capture_secrets=lambda: captures,
            collection=lambda: collection,
            start=start,
            clock=clock,
            sleep=sleep,
        )

    phase_a = phase("A", State.RUNNING_A)
    phase_b: dict[str, Any] = {}
    observed = ledger.observed_hosts()
    if ledger.state() is State.AWAITING_IMAGE_HOST_APPROVAL and observed:
        ledger.approve_hosts(observed)
        ledger.record("APPROVED_B", sha="dry", hosts=sorted(observed), nonce="dry")
        phase_b = phase("B", State.RUNNING_B)
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
    """The operator types the phrase in an interactive terminal; a non-TTY stdin never
    approves (PR #60 follow-up 5686950430 §1)."""
    if not sys.stdin.isatty():
        return False
    print(f"Type exactly: {phrase}")
    return input("> ").strip() == phrase


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m3_recon", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "init",
        "preflight",
        "approve",
        "run",
        "finalize",
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
        if name in ("approve", "approve-images"):
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
            approve_images(root, args.sha, hosts, confirm=_typed, checkout=GitCheckout())
            print("image hosts approved: run phase B with `run-images --real` within two hours")
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
        elif args.command == "finalize":
            findings = finalize(root)
            print(
                json.dumps(
                    {
                        "observed_image_hosts": findings.get("observed_image_hosts", []),
                        "requests": findings.get("requests"),
                    }
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
