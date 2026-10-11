"""「+ 공급처 추가」: the onboarding add flow (ADR-0036 §4 steps 1–5, slice O2).

1. **Detect** (``detect``): the operator's origin passes the host guard (OB-01) and the repository
   and duplicate checks *before* any read; then one home read and one read of each template's
   login page decide the platform, and robots.txt and that template's terms page are read.
   *Unsupported* stops there with "a template is needed" (§4 step 2). Only when both templates'
   markers matched (``AMBIGUOUS``) does the operator choose, and only between those, so a
   template is never forced onto a site that does not carry its markers. Nothing is written: the
   pass is held in this process for a short time under a ``detection_id``, so 「저장」 confirms
   exactly what the operator was shown.
2. **Save** (``create``): the operator confirms the platform and acknowledges the policy reads; the
   store writes the ``DRAFT`` identity, its bootstrap storefront host and the acknowledged policy
   reads in one unit. The login is then saved through CONNECT's own credential route for the new
   key, into the OS secret store (ADR-0007, OB-05): this module never sees it.
3. **Connect** is CONNECT's own connection test, on the template's login and probe paths.
4. **RECON** (``start_recon``): ``DRAFT → RECON`` only when the platform is confirmed, the policy
   reads are acknowledged, a credential is stored and CONNECT's protected-read proof holds (READY).
   An onboarded ``RECON`` site is still not collectable (OB-10).

Policy content never blocks: a robots rule against the product path is a warning the operator
reads and acknowledges (the seller carries that risk and decides).
"""

import threading
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

from app.platform.core.clock import Clock
from app.platform.core.errors import InputValidationError, NotFoundError
from app.stages.collect.onboarding.catalog import SupplierCatalog
from app.stages.collect.onboarding.contracts import (
    ConnectionView,
    DetectionView,
    OnboardedHostView,
    OnboardingView,
    PolicyDocumentView,
    ReconReadiness,
    RobotsView,
    StoredPolicyReadView,
    TemplateEvidenceView,
)
from app.stages.collect.onboarding.detect import (
    UNSUPPORTED,
    Detection,
    PolicyDocument,
    detect,
    detections,
    read_policies,
)
from app.stages.collect.onboarding.hosts import onboarding_host, onboarding_origin
from app.stages.collect.onboarding.site import OnboardingRecord, OnboardingState
from app.stages.collect.onboarding.store import (
    ONBOARDING_HOST_TAKEN,
    ONBOARDING_TEMPLATE_UNKNOWN,
    ONBOARDING_TRANSITION_REFUSED,
    OnboardingConflictError,
    OnboardingStore,
    PolicyReadInput,
)
from app.stages.connect.contracts import SupplierConnectionSummary
from app.stages.connect.state import ConnectionState
from integrations.suppliers.base import PublicReader
from integrations.suppliers.platforms import PlatformTemplate
from integrations.suppliers.sites import TEMPLATES

ONBOARDING_DETECTION_UNKNOWN: Final = "ONBOARDING_DETECTION_UNKNOWN"
ONBOARDING_DETECTION_MISMATCH: Final = "ONBOARDING_DETECTION_MISMATCH"
ONBOARDING_TEMPLATE_NEEDED: Final = "ONBOARDING_TEMPLATE_NEEDED"
ONBOARDING_TEMPLATE_NOT_DETECTED: Final = "ONBOARDING_TEMPLATE_NOT_DETECTED"
ONBOARDING_POLICY_NOT_ACKNOWLEDGED: Final = "ONBOARDING_POLICY_NOT_ACKNOWLEDGED"
ONBOARDING_POLICY_UNREAD: Final = "ONBOARDING_POLICY_UNREAD"
ONBOARDING_RECON_NOT_READY: Final = "ONBOARDING_RECON_NOT_READY"

# A detection pass is held this long for 「저장」, and at most this many at once.
DETECTION_TTL: Final = timedelta(minutes=30)
DETECTION_LIMIT: Final = 32


class ConnectionSource(Protocol):
    """CONNECT's read of one supplier's connection (``ConnectService``)."""

    def supplier_connection(self, supplier_key: str) -> SupplierConnectionSummary: ...


@dataclass(frozen=True)
class _Pending:
    view: DetectionView
    detection: Detection
    policy: tuple[PolicyDocument, ...]
    read_at: datetime


class OnboardingService:
    def __init__(
        self,
        *,
        store: OnboardingStore,
        catalog: SupplierCatalog,
        connect: ConnectionSource,
        reader: PublicReader,
        clock: Clock,
        templates: Mapping[str, PlatformTemplate] = TEMPLATES,
    ) -> None:
        self._store = store
        self._catalog = catalog
        self._connect = connect
        self._reader = reader
        self._clock = clock
        self._templates = templates
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, _Pending] = OrderedDict()

    @property
    def supported(self) -> list[str]:
        return sorted(detections(self._templates))

    # ------------------------------------------------------------------ step 1–3: detect

    def detect(self, origin: str, *, template: str | None = None) -> DetectionView:
        """Guard, then detect and read the policy documents. Writes nothing (§4 steps 1–3)."""
        normalised, host = onboarding_origin(origin)
        self._require_free(normalised, host)
        chosen = None if template is None else self._supported(template)
        # The operator's choice after a detection of the same origin reuses that detection:
        # only the chosen template's policy documents are read again.
        detection = self._recent(normalised) if chosen is not None else None
        reads = 0 if detection is not None else None
        if detection is None:
            detection = detect(self._reader, host, self._templates)
        if chosen is not None and chosen not in detection.choices:
            # §4 step 2: a choice exists only between the templates an AMBIGUOUS detection
            # matched. Anything else stops at "a template is needed"; nothing more is read.
            raise InputValidationError(
                ONBOARDING_TEMPLATE_NOT_DETECTED,
                "only a template whose markers this storefront carries can be confirmed",
                details={"choices": list(detection.choices)},
            )
        policy_template = chosen or (detection.platform if detection.supported else None)
        policy: tuple[PolicyDocument, ...] = ()
        if policy_template is not None:
            rules = self._templates[policy_template].detection
            assert rules is not None  # _supported and detect only name templates with rules
            policy = read_policies(self._reader, host, rules)
        now = self._clock.now()
        view = DetectionView(
            detection_id=uuid.uuid4().hex,
            origin=normalised,
            host=host,
            detected=detection.platform,
            reason=None if detection.reason is None else detection.reason.value,
            home_status=detection.home_status,
            suggested_origin=_acceptable(detection.home_redirect, host),
            chosen=chosen,
            policy_template=policy_template,
            evidence=[
                TemplateEvidenceView(
                    platform=e.platform,
                    home_markers=list(e.home_markers),
                    login_path=e.login_path,
                    login_status=e.login_status,
                    login_markers=list(e.login_markers),
                    matched=e.matched,
                )
                for e in detection.evidence
            ],
            policy=[_policy_view(normalised, document) for document in policy],
            templates=self.supported,
            choices=list(detection.choices),
            reads=detection.reads if reads is None else reads,
            policy_reads=len(policy),
            expires_at=now + DETECTION_TTL,
        )
        with self._lock:
            self._expire(now)
            self._pending[view.detection_id] = _Pending(view, detection, policy, now)
            while len(self._pending) > DETECTION_LIMIT:
                self._pending.popitem(last=False)
        return view

    # ------------------------------------------------------------------ step 1–3: save

    def create(
        self,
        *,
        name: str,
        origin: str,
        template: str,
        detection_id: str,
        acknowledged: bool,
        actor: str,
    ) -> OnboardingView:
        """The ``DRAFT`` with the confirmed platform and the acknowledged policy reads."""
        if acknowledged is not True:
            raise InputValidationError(
                ONBOARDING_POLICY_NOT_ACKNOWLEDGED,
                "the robots.txt and terms reads are acknowledged before the supplier is saved",
            )
        normalised, _ = onboarding_origin(origin)
        with self._lock:
            self._expire(self._clock.now())
            pending = self._pending.get(detection_id)
        if pending is None:
            raise NotFoundError(
                ONBOARDING_DETECTION_UNKNOWN, "confirm the platform again: the check has expired"
            )
        if pending.view.origin != normalised:
            raise InputValidationError(
                ONBOARDING_DETECTION_MISMATCH, "the platform check was for another origin"
            )
        confirmed = self._supported(template)
        choices = pending.detection.choices
        if not choices:
            raise InputValidationError(
                ONBOARDING_TEMPLATE_NEEDED,
                "no template recognised this storefront: a template is needed",
            )
        if confirmed not in choices:
            raise InputValidationError(
                ONBOARDING_TEMPLATE_NOT_DETECTED,
                "only a template whose markers this storefront carries can be confirmed",
                details={"choices": list(choices)},
            )
        if confirmed != pending.view.policy_template:
            raise InputValidationError(
                ONBOARDING_DETECTION_MISMATCH,
                "the confirmed platform is not the one whose policy documents were shown",
                details={"policy_template": pending.view.policy_template},
            )
        reads = [
            PolicyReadInput(
                document=document.document,
                path=document.path,
                http_status=document.status,
                content_sha256=document.content_sha256,
                read_at=pending.read_at,
            )
            for document in pending.policy
            if document.status is not None
        ]
        if not reads:
            raise InputValidationError(
                ONBOARDING_POLICY_UNREAD,
                "neither robots.txt nor the terms page answered; check the platform again later",
            )
        record = self._store.create(
            name=name,
            origin=normalised,
            template=confirmed,
            actor=actor,
            policy_reads=reads,
            detected=pending.detection.platform,
            markers=pending.detection.markers_of(confirmed),
        )
        with self._lock:
            self._pending.pop(detection_id, None)
        return self.view(record.supplier_key)

    # ------------------------------------------------------------------ step 5: RECON

    def readiness(self, record: OnboardingRecord) -> ReconReadiness:
        connection = self._connection(record.supplier_key)
        ready = connection is not None and connection.state is ConnectionState.READY
        return ReconReadiness(
            platform_confirmed=record.template in self._templates,
            policy_acknowledged=bool(self._store.policy_reads(record.supplier_key)),
            credentials_stored=connection is not None and connection.credentials_stored,
            connection_verified=ready,
        )

    def start_recon(self, key: str, *, actor: str) -> OnboardingView:
        """``DRAFT → RECON`` once every condition of §4 steps 2–4 holds."""
        record = self._store.current(key)
        if record.state is not OnboardingState.DRAFT:
            raise OnboardingConflictError(
                ONBOARDING_TRANSITION_REFUSED,
                "only a draft supplier starts site analysis",
                details={"from": record.state.value, "to": OnboardingState.RECON.value},
            )
        readiness = self.readiness(record)
        if not readiness.ready:
            raise OnboardingConflictError(
                ONBOARDING_RECON_NOT_READY,
                "site analysis starts after the platform, the policy reads, the login and a"
                " successful connection test",
                details=readiness.model_dump(),
            )
        evidence = [read.read_id for read in self._store.policy_reads(key)]
        self._store.transition(
            key, OnboardingState.RECON, actor=actor, reason="connection verified", evidence=evidence
        )
        return self.view(key)

    def disable(self, key: str, *, actor: str) -> OnboardingView:
        self._store.transition(key, OnboardingState.DISABLED, actor=actor)
        return self.view(key)

    # ------------------------------------------------------------------ reads

    def view(self, key: str) -> OnboardingView:
        return self._view(self._store.current(key))

    def views(self) -> list[OnboardingView]:
        return [self._view(record) for record in self._store.all_current()]

    # ------------------------------------------------------------------ internals

    def _view(self, record: OnboardingRecord) -> OnboardingView:
        connection = self._connection(record.supplier_key)
        entry = self._catalog.onboarded(record.supplier_key)
        return OnboardingView(
            supplier_key=record.supplier_key,
            display_name=record.display_name,
            origin=record.origin,
            template=record.template,
            state=record.state.value,
            state_seq=record.state_seq,
            created_at=record.created_at,
            hosts=[OnboardedHostView(host=h.host, role=h.role.value) for h in record.hosts],
            policy_reads=[
                StoredPolicyReadView(
                    read_id=r.read_id,
                    document=r.document,
                    path=r.path,
                    http_status=r.http_status,
                    content_sha256=r.content_sha256,
                    read_at=r.read_at,
                    acknowledged_by=r.acknowledged_by,
                    acknowledged_at=r.acknowledged_at,
                )
                for r in self._store.policy_reads(record.supplier_key)
            ],
            connection=None
            if connection is None
            else ConnectionView(
                state=connection.state.value,
                capability_status=connection.capability_status.value,
                credentials_stored=connection.credentials_stored,
                last_verified_at=connection.last_verified_at,
                last_error_code=connection.last_error_code,
            ),
            recon=self.readiness(record),
            problems=list(entry.problems) if entry is not None else [],
        )

    def _connection(self, key: str) -> SupplierConnectionSummary | None:
        if key not in self._catalog.definitions:
            return None
        return self._connect.supplier_connection(key)

    def _require_free(self, origin: str, host: str) -> None:
        if self._store.reserved_host(host):
            raise OnboardingConflictError(
                ONBOARDING_HOST_TAKEN, "that storefront belongs to a repository supplier"
            )
        taken = self._store.supplier_of_origin(origin)
        if taken is not None:
            raise OnboardingConflictError(
                ONBOARDING_HOST_TAKEN,
                "that storefront is already onboarded",
                details={"supplier_key": taken},
            )

    def _supported(self, template: str) -> str:
        if template not in detections(self._templates):
            raise InputValidationError(
                ONBOARDING_TEMPLATE_UNKNOWN,
                "an onboarded site runs on a platform ICBM has a template for",
                details={"templates": self.supported},
            )
        return template

    def _recent(self, origin: str) -> Detection | None:
        with self._lock:
            self._expire(self._clock.now())
            for pending in reversed(self._pending.values()):
                if pending.view.origin == origin:
                    return pending.detection
        return None

    def _expire(self, now: datetime) -> None:
        for key in [k for k, p in self._pending.items() if p.view.expires_at <= now]:
            del self._pending[key]


def _acceptable(origin: str | None, host: str) -> str | None:
    """A redirect target worth suggesting: another host that passes the guard itself."""
    if origin is None:
        return None
    try:
        suggested, suggested_host = onboarding_origin(origin)
    except InputValidationError:
        return None
    return suggested if suggested_host != onboarding_host(host) else None


def _policy_view(origin: str, document: PolicyDocument) -> PolicyDocumentView:
    robots = document.robots
    return PolicyDocumentView(
        document=document.document,
        path=document.path,
        url=f"{origin}{document.path}",
        status=document.status,
        failure=document.failure,
        content_sha256=document.content_sha256,
        truncated=document.truncated,
        robots=None
        if robots is None
        else RobotsView(
            lines=list(robots.lines),
            disallows_all=robots.disallows_all,
            disallows_products=robots.disallows_products,
            crawl_delay=robots.crawl_delay,
        ),
        warnings=[w.value for w in document.warnings],
    )


__all__ = ["UNSUPPORTED", "OnboardingService"]
