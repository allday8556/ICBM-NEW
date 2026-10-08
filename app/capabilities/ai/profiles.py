"""The AI provider profile and the provider it binds (ADR-0027 §2–§5, §8; AIS-1).

One profile (``default``) names the operator's CLIProxyAPI sidecar:
- a loopback endpoint;
- the owner's model (``gpt-5.6-sol``, Issue #219 ``6068160917``);
- the billing mode and the daily call cap;
- three approvals, each a protected, audited revision: the executable identity (path and SHA-256),
  the routing identity (configuration fingerprint with the remote catalog and the panel
  auto-update off), and the owner's data-transfer approval.

The client key is never stored by ICBM: it is read from the approved sidecar's own configuration
at call time (ADR-0027 §2, as amended by AIS-1).

``ProfiledProvider`` is the ADR-0012 port over that profile.
- Its requested identity exists only when every approval is recorded.
- Every call first proves the serving process against the approved executable and routing identity
  (fail-closed), then checks the data-transfer approval and the daily cap, then calls, and records
  the call in the ledger.
"""

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.capabilities.ai.profile_models import (
    AIProviderCall,
    AIProviderProfile,
    AIProviderProfileCurrent,
    AIProviderProfileRevision,
)
from app.capabilities.ai.provider import (
    AI_CAPABILITY_KEY,
    AIExecutionProvenance,
    BillingMode,
    ProviderOutcome,
    RequestedIdentity,
    TaskRequest,
    not_configured,
)
from app.capabilities.audit.models import AuditEventType, AuditOutcome
from app.capabilities.audit.service import AuditEntry, AuditLog
from app.platform.core.clock import Clock
from app.platform.core.errors import (
    AppError,
    ErrorClass,
    InputValidationError,
    PolicyBlockedError,
)
from app.platform.db.database import Database
from app.stages.connect.contracts import CapabilityReport
from app.stages.connect.state import CapabilityStatus
from integrations.ai import cliproxyapi, sidecar


class ProfileConflictError(AppError):
    """A profile save against a revision that is no longer current."""

    error_class = ErrorClass.CONFLICT


PROFILE_KEY: Final = "default"
PROVIDER_TYPE: Final = "CLIPROXYAPI"
REQUESTED_PROVIDER: Final = "cliproxyapi"
DEFAULT_MODEL: Final = "gpt-5.6-sol"
_LOOPBACK: Final = re.compile(r"^http://(127\.0\.0\.1|localhost):(\d{2,5})$")
_PROBE_TTL_S: Final = 10.0

AI_PROFILE_CURRENT_MOVED: Final = "AI_PROFILE_CURRENT_MOVED"
AI_PROFILE_INVALID: Final = "AI_PROFILE_INVALID"
AI_PROFILE_MISSING: Final = "AI_PROFILE_MISSING"
AI_EXECUTABLE_NOT_SERVING: Final = "AI_EXECUTABLE_NOT_SERVING"
AI_EXECUTABLE_MISMATCH: Final = "AI_EXECUTABLE_MISMATCH"
AI_ROUTING_MISMATCH: Final = "AI_ROUTING_MISMATCH"
AI_ROUTING_UNREADABLE: Final = "AI_ROUTING_UNREADABLE"
AI_ROUTING_UPDATES_ON: Final = "AI_ROUTING_UPDATES_ON"
AI_DATA_TRANSFER_NOT_APPROVED: Final = "AI_DATA_TRANSFER_NOT_APPROVED"
AI_DAILY_CAP_REACHED: Final = "AI_DAILY_CAP_REACHED"
AI_SIDECAR_KEY_UNREADABLE: Final = "AI_SIDECAR_KEY_UNREADABLE"
AI_PROFILE_UNREADABLE: Final = "AI_PROFILE_UNREADABLE"
AI_PROVIDER_CALL_FAILED: Final = "AI_PROVIDER_CALL_FAILED"

Action = Literal["CONFIGURE", "APPROVE_EXECUTABLE", "APPROVE_ROUTING", "DATA_TRANSFER"]

# The ADR-0012 §8 runtime state of a fully approved profile, by the reason a call is refused.
# A reached daily cap is the profile's policy, not the sidecar's state: the runtime stays
# AVAILABLE while the capability is DEGRADED (ADR-0027 §7).
_RUNTIME_STATE: Final = {
    AI_EXECUTABLE_NOT_SERVING: "UNAVAILABLE",
    AI_ROUTING_UNREADABLE: "UNAVAILABLE",
    AI_EXECUTABLE_MISMATCH: "VERSION_MISMATCH",
    AI_ROUTING_MISMATCH: "ROUTING_CONFIG_MISMATCH",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _port(endpoint: str) -> int:
    match = _LOOPBACK.match(endpoint)
    if match is None:
        raise InputValidationError(
            AI_PROFILE_INVALID, "the AI endpoint is loopback only: http://127.0.0.1:<port>"
        )
    return int(match.group(2))


@dataclass(frozen=True)
class ProfileRevision:
    revision_id: str
    revision_no: int
    content: Mapping[str, Any]
    action: str
    authored_by: str
    authored_at: datetime


class ProfileStore:
    """The only production writer of the four provider-profile tables."""

    def __init__(self, db: Database, clock: Clock, audit: AuditLog) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit

    def current(self) -> ProfileRevision | None:
        with self._db.read() as session:
            pointer = session.get(AIProviderProfileCurrent, PROFILE_KEY)
            if pointer is None:
                return None
            row = session.get(AIProviderProfileRevision, pointer.revision_id)
            return None if row is None else _record(row)

    def history(self) -> list[ProfileRevision]:
        with self._db.read() as session:
            rows = session.scalars(
                select(AIProviderProfileRevision)
                .where(AIProviderProfileRevision.profile_key == PROFILE_KEY)
                .order_by(AIProviderProfileRevision.revision_no.desc())
            ).all()
            return [_record(row) for row in rows]

    def append(
        self,
        content: Mapping[str, Any],
        *,
        action: Action,
        expected_current_revision: str | None,
        actor: str,
        correlation_id: str,
        audit_details: Mapping[str, Any],
    ) -> ProfileRevision:
        now = self._clock.now()
        with self._db.write() as session:
            pointer = session.get(AIProviderProfileCurrent, PROFILE_KEY)
            if (None if pointer is None else pointer.revision_id) != expected_current_revision:
                raise ProfileConflictError(
                    AI_PROFILE_CURRENT_MOVED,
                    "the AI provider profile changed since it was read; reload it",
                    details={"current_revision": None if pointer is None else pointer.revision_id},
                )
            if session.get(AIProviderProfile, PROFILE_KEY) is None:
                session.add(AIProviderProfile(profile_key=PROFILE_KEY, created_at=now))
                session.flush()
            number = 1 + int(
                session.scalar(
                    select(func.coalesce(func.max(AIProviderProfileRevision.revision_no), 0)).where(
                        AIProviderProfileRevision.profile_key == PROFILE_KEY
                    )
                )
                or 0
            )
            row = AIProviderProfileRevision(
                revision_id=str(uuid.uuid4()),
                profile_key=PROFILE_KEY,
                revision_no=number,
                content_json=_canonical(dict(content)),
                content_fingerprint=hashlib.sha256(_canonical(dict(content)).encode()).hexdigest(),
                action=action,
                authored_by=actor,
                correlation_id=correlation_id,
                authored_at=now,
            )
            session.add(row)
            session.flush()
            if pointer is None:
                session.add(
                    AIProviderProfileCurrent(
                        profile_key=PROFILE_KEY,
                        revision_id=row.revision_id,
                        moved_by=actor,
                        correlation_id=correlation_id,
                        moved_at=now,
                    )
                )
            else:
                pointer.revision_id = row.revision_id
                pointer.moved_by = actor
                pointer.correlation_id = correlation_id
                pointer.moved_at = now
            session.flush()
            self._audit.append(
                AuditEntry(
                    event_type=AuditEventType.AI_PROVIDER_PROFILE_REVISED,
                    action=f"AI_PROVIDER_{action}",
                    actor=actor,
                    outcome=AuditOutcome.RECORDED,
                    target_ref=PROFILE_KEY,
                    after={"revision": row.revision_id, "revision_no": number},
                    details=dict(audit_details),
                    correlation_id=correlation_id,
                ),
                session=session,
            )
            return _record(row)

    def calls_on(self, day: str) -> int:
        with self._db.read() as session:
            return int(
                session.scalar(
                    select(func.count()).where(
                        AIProviderCall.profile_key == PROFILE_KEY, AIProviderCall.call_day == day
                    )
                )
                or 0
            )

    def reserve_call(
        self, revision_id: str, task_key: str, cap: int, correlation_id: str
    ) -> str | None:
        """Count today's calls and, below ``cap``, write this one as ``SENT``, in one serialized
        write unit: two calls can never both take the last place (AIS-04). ``None`` at the cap."""
        now = self._clock.now()
        day = now.date().isoformat()
        with self._db.write() as session:
            made = session.scalar(
                select(func.count()).where(
                    AIProviderCall.profile_key == PROFILE_KEY, AIProviderCall.call_day == day
                )
            )
            if int(made or 0) >= cap:
                return None
            call_id = str(uuid.uuid4())
            session.add(
                AIProviderCall(
                    call_id=call_id,
                    profile_key=PROFILE_KEY,
                    revision_id=revision_id,
                    call_day=day,
                    task_key=task_key,
                    outcome="SENT",
                    error_code=None,
                    correlation_id=correlation_id,
                    called_at=now,
                )
            )
            return call_id

    def settle_call(self, call_id: str, ok: bool, error_code: str | None) -> None:
        """Settle a ``SENT`` call once, to ``OK`` or ``FAILED``."""
        with self._db.write() as session:
            row = session.get(AIProviderCall, call_id)
            assert row is not None and row.outcome == "SENT"
            row.outcome = "OK" if ok else "FAILED"
            row.error_code = error_code


def _record(row: AIProviderProfileRevision) -> ProfileRevision:
    return ProfileRevision(
        revision_id=row.revision_id,
        revision_no=row.revision_no,
        content=json.loads(row.content_json),
        action=row.action,
        authored_by=row.authored_by,
        authored_at=row.authored_at,
    )


# ------------------------------------------------------------------ the provider binding

Complete = Callable[[str, str, str, str], cliproxyapi.SidecarAnswer]


def _complete(endpoint: str, key: str, model: str, text: str) -> cliproxyapi.SidecarAnswer:
    return cliproxyapi.complete(endpoint, key, model, text)


@dataclass(frozen=True)
class Observation:
    process: sidecar.ServingProcess | None
    routing: sidecar.RoutingObservation | None


class ProfiledProvider:
    """The ADR-0012 port over the current profile and the process actually serving it."""

    def __init__(
        self,
        store: ProfileStore,
        probe: sidecar.ServingProcessProbe,
        clock: Clock,
        complete: Complete = _complete,
    ) -> None:
        self._store = store
        self._probe = probe
        self._clock = clock
        self._complete = complete
        self._observed: tuple[float, int, Observation] | None = None

    # ------------------------------------------------------------ what is approved and served

    def observe(self, port: int, *, fresh: bool = False) -> Observation:
        cached = self._observed
        if (
            not fresh
            and cached
            and cached[1] == port
            and time.monotonic() - cached[0] < _PROBE_TTL_S
        ):
            return cached[2]
        process = self._probe.serving(port)
        observation = Observation(process, None if process is None else sidecar.routing(process))
        self._observed = (time.monotonic(), port, observation)
        return observation

    def requested_identity(self) -> RequestedIdentity | None:
        current = self._store.current()
        content = None if current is None else current.content
        if not content or _missing(content):
            return None
        return RequestedIdentity(
            requested_provider=REQUESTED_PROVIDER,
            requested_model=str(content["requested_model"]),
            routing_config_version=str(content["approved_routing"]["fingerprint"]),
            proxy_version=str(content["approved_executable"]["sha256"]),
        )

    def capability_report(self) -> CapabilityReport:
        """The ``ai`` capability. A capability never fails core readiness (ADR-0012 §9): a profile
        store that cannot be read, as before the schema is migrated, is a degraded capability."""
        try:
            return self._capability_report()
        except SQLAlchemyError:
            return _report(CapabilityStatus.DEGRADED, AI_PROFILE_UNREADABLE)

    def _capability_report(self) -> CapabilityReport:
        current = self._store.current()
        if current is None:
            return _report(CapabilityStatus.NOT_CONFIGURED, "no AI provider is configured")
        missing = _missing(current.content)
        if missing:
            return _report(CapabilityStatus.NOT_CONFIGURED, "not approved: " + ", ".join(missing))
        problem = self._problem(current.content, self.observe(_port(current.content["endpoint"])))
        if problem is not None:
            return _report(CapabilityStatus.DEGRADED, problem)
        if self._store.calls_on(self._today()) >= int(current.content["daily_call_cap"]):
            return _report(CapabilityStatus.DEGRADED, AI_DAILY_CAP_REACHED)
        return _report(
            CapabilityStatus.READY,
            f"provider={REQUESTED_PROVIDER} model={current.content['requested_model']}",
        )

    def _problem(self, content: Mapping[str, Any], observed: Observation) -> str | None:
        approved = content["approved_executable"]
        process = observed.process
        if process is None:
            return AI_EXECUTABLE_NOT_SERVING
        if (process.path.lower(), process.sha256) != (
            str(approved["path"]).lower(),
            approved["sha256"],
        ):
            return AI_EXECUTABLE_MISMATCH
        if observed.routing is None:
            return AI_ROUTING_UNREADABLE
        if observed.routing.fingerprint != content["approved_routing"]["fingerprint"]:
            return AI_ROUTING_MISMATCH
        return None

    def runtime_state(self) -> str:
        """``NOT_CONFIGURED`` until every approval exists, then ADR-0012 §8's runtime state."""
        current = self._store.current()
        if current is None or _missing(current.content):
            return "NOT_CONFIGURED"
        problem = self._problem(current.content, self.observe(_port(current.content["endpoint"])))
        return "AVAILABLE" if problem is None else _RUNTIME_STATE[problem]

    def _today(self) -> str:
        return self._clock.now().date().isoformat()

    # ------------------------------------------------------------ one call

    def execute(self, request: TaskRequest) -> ProviderOutcome:
        current = self._store.current()
        if current is None or _missing(current.content):
            raise not_configured()
        content = current.content
        observed = self.observe(_port(content["endpoint"]), fresh=True)
        problem = self._problem(content, observed)
        if problem is not None:
            raise PolicyBlockedError(
                problem, "the served sidecar is not the approved one; nothing was sent"
            )
        assert observed.process is not None and observed.routing is not None
        key = sidecar.client_key(observed.process)
        if key is None:
            raise PolicyBlockedError(
                AI_SIDECAR_KEY_UNREADABLE, "the sidecar's client key cannot be read"
            )
        # The place under the cap is taken before anything is sent, atomically (AIS-04).
        call_id = self._store.reserve_call(
            current.revision_id,
            request.task_key,
            int(content["daily_call_cap"]),
            request.correlation_id,
        )
        if call_id is None:
            raise PolicyBlockedError(
                AI_DAILY_CAP_REACHED, "the profile's daily call cap is reached"
            )
        try:
            answer = self._complete(
                str(content["endpoint"]), key, str(content["requested_model"]), request.text
            )
        except BaseException:
            self._store.settle_call(call_id, False, AI_PROVIDER_CALL_FAILED)
            raise
        provenance = AIExecutionProvenance(
            requested_provider=REQUESTED_PROVIDER,
            requested_model=str(content["requested_model"]),
            actual_provider=None,
            actual_model=answer.actual_model,
            proxy_name="CLIProxyAPI",
            proxy_version=str(content["approved_executable"]["sha256"]),
            proxy_binary_sha256=observed.process.sha256,
            routing_config_version=observed.routing.fingerprint,
            alias_applied=None,
            fallback_applied=None,
            billing_mode=BillingMode(content["billing_mode"]),
            tokens_in=answer.tokens_in,
            tokens_out=answer.tokens_out,
            vendor_cost=None,
            estimated_cost=None,
            allocated_cost=None,
            latency_ms=answer.latency_ms,
        )
        ok = answer.value is not None
        self._store.settle_call(call_id, ok, answer.error_code)
        if ok:
            return ProviderOutcome(ok=True, provenance=provenance, value=answer.value)
        return ProviderOutcome(
            ok=False,
            provenance=provenance,
            error_class=ErrorClass(answer.error_kind or "UNKNOWN"),
            error_code=answer.error_code,
        )


def _report(status: CapabilityStatus, detail: str) -> CapabilityReport:
    return CapabilityReport(key=AI_CAPABILITY_KEY, status=status, detail=detail)


def _missing(content: Mapping[str, Any]) -> list[str]:
    missing = []
    if not content.get("approved_executable"):
        missing.append("executable")
    if not content.get("approved_routing"):
        missing.append("routing")
    if not content.get("data_transfer_approved"):
        missing.append("data_transfer")
    return missing


# ------------------------------------------------------------------ the Settings contract


class ConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: StrictStr = Field(min_length=1, max_length=64)
    expected_current_revision: StrictStr | None = Field(default=None, max_length=36)
    endpoint: StrictStr = Field(min_length=1, max_length=64)
    requested_model: StrictStr = Field(min_length=1, max_length=64)
    billing_mode: Literal["SUBSCRIPTION", "METERED_API", "UNKNOWN"]
    daily_call_cap: StrictInt = Field(ge=1, le=10_000)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: StrictStr = Field(min_length=1, max_length=64)
    expected_current_revision: StrictStr = Field(min_length=1, max_length=36)
    # What the operator saw and approves; it must still be what is served.
    observed: StrictStr = Field(min_length=64, max_length=64)


class DataTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: StrictStr = Field(min_length=1, max_length=64)
    expected_current_revision: StrictStr = Field(min_length=1, max_length=36)
    approved: StrictBool


class ObservedView(BaseModel):
    serving: bool
    path: str | None
    sha256: str | None
    routing_fingerprint: str | None
    local_model: bool | None
    panel_auto_update_disabled: bool | None


class ProviderView(BaseModel):
    capability: CapabilityReport
    runtime_state: str
    current_revision: str | None
    revision_no: int | None
    content: dict[str, Any] | None
    observed: ObservedView | None
    calls_today: int
    history: list[dict[str, Any]]


class ProviderProfileService:
    def __init__(self, store: ProfileStore, provider: ProfiledProvider, clock: Clock) -> None:
        self._store = store
        self._provider = provider
        self._clock = clock

    def view(self) -> ProviderView:
        current = self._store.current()
        observed = None
        if current is not None:
            seen = self._provider.observe(_port(current.content["endpoint"]), fresh=True)
            observed = ObservedView(
                serving=seen.process is not None,
                path=None if seen.process is None else seen.process.path,
                sha256=None if seen.process is None else seen.process.sha256,
                routing_fingerprint=None if seen.routing is None else seen.routing.fingerprint,
                local_model=None if seen.routing is None else seen.routing.local_model,
                panel_auto_update_disabled=(
                    None if seen.routing is None else seen.routing.panel_auto_update_disabled
                ),
            )
        return ProviderView(
            capability=self._provider.capability_report(),
            runtime_state=self._provider.runtime_state(),
            current_revision=None if current is None else current.revision_id,
            revision_no=None if current is None else current.revision_no,
            content=None if current is None else dict(current.content),
            observed=observed,
            calls_today=self._store.calls_on(self._clock.now().date().isoformat()),
            history=[
                {
                    "revision_no": r.revision_no,
                    "action": r.action,
                    "authored_by": r.authored_by,
                    "authored_at": r.authored_at.isoformat(),
                }
                for r in self._store.history()
            ],
        )

    def configure(self, request: ConfigureRequest, *, cid: str) -> ProviderView:
        _port(request.endpoint)
        current = self._store.current()
        base = (
            dict(current.content)
            if current
            else {
                "provider_type": PROVIDER_TYPE,
                "credential_source": "SIDECAR_CONFIG",
                "approved_executable": None,
                "approved_routing": None,
                "data_transfer_approved": False,
            }
        )
        content = {
            **base,
            "endpoint": request.endpoint,
            "requested_model": request.requested_model,
            "billing_mode": request.billing_mode,
            "daily_call_cap": request.daily_call_cap,
        }
        self._store.append(
            content,
            action="CONFIGURE",
            expected_current_revision=request.expected_current_revision,
            actor=request.actor,
            correlation_id=cid,
            audit_details={
                "endpoint": request.endpoint,
                "requested_model": request.requested_model,
                "billing_mode": request.billing_mode,
                "daily_call_cap": request.daily_call_cap,
            },
        )
        return self.view()

    def approve_executable(self, request: ApproveRequest, *, cid: str) -> ProviderView:
        current = self._require_current()
        seen = self._provider.observe(_port(current.content["endpoint"]), fresh=True)
        if seen.process is None or seen.process.sha256 != request.observed:
            raise PolicyBlockedError(
                AI_EXECUTABLE_MISMATCH, "only the executable actually serving now can be approved"
            )
        approved = {"path": seen.process.path, "sha256": seen.process.sha256}
        self._store.append(
            {**current.content, "approved_executable": approved},
            action="APPROVE_EXECUTABLE",
            expected_current_revision=request.expected_current_revision,
            actor=request.actor,
            correlation_id=cid,
            audit_details=approved,
        )
        return self.view()

    def approve_routing(self, request: ApproveRequest, *, cid: str) -> ProviderView:
        current = self._require_current()
        seen = self._provider.observe(_port(current.content["endpoint"]), fresh=True)
        routing = seen.routing
        if routing is None or routing.fingerprint != request.observed:
            raise PolicyBlockedError(
                AI_ROUTING_MISMATCH, "only the routing identity actually served now can be approved"
            )
        if not (routing.local_model and routing.panel_auto_update_disabled):
            raise PolicyBlockedError(
                AI_ROUTING_UPDATES_ON,
                "the sidecar must run with -local-model and disable-auto-update-panel: true",
                details={
                    "local_model": routing.local_model,
                    "panel_auto_update_disabled": routing.panel_auto_update_disabled,
                },
            )
        approved = {
            "fingerprint": routing.fingerprint,
            "local_model": routing.local_model,
            "panel_auto_update_disabled": routing.panel_auto_update_disabled,
        }
        self._store.append(
            {**current.content, "approved_routing": approved},
            action="APPROVE_ROUTING",
            expected_current_revision=request.expected_current_revision,
            actor=request.actor,
            correlation_id=cid,
            audit_details=approved,
        )
        return self.view()

    def set_data_transfer(self, request: DataTransferRequest, *, cid: str) -> ProviderView:
        current = self._require_current()
        self._store.append(
            {**current.content, "data_transfer_approved": request.approved},
            action="DATA_TRANSFER",
            expected_current_revision=request.expected_current_revision,
            actor=request.actor,
            correlation_id=cid,
            audit_details={"data_transfer_approved": request.approved},
        )
        return self.view()

    def _require_current(self) -> ProfileRevision:
        current = self._store.current()
        if current is None:
            raise PolicyBlockedError(AI_PROFILE_MISSING, "configure the AI provider profile first")
        return current
