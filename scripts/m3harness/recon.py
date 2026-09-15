"""The M3 reconnaissance run (ADR-0010 §4, §5; architect rulings on Q1, Q2 and Q5).

Phase A starts only after the user's approval. It reads, in this order and only through the
collection gateway and the campaign ledger:
1. robots.txt, a public policy read. If its ``*`` group disallows the product path, the run
   stops before any product read;
2. the authenticated session, from the M1 connection owner: session reuse, at most one login;
3. the chosen product page, once, then once more no sooner than 60 s later, to see which
   structure is stable;
4. the terms page linked from the product page, when there is one (a public policy read).

Raw bodies go only into encrypted captures. The findings hold:
* the sanitized inventory;
* the robots and terms signals;
* the observed image hosts;
* the request counts.

The run then stops at AWAITING_IMAGE_HOST_APPROVAL.

Phase B needs a second approval that names the exact image hosts. It fetches a bounded sample of
the page's images on those hosts, plus one conditional re-request to test validator support. It
records only format signatures, sizes, content types and whether validators are present.
"""

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlsplit

from app.collect.urls import secret_looking
from app.core.errors import AppError
from integrations.suppliers.base import SupplierProfile
from integrations.suppliers.collection import (
    CollectionLimits,
    CollectionProfile,
    DocumentView,
    ImageResponse,
    ReadKind,
)
from integrations.suppliers.transport.collection import (
    CollectionBudgetRefused,
    ImageFetchRefused,
    PolicedCollectionGateway,
)
from scripts.m3harness.capture import CaptureStore
from scripts.m3harness.inventory import (
    assert_sanitized,
    document_inventory,
    image_urls,
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
    policy_paths: Iterable[str] = (ROBOTS_PATH,),
    image_hosts: Iterable[str] = (),
) -> CollectionProfile:
    """The collection profile of this reconnaissance only: exactly the chosen product path, its
    own query keys, the policy documents and — in phase B — the approved image hosts."""
    parts = urlsplit(product_url)
    keys = frozenset(unquote_plus(k) for k, _ in parse_qsl(parts.query, keep_blank_values=True))
    cap = CAPS[ReadKind.IMAGE_REQUEST]
    return CollectionProfile(
        supplier=supplier,
        product_path=re.escape(parts.path),
        policy_paths=frozenset(policy_paths),
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


@dataclass
class Recon:
    ledger: Ledger
    gateway: PolicedCollectionGateway
    supplier: SupplierProfile
    captures: CaptureStore
    findings_dir: Path
    # The M1 connection owner, operator-initiated: session reuse, at most one login.
    session: Callable[[], bytes]
    # Values that may never appear in findings: the login, the session's cookie values.
    secrets: Callable[[], list[str]]
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
        findings: dict[str, Any] = {"phase": "A", "product_path_form": mask(urlsplit(url).path)}
        try:
            robots = self._document(profile, origin + ROBOTS_PATH, ReadKind.POLICY_READ)
            groups = parse_robots(robots.body) if robots.status == 200 else {}
            findings["robots"] = {
                "http_status": robots.status,
                "star_rules": [[d, mask(p)] for d, p in groups.get("*", [])],
                "product_path_disallowed": robots_disallows(groups, urlsplit(url).path),
            }
            if findings["robots"]["product_path_disallowed"]:
                raise ReconStop("ROBOTS_DISALLOWS_PRODUCT_PATH")
            session = self.session()
            first = self._product(profile, url, session, "product-1")
            started = self.clock()
            inventory = document_inventory(first.body, url)
            terms = policy_links(first.body, url)["terms"][:1]
            self.sleep(max(0.0, SAME_PRODUCT_INTERVAL_S - (self.clock() - started)) + 1.0)
            second = self._product(profile, url, session, "product-2")
            again = document_inventory(second.body, url)
            findings["inventory"] = inventory
            findings["stability"] = {
                "body_identical": _digest(first.body) == _digest(second.body),
                "inventory_identical": inventory == again,
                "changed_sections": sorted(k for k in inventory if inventory[k] != again.get(k)),
            }
            findings["observed_image_hosts"] = sorted(inventory["images"]["hosts"])
            if terms:
                path = terms[0]
                view = self._document(
                    replace(profile, policy_paths=profile.policy_paths | {path}),
                    origin + path,
                    ReadKind.POLICY_READ,
                )
                self.captures.save("terms", view.body.encode("utf-8"))
                findings["terms"] = {
                    "http_status": view.status,
                    "path_form": mask(path),
                    **terms_signals(view.body),
                }
            else:
                findings["terms"] = {"http_status": None, "note": "no terms link on the page"}
        except ReconStop as stop:
            return self._stop(findings, "phase-a", stop.reason, State.STOPPED)
        except CollectionBudgetRefused:
            return self._stop(findings, "phase-a", "BUDGET_REFUSED", State.BUDGET_EXHAUSTED)
        except AppError as exc:
            return self._stop(findings, "phase-a", exc.code, State.STOPPED)
        findings["requests"] = self.ledger.counts()
        self._write("phase-a", findings)
        self.ledger.record("PHASE_A_DONE", state=State.AWAITING_IMAGE_HOST_APPROVAL)
        return findings

    # ---------------------------------------------------------------- phase B

    def phase_b(self) -> dict[str, Any]:
        if self.ledger.state() is not State.RUNNING_B:
            raise ReconStop("PHASE_B_NOT_RUNNING")
        url = self.ledger.campaign().product_url
        hosts = self.ledger.approved_hosts()
        body = self.captures.load("product-1").decode("utf-8")
        candidates = [u for u in image_urls(body, url) if urlsplit(u).hostname in hosts]
        profile = recon_profile(self.supplier, url, image_hosts=hosts)
        findings: dict[str, Any] = {"phase": "B", "approved_hosts": sorted(hosts), "images": []}
        try:
            revalidated = False
            for candidate in candidates[:IMAGE_SAMPLE]:
                entry: dict[str, Any] = {
                    "host": urlsplit(candidate).hostname,
                    "has_query": bool(urlsplit(candidate).query),
                }
                try:
                    image = self._image(profile, candidate)
                except ImageFetchRefused as refused:
                    entry["issue"] = refused.issue.value
                    findings["images"].append(entry)
                    continue
                entry.update(_image_signals(image))
                if not revalidated and (image.etag or image.last_modified):
                    again = self._image(profile, candidate, image.etag, image.last_modified)
                    entry["revalidation_status"] = again.status
                    revalidated = True
                findings["images"].append(entry)
        except CollectionBudgetRefused:
            return self._stop(findings, "phase-b", "BUDGET_REFUSED", State.BUDGET_EXHAUSTED)
        except AppError as exc:
            return self._stop(findings, "phase-b", exc.code, State.STOPPED)
        findings["requests"] = self.ledger.counts()
        self._write("phase-b", findings)
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

    def _stop(
        self, findings: dict[str, Any], name: str, reason: str, state: State
    ) -> dict[str, Any]:
        findings["stopped"] = reason
        findings["requests"] = self.ledger.counts()
        self._write(name, findings)
        self.ledger.record("STOPPED", state=state, reason=reason)
        return findings

    def _write(self, name: str, findings: dict[str, Any]) -> Path:
        assert_sanitized(findings, self.secrets())
        self.findings_dir.mkdir(parents=True, exist_ok=True)
        path = self.findings_dir / f"{name}.json"
        path.write_text(json.dumps(findings, ensure_ascii=False, indent=2) + "\n", "utf-8")
        return path


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
