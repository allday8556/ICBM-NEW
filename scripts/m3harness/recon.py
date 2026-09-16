"""The M3 reconnaissance run (ADR-0010 §4, §5; architect rulings on Q1, Q2 and Q5).

Phase A starts only after the user's approval. It reads, in this order and only through the
collection gateway and the campaign ledger:
1. robots.txt, a fixed public policy read. If its ``*`` group disallows the product path, the
   run stops before any product read;
2. the authenticated session, from the M1 connection owner: session reuse, at most one login;
3. the chosen product page, once, then once more no sooner than 60 s later, to see which
   structure is stable;
4. the terms page linked from the product page, when there is one. This is a *discovered*
   policy read: same storefront, no session, never followed further, and at most once. It never
   widens the profile's fixed policy paths.

Raw bodies go only into encrypted captures. The findings hold the sanitized inventory, the robots
and terms signals, the observed image hosts and the request counts.

Provider reads and local work are separate steps (Issue #52 comment 5689874555 §4). Once every
read is done and its capture is durable, the campaign moves to FINALIZING_A, where no reservation
is possible. Only then are the findings built, scanned and written, and the observation bound in
one transaction with the stop at AWAITING_IMAGE_HOST_APPROVAL. A local failure after the reads
leaves the campaign in that finalization state, from which the same work can be retried offline
and network collection can never resume.

The findings are a pure function of the evidence, so ``finalize_phase_a`` serves both the live run
and an offline finalization rebuilt from the encrypted captures and the append-only ledger.

Phase B needs a second approval that names the exact image hosts, bound to the same clean SHA.
It fetches a bounded sample of the page's images on those hosts, plus one conditional re-request
to test validator support. It records only format signatures, sizes, content types and whether
validators are present.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from app.collect.urls import secret_looking
from app.core.errors import AppError
from integrations.suppliers.base import SupplierProfile
from integrations.suppliers.collection import (
    DISCOVERED_POLICY_PREFIX,
    CollectionLimits,
    CollectionProfile,
    DocumentView,
    ImageCandidate,
    ImageResponse,
    ImageRoleRules,
    ReadKind,
    plan_image_sample,
)
from integrations.suppliers.transport.collection import (
    CollectionBudgetRefused,
    ImageFetchRefused,
    PolicedCollectionGateway,
)
from scripts.m3harness.capture import CaptureStore
from scripts.m3harness.inventory import (
    FindingsSecrets,
    assert_sanitized,
    document_inventory,
    mask,
    parse_robots,
    policy_links,
    robots_disallows,
    terms_signals,
)
from scripts.m3harness.ledger import CAPS, SAME_PRODUCT_INTERVAL_S, Ledger, LedgerBudget, State

# Reconnaissance only (not the frozen M3 acceptance limits): one image may be at most 10 MiB.
RECON_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_SAMPLE = 6
ROBOTS_PATH = "/robots.txt"
# A robots document that is simply not published: the host answered, and said there is no rule
# file. Anything else — an authentication or rate-limit answer, a server failure, a network
# failure — leaves the host's rules unknown, and unknown is a stop (ruling 5699776908 §3).
ROBOTS_ABSENT = (404, 410)
PRODUCT_CAPTURES = ("product-1", "product-2")
# A harness before Issue #52 comment 5689874555 did not keep the robots body.
ROBOTS_NOT_PUBLISHED = (
    "the host answered that it publishes no robots document, so it states no exclusion rule; that "
    "is not permission to publish, reuse or licence anything it serves"
)
ROBOTS_NOT_RETAINED = (
    "the robots body was not retained by the harness that made this read; the ledger records the "
    "policy read and its status, and phase A reads a product only after a false disallow check"
)
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


class ReconStop(RuntimeError):
    """The run stopped on a finding; the reason is a code, never page content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def product_url_problems(url: str, supplier: SupplierProfile) -> list[str]:
    """Why ``url`` is not an acceptable reconnaissance product URL (names no value)."""
    parts = urlsplit(url)
    problems = []
    if parts.scheme != "https" or parts.hostname != urlsplit(supplier.base_url).hostname:
        problems.append("the product URL must be https on the supplier's storefront host")
    if parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
        problems.append("the product URL carries no credentials, port or fragment")
    if any(secret_looking(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        problems.append("the product URL carries a secret-looking query key")
    return problems


def recon_profile(
    supplier: SupplierProfile,
    product_url: str,
    *,
    image_hosts: Iterable[str] = (),
) -> CollectionProfile:
    """The collection profile of this reconnaissance only: exactly the chosen product path, its
    own query keys, robots.txt as the one fixed policy document and — in phase B — the approved
    image hosts. It is never widened during a run."""
    parts = urlsplit(product_url)
    keys = frozenset(unquote_plus(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True))
    cap = CAPS[ReadKind.IMAGE_REQUEST]
    return CollectionProfile(
        supplier=supplier,
        product_path=re.escape(parts.path),
        policy_paths=frozenset({ROBOTS_PATH}),
        image_hosts=frozenset(image_hosts),
        safe_query_keys={parts.hostname or "": keys} if keys else {},
        limits=CollectionLimits(
            max_image_refs=cap,
            max_image_bytes=RECON_IMAGE_BYTES,
            max_image_requests_per_run=cap,
            max_new_image_bytes_per_run=cap * RECON_IMAGE_BYTES,
            same_product_interval_s=SAME_PRODUCT_INTERVAL_S,
        ),
    )


# ---------------------------------------------------------------- phase A evidence and findings


@dataclass
class PhaseAEvidence:
    """What phase A's reads produced: the bodies and statuses, in memory during a run or reread
    from the encrypted captures afterwards. The findings are a pure function of it."""

    product_url: str
    robots_status: int | None = None
    robots_body: str | None = None
    robots_note: str | None = None
    product_bodies: tuple[str, ...] = ()
    terms_status: int | None = None
    terms_path: str | None = None
    terms_body: str | None = None
    terms_note: str | None = None


def phase_a_findings(evidence: PhaseAEvidence, requests: dict[str, int]) -> dict[str, Any]:
    """The sanitized findings of phase A: structure, signals, hosts and counts only."""
    url = evidence.product_url
    findings: dict[str, Any] = {"phase": "A", "product_path_form": mask(urlsplit(url).path)}
    groups = parse_robots(evidence.robots_body) if evidence.robots_body is not None else {}
    findings["robots"] = {
        "http_status": evidence.robots_status,
        # Never invented: without the body the rules are unknown, and the note says why.
        "star_rules": (
            [[directive, mask(rule)] for directive, rule in groups.get("*", [])]
            if evidence.robots_body is not None
            else None
        ),
        "product_path_disallowed": robots_disallows(groups, urlsplit(url).path),
    }
    if evidence.robots_note:
        findings["robots"]["note"] = evidence.robots_note
    if evidence.product_bodies:
        first, *rest = evidence.product_bodies
        inventory = document_inventory(first, url)
        findings["inventory"] = inventory
        findings["observed_image_hosts"] = sorted(inventory["images"]["hosts"])
        if rest:
            again = document_inventory(rest[0], url)
            findings["stability"] = {
                "body_identical": _digest(first) == _digest(rest[0]),
                "inventory_identical": inventory == again,
                "changed_sections": sorted(k for k in inventory if inventory[k] != again.get(k)),
            }
    if evidence.terms_body is not None:
        findings["terms"] = {
            "http_status": evidence.terms_status,
            "path_form": mask(evidence.terms_path or ""),
            **terms_signals(evidence.terms_body),
        }
    else:
        findings["terms"] = {
            "http_status": evidence.terms_status,
            "note": evidence.terms_note or "no terms link on the page",
        }
    findings["requests"] = requests
    return findings


def evidence_from_captures(ledger: Ledger, captures: CaptureStore) -> PhaseAEvidence:
    """Rebuild phase A's evidence from the append-only ledger and the encrypted captures only.

    It reads no page and asks the supplier for nothing: what the captures do not hold is recorded
    as unavailable rather than guessed (Issue #52 comment 5689810516 §5).
    """
    labels = set(captures.labels())
    missing = [label for label in PRODUCT_CAPTURES if label not in labels]
    if missing:
        raise ReconStop(f"CAPTURES_MISSING_{'_'.join(missing).upper().replace('-', '_')}")
    evidence = PhaseAEvidence(
        product_url=ledger.campaign().product_url,
        product_bodies=tuple(
            captures.load(label).decode("utf-8") for label in PRODUCT_CAPTURES if label in labels
        ),
    )
    reservations = ledger.reservations()
    robots = next((r for r in reservations if r["subject"] == ROBOTS_PATH), None)
    evidence.robots_status = robots["http_status"] if robots else None
    if "robots" in labels:
        evidence.robots_body = captures.load("robots").decode("utf-8")
    else:
        evidence.robots_note = ROBOTS_NOT_RETAINED
    discovered = next(
        (r for r in reservations if r["subject"].startswith(DISCOVERED_POLICY_PREFIX)), None
    )
    if discovered is None:
        evidence.terms_note = "no discovered policy read was reserved"
    elif "terms" not in labels:
        raise ReconStop("CAPTURES_MISSING_TERMS")
    else:
        evidence.terms_status = discovered["http_status"]
        evidence.terms_path = discovered["subject"][len(DISCOVERED_POLICY_PREFIX) :]
        evidence.terms_body = captures.load("terms").decode("utf-8")
    return evidence


def write_findings(
    findings_dir: Path, name: str, findings: dict[str, Any], secrets: FindingsSecrets
) -> Path:
    """Scan the findings against the secret set, record which cookies it left out (names only)
    and write them. Nothing is written when the scan refuses."""
    findings["secret_scan"] = secrets.audit()
    assert_sanitized(findings, secrets.values)
    findings_dir.mkdir(parents=True, exist_ok=True)
    path = findings_dir / f"{name}.json"
    path.write_text(json.dumps(findings, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return path


def finalize_phase_a(
    *,
    ledger: Ledger,
    findings_dir: Path,
    evidence: Callable[[], PhaseAEvidence],
    secrets: Callable[[], FindingsSecrets],
) -> dict[str, Any]:
    """Local work only: take or rebuild the evidence, build the findings, write them, and bind the
    observation and the digests.

    It makes no request and reserves nothing. Every step of that local work is covered: rebuilding
    the evidence, building the findings, the secret scan, the write and the binding. Any failure
    appends ``LOCAL_FINALIZATION_FAILED`` with the failure's class name, never a value, and leaves
    the campaign where it is, so the same work can be retried offline while phase A's network
    collection stays closed (PR #64 review 5217542767 §2).
    """
    try:
        findings = phase_a_findings(evidence(), ledger.counts())
        written = write_findings(findings_dir, "phase-a", findings, secrets())
        ledger.finish_phase_a(
            findings.get("observed_image_hosts", []),
            findings_digest=hashlib.sha256(written.read_bytes()).hexdigest(),
        )
    except Exception as exc:
        ledger.record("LOCAL_FINALIZATION_FAILED", reason=type(exc).__name__)
        raise
    return findings


@dataclass
class Recon:
    ledger: Ledger
    gateway: PolicedCollectionGateway
    supplier: SupplierProfile
    captures: CaptureStore
    findings_dir: Path
    # The M1 connection owner, operator-initiated: session reuse, at most one login.
    session: Callable[[], bytes]
    # What the findings are scanned against: the login and the session's own cookie values.
    secrets: Callable[[], FindingsSecrets]
    # The supplier's own image-role knowledge. This runner never reads a selector or a path word.
    image_roles: ImageRoleRules
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    budget: LedgerBudget = field(init=False)

    def __post_init__(self) -> None:
        self.budget = LedgerBudget(self.ledger, clock=self.clock)

    # ---------------------------------------------------------------- phase A

    def phase_a(self) -> dict[str, Any]:
        if self.ledger.state() is not State.RUNNING_A:
            raise ReconStop("PHASE_A_NOT_RUNNING")
        url = self.ledger.campaign().product_url
        origin = f"https://{urlsplit(url).hostname}"
        profile = recon_profile(self.supplier, url)
        evidence = PhaseAEvidence(product_url=url)
        try:
            robots = self._document(profile, origin + ROBOTS_PATH, ReadKind.POLICY_READ)
            self.captures.save("robots", robots.body.encode("utf-8"))
            evidence.robots_status = robots.status
            evidence.robots_body = robots.body if robots.status == 200 else None
            if robots_disallows(parse_robots(robots.body or ""), urlsplit(url).path):
                raise ReconStop("ROBOTS_DISALLOWS_PRODUCT_PATH")
            session = self.session()
            first = self._product(profile, url, session, "product-1")
            started = self.clock()
            terms = policy_links(first.body, url)["terms"][:1]
            self.sleep(max(0.0, SAME_PRODUCT_INTERVAL_S - (self.clock() - started)) + 1.0)
            second = self._product(profile, url, session, "product-2")
            evidence.product_bodies = (first.body, second.body)
            if terms:
                view = self._discovered(profile, origin + terms[0])
                self.captures.save("terms", view.body.encode("utf-8"))
                evidence.terms_status, evidence.terms_path = view.status, terms[0]
                evidence.terms_body = view.body
        except ReconStop as stop:
            return self._stop(evidence, stop.reason, State.STOPPED)
        except CollectionBudgetRefused:
            return self._stop(evidence, "BUDGET_REFUSED", State.BUDGET_EXHAUSTED)
        except AppError as exc:
            return self._stop(evidence, exc.code, State.STOPPED)
        # Every read is done and every capture is durable: nothing further may be reserved.
        self.ledger.record("PHASE_A_READS_DONE", state=State.FINALIZING_A, requests=self.counts())

        def collected() -> PhaseAEvidence:
            return evidence

        return finalize_phase_a(
            ledger=self.ledger,
            findings_dir=self.findings_dir,
            evidence=collected,
            secrets=self.secrets,
        )

    def counts(self) -> dict[str, int]:
        return self.ledger.counts()

    # ---------------------------------------------------------------- phase B

    def _image_host_robots(
        self, profile: CollectionProfile, host: str, paths: Sequence[str]
    ) -> dict[str, Any]:
        """Read one image host's own robots rules and say whether the chosen paths may be read.

        The storefront's rules were already read and recorded in phase A, so its host is never
        asked again: phase A's evidence is reused and no request is made for it. Any other host is
        a distinct origin, and exactly one ``/robots.txt`` is read from it.

        A stop is the default. Only a published rule file that allows the chosen paths, or a plain
        answer that no rule file exists, lets phase B continue; an answer that leaves the rules
        unknown does not. Nothing here is a publication, reuse or licence grant.
        """
        if host == profile.storefront_host:
            return {"host": host, "source": "phase-a", "requested": False, "allowed": True}
        view = self._document(profile, f"https://{host}{ROBOTS_PATH}", ReadKind.POLICY_READ)
        record: dict[str, Any] = {
            "host": host,
            "source": "image-host-robots",
            "requested": True,
            "http_status": view.status,
        }
        if view.status in ROBOTS_ABSENT:
            return record | {"star_rules": None, "allowed": True, "note": ROBOTS_NOT_PUBLISHED}
        if view.status != 200:
            return record | {"allowed": False, "reason": f"ROBOTS_HTTP_{view.status}"}
        groups = parse_robots(view.body)
        disallowed = sorted({path for path in paths if robots_disallows(groups, path)})
        record |= {"star_rules": len(groups.get("*", [])), "allowed": not disallowed}
        if disallowed:
            record["reason"] = "ROBOTS_DISALLOWS_IMAGE_PATH"
            record["disallowed_paths"] = [mask(path) for path in disallowed]
        return record

    def _robots_cleared(
        self, profile: CollectionProfile, selected: Sequence[ImageCandidate]
    ) -> list[dict[str, Any]]:
        """Every host the sample would request, cleared before the first image leaves.

        A host is checked once, in the order the sample would reach it, and the first refusal ends
        the walk: a host whose rules refused is never asked for an image, and a host after it is
        never even asked for its rules.
        """
        checks: list[dict[str, Any]] = []
        for host in dict.fromkeys(candidate.host for candidate in selected):
            paths = [urlsplit(c.url).path for c in selected if c.host == host]
            try:
                check = self._image_host_robots(profile, host, paths)
            except AppError as exc:
                # The transport refused the answer outright — a rate limit, a server failure, a
                # network failure. The refusal is recorded before it is raised, so the ledger says
                # why phase B stopped rather than only that it did.
                self.ledger.record(
                    "IMAGE_HOST_ROBOTS",
                    host=host,
                    requested=True,
                    allowed=False,
                    reason=f"ROBOTS_{exc.code}",
                )
                raise
            self.ledger.record("IMAGE_HOST_ROBOTS", **check)
            checks.append(check)
            if not check["allowed"]:
                break
        return checks

    def phase_b(self) -> dict[str, Any]:
        if self.ledger.state() is not State.RUNNING_B:
            raise ReconStop("PHASE_B_NOT_RUNNING")
        url = self.ledger.campaign().product_url
        hosts = self.ledger.approved_hosts()
        body = self.captures.load("product-1").decode("utf-8")
        # The supplier's site knowledge says what each reference is for; this runner is handed
        # roles and never reads a selector, a host name or a path word (comment 5696242775 §1).
        classified = self.image_roles.classify(body, url)
        plan = plan_image_sample(
            [candidate for candidate in classified if candidate.host in hosts],
            IMAGE_SAMPLE,
            rules=self.image_roles.identity,
        )
        audit = plan.audit()
        audit["off_approved_hosts"] = _by_role_and_host(
            candidate for candidate in classified if candidate.host not in hosts
        )
        profile = recon_profile(self.supplier, url, image_hosts=hosts)
        findings: dict[str, Any] = {
            "phase": "B",
            "approved_hosts": sorted(hosts),
            "sample_plan": audit,
            "images": [],
        }
        try:
            # Every host's own rules first: not one image is requested until they allow it.
            findings["robots"] = self._robots_cleared(profile, plan.selected)
            blocked = next((c for c in findings["robots"] if not c["allowed"]), None)
            if blocked is not None:
                raise ReconStop(str(blocked.get("reason", "ROBOTS_REFUSED")))
            revalidated = False
            for candidate in plan.selected:
                entry: dict[str, Any] = {
                    "role": candidate.role.value,
                    "host": candidate.host,
                    "order": candidate.order,
                    "identity": candidate.identity,
                    "rule": candidate.rule,
                    "has_query": bool(urlsplit(candidate.url).query),
                }
                try:
                    image = self._image(profile, candidate.url)
                except ImageFetchRefused as refused:
                    entry["issue"] = refused.issue.value
                    findings["images"].append(entry)
                    continue
                entry.update(_image_signals(image))
                if not revalidated and (image.etag or image.last_modified):
                    again = self._image(profile, candidate.url, image.etag, image.last_modified)
                    entry["revalidation_status"] = again.status
                    revalidated = True
                findings["images"].append(entry)
        except ReconStop as stop:
            return self._stop_phase_b(findings, stop.reason, State.STOPPED)
        except CollectionBudgetRefused:
            return self._stop_phase_b(findings, "BUDGET_REFUSED", State.BUDGET_EXHAUSTED)
        except AppError as exc:
            return self._stop_phase_b(findings, exc.code, State.STOPPED)
        findings["requests"] = self.counts()
        write_findings(self.findings_dir, "phase-b", findings, self.secrets())
        self.ledger.record("PHASE_B_DONE", state=State.COMPLETED)
        return findings

    # ---------------------------------------------------------------- requests

    def _document(self, profile: CollectionProfile, url: str, kind: ReadKind) -> DocumentView:
        try:
            view = self.gateway.read_document(
                profile, url, kind=kind, budget=self.budget, session=None
            )
        except BaseException as exc:
            self._complete(None, getattr(exc, "code", type(exc).__name__))
            raise
        self._complete(view.status, "OK")
        return view

    def _discovered(self, profile: CollectionProfile, url: str) -> DocumentView:
        try:
            view = self.gateway.read_discovered_policy(profile, url, budget=self.budget)
        except BaseException as exc:
            self._complete(None, getattr(exc, "code", type(exc).__name__))
            raise
        self._complete(view.status, "OK")
        return view

    def _product(
        self, profile: CollectionProfile, url: str, session: bytes, label: str
    ) -> DocumentView:
        try:
            view = self.gateway.read_document(
                profile, url, kind=ReadKind.PRODUCT_READ, budget=self.budget, session=session
            )
        except BaseException as exc:
            self._complete(None, getattr(exc, "code", type(exc).__name__))
            raise
        self._complete(view.status, "OK")
        if view.status != 200:
            login = view.location is not None and "login" in view.location.lower()
            raise ReconStop(
                "PRODUCT_REDIRECTS_TO_LOGIN" if login else f"PRODUCT_HTTP_{view.status}"
            )
        self.captures.save(label, view.body.encode("utf-8"))
        return view

    def _image(
        self,
        profile: CollectionProfile,
        url: str,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> ImageResponse:
        try:
            image = self.gateway.read_image(
                profile, url, budget=self.budget, etag=etag, last_modified=last_modified
            )
        except BaseException as exc:
            self._complete(None, getattr(exc, "code", type(exc).__name__))
            raise
        self._complete(image.status, "OK")
        return image

    def _complete(self, status: int | None, outcome: str) -> None:
        if self.budget.last is not None:
            self.ledger.complete(self.budget.last, http_status=status, outcome=outcome)
            self.budget.last = None

    # ---------------------------------------------------------------- findings

    def _stop(self, evidence: PhaseAEvidence, reason: str, state: State) -> dict[str, Any]:
        self.ledger.record("STOPPED", state=state, reason=reason)

        def built() -> dict[str, Any]:
            findings = phase_a_findings(evidence, self.counts())
            findings["stopped"] = reason
            return findings

        return self._stop_findings("phase-a", built)

    def _stop_phase_b(self, findings: dict[str, Any], reason: str, state: State) -> dict[str, Any]:
        self.ledger.record("STOPPED", state=state, reason=reason)

        def built() -> dict[str, Any]:
            findings["stopped"] = reason
            findings["requests"] = self.counts()
            return findings

        return self._stop_findings("phase-b", built)

    def _stop_findings(self, name: str, built: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Write the findings of a stop the ledger has already made terminal.

        The provider side has stopped, so the terminal state is recorded first and the local work
        follows it. Building the findings, the secret scan and the write can each fail; none of
        that may hold the campaign open in a state that could collect again, and none of it may
        mask why the provider stopped. A failure appends its class name only — never a value — and
        the recorded stop and its reason stand (PR #64 review 5217847727 §1).
        """
        try:
            findings = built()
            write_findings(self.findings_dir, name, findings, self.secrets())
        except Exception as exc:
            self.ledger.record("LOCAL_FINALIZATION_FAILED", reason=type(exc).__name__)
            raise
        return findings


def _by_role_and_host(candidates: Iterable[ImageCandidate]) -> list[dict[str, Any]]:
    """How many references of each role each host contributed; no URL and no value."""
    counted: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        key = (candidate.role.value, candidate.host)
        counted[key] = counted.get(key, 0) + 1
    return [
        {"role": role, "host": host, "count": count}
        for (role, host), count in sorted(counted.items())
    ]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _image_signals(image: ImageResponse) -> dict[str, Any]:
    content = image.content
    signature = next((name for magic, name in _SIGNATURES if content.startswith(magic)), None)
    if signature is None and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        signature = "webp"
    if signature is None and content[4:12] in (b"ftypavif", b"ftypavis"):
        signature = "avif"
    return {
        "status": image.status,
        "content_type": image.content_type,
        "bytes": len(content),
        "signature": signature or "unknown",
        "etag": image.etag is not None,
        "last_modified": image.last_modified is not None,
    }
