"""Owner of marketplace capability truth (M2 PR-B).

Transitions are the pure functions of ``app.connect.marketplace.capability``. This service only
loads a state, applies one transition, persists the result and audits the change, all in one
write transaction. It holds no gateway, credential or session, so it cannot reach a provider:
the SmartStore adapter (PR-A) and the permission attestation (PR-C) feed it typed evidence, and
the local operator records reviewed contract freshness (CAPABILITY_MAPPING F8).
"""

import contextlib
import logging
from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.models import AuditEventType, AuditOutcome
from app.audit.service import AuditEntry, AuditLog
from app.connect.marketplace.capability import (
    DEFAULT_POLICY,
    INITIAL,
    AuthEvidence,
    AuthStatus,
    CapabilityInvariantError,
    CapabilityPolicy,
    CapabilityState,
    ContractFreshness,
    EvidenceStrength,
    ExpansionBlockedError,
    FailureEvidence,
    FreshnessTransitionError,
    PauseReason,
    RemoteOutcome,
    Resolution,
    WorkflowOverlay,
    WorkflowScope,
    WorkflowState,
    WriteScope,
    WriteScopeStatus,
    WriteStatus,
    observe_auth,
    observe_failure,
    observe_permission,
    on_process_start,
    record_freshness,
    resolve,
)
from app.connect.marketplace.contracts import MarketplaceCapabilityView
from app.connect.marketplace.models import MarketplaceCapability, MarketplaceWorkflowOverlay
from app.connect.marketplace.sources import PermissionEvidenceSource
from app.core.clock import Clock
from app.core.errors import ErrorClass, InputValidationError, NotFoundError, PolicyBlockedError
from app.core.safe_payload import safe_payload
from app.db.database import Database

logger = logging.getLogger("icbm.connect.marketplace")

# Marketplaces with an adopted capability contract. SmartStore is the only one in M2.
CAPABILITY_MARKETPLACES: tuple[str, ...] = ("smartstore",)
SYSTEM_ACTOR = "system:connect"

Transition = Callable[[CapabilityState], CapabilityState]


def _target_ref(marketplace_key: str) -> str:
    return f"marketplace:{marketplace_key}"


def _overlay(row: MarketplaceWorkflowOverlay) -> WorkflowOverlay:
    return WorkflowOverlay(
        WorkflowState(row.workflow_state),
        WorkflowScope(row.workflow_scope),
        PauseReason(row.reason_code) if row.reason_code is not None else None,
        row.session_generation,
    )


def _state(
    row: MarketplaceCapability | None, overlays: Sequence[MarketplaceWorkflowOverlay]
) -> CapabilityState:
    """Rebuild the domain state; constructing it re-checks every invariant of the contract."""
    if row is None:
        return INITIAL
    strength = row.evidence_strength
    return CapabilityState(
        auth=AuthStatus(row.auth),
        write_scope=WriteScope(
            WriteScopeStatus(row.write_scope_status),
            EvidenceStrength(strength) if strength is not None else None,
        ),
        write=WriteStatus(row.write_status),
        contract_freshness=ContractFreshness(row.contract_freshness),
        overlays=tuple(_overlay(o) for o in overlays),
        error_class=ErrorClass(row.error_class) if row.error_class is not None else None,
        remote_outcome=RemoteOutcome(row.remote_outcome)
        if row.remote_outcome is not None
        else None,
        auth_verified_at=row.auth_verified_at,
        session_generation_floor=row.session_generation_floor,
        freshness_recorded_at=row.freshness_recorded_at,
    )


def _axes(state: CapabilityState) -> dict[str, object]:
    """The axes as enum values and overlay markers only — never evidence content."""
    return {
        "auth": state.auth,
        "write_scope_status": state.write_scope.status,
        "evidence_strength": state.write_scope.evidence_strength,
        "write_status": state.write,
        "contract_freshness": state.contract_freshness,
        "freshness_recorded_at": state.freshness_recorded_at,
        "workflow": [
            ":".join(part for part in (o.scope, o.state, o.reason_code) if part)
            for o in state.overlays
        ],
        "error_class": state.error_class,
        "remote_outcome": state.remote_outcome,
    }


class MarketplaceCapabilityService:
    def __init__(
        self,
        *,
        db: Database,
        clock: Clock,
        audit: AuditLog,
        marketplace_keys: Sequence[str] = CAPABILITY_MARKETPLACES,
        policy: CapabilityPolicy = DEFAULT_POLICY,
    ) -> None:
        self._db = db
        self._clock = clock
        self._audit = audit
        self._keys = tuple(marketplace_keys)
        self._policy = policy
        self._permission_evidence: PermissionEvidenceSource | None = None

    def set_permission_evidence(self, source: PermissionEvidenceSource) -> None:
        """Wire the permission evidence write_scope converges on (PR-C A0 attestations)."""
        self._permission_evidence = source

    # ------------------------------------------------------------------ reads

    def _known(self, marketplace_key: str) -> None:
        if marketplace_key not in self._keys:
            raise NotFoundError(
                "MARKETPLACE_CAPABILITY_UNKNOWN",
                f"no capability contract for marketplace {marketplace_key!r}",
            )

    @staticmethod
    def _load(
        session: Session, marketplace_key: str
    ) -> tuple[MarketplaceCapability | None, list[MarketplaceWorkflowOverlay]]:
        row = session.get(MarketplaceCapability, marketplace_key)
        overlays = session.scalars(
            select(MarketplaceWorkflowOverlay).where(
                MarketplaceWorkflowOverlay.marketplace_key == marketplace_key
            )
        ).all()
        return row, list(overlays)

    def capability(self, marketplace_key: str) -> MarketplaceCapabilityView:
        self._known(marketplace_key)
        # Capability truth is derived from current evidence: converge before answering.
        self.converge_permission(marketplace_key)
        with self._db.read() as session:
            row, overlays = self._load(session, marketplace_key)
            return MarketplaceCapabilityView.of(
                marketplace_key, _state(row, overlays), row.updated_at if row else None
            )

    def capabilities(self) -> list[MarketplaceCapabilityView]:
        return [self.capability(key) for key in self._keys]

    # ------------------------------------------------------------------ evidence and actions

    def converge_permission(self, marketplace_key: str) -> None:
        """Converge write_scope on current permission evidence (§5, S6, S7; PR-C).

        Evidence that expired, lost its application or mapping binding, or was never recorded
        is judged by the evidence source. Expired MISSING converges to UNKNOWN and so releases
        the SCOPE_INSUFFICIENT pause it was the basis of, without promoting write (S6). A READY
        promotion that the contract freshness blocks (F5) leaves write_scope as it is: the
        evidence waits, and the A0 projection says why.

        This runs on every read, so it is also where time-driven expiry converges (S7). Only a
        real change is written and audited, with the reasons the evidence gave; a read after
        convergence finds nothing to change and writes nothing.
        """
        self._known(marketplace_key)
        if self._permission_evidence is None:
            return
        evidence = self._permission_evidence.current_permission(marketplace_key)
        if evidence is None:
            return
        scope = evidence.write_scope
        with self._db.read() as session:
            row, overlays = self._load(session, marketplace_key)
            current = _state(row, overlays).write_scope
        if current == scope:
            return
        with contextlib.suppress(PolicyBlockedError):
            self._apply(
                marketplace_key,
                "OBSERVE_PERMISSION",
                lambda s: observe_permission(s, scope),
                invalidations=list(evidence.invalidations),
            )

    def observe_auth(
        self, marketplace_key: str, evidence: AuthEvidence
    ) -> MarketplaceCapabilityView:
        return self._apply(marketplace_key, "OBSERVE_AUTH", lambda s: observe_auth(s, evidence))

    def observe_permission(
        self, marketplace_key: str, write_scope: WriteScope
    ) -> MarketplaceCapabilityView:
        return self._apply(
            marketplace_key, "OBSERVE_PERMISSION", lambda s: observe_permission(s, write_scope)
        )

    def observe_failure(
        self, marketplace_key: str, failure: FailureEvidence
    ) -> MarketplaceCapabilityView:
        return self._apply(
            marketplace_key,
            "OBSERVE_FAILURE",
            lambda s: observe_failure(s, failure, self._policy),
        )

    def record_contract_freshness(
        self, marketplace_key: str, freshness: ContractFreshness, *, actor: str
    ) -> MarketplaceCapabilityView:
        """A local operator's reviewed contract-freshness determination (F8).

        It is persisted with ``freshness_recorded_at`` and audited with the actor even when the
        value is unchanged: a same-value re-recording is an event, so this path never relies on
        ``after != before`` (F7). It makes no provider call. Afterwards permission evidence that
        was waiting on a CURRENT contract converges.
        """
        recorded_at = self._clock.now()
        self._apply(
            marketplace_key,
            "RECORD_CONTRACT_FRESHNESS",
            lambda s: record_freshness(s, freshness, recorded_at=recorded_at),
            actor=actor,
            outcome=AuditOutcome.ALLOWED,
            always_record=True,
        )
        return self.capability(marketplace_key)

    def resolve(
        self, marketplace_key: str, scope: WorkflowScope, resolution: Resolution, *, actor: str
    ) -> MarketplaceCapabilityView:
        """Explicit, audited operator action: the only way an overlay is lifted."""
        return self._apply(
            marketplace_key,
            "RESOLVE_WORKFLOW",
            lambda s: resolve(s, scope, resolution),
            actor=actor,
            outcome=AuditOutcome.ALLOWED,
            resolution=resolution,
        )

    def _apply(
        self,
        marketplace_key: str,
        event: str,
        transition: Transition,
        *,
        actor: str = SYSTEM_ACTOR,
        outcome: AuditOutcome = AuditOutcome.RECORDED,
        always_record: bool = False,
        **details: object,
    ) -> MarketplaceCapabilityView:
        self._known(marketplace_key)
        with self._db.write() as session:
            row, overlays = self._load(session, marketplace_key)
            before = _state(row, overlays)
            try:
                after = transition(before)
            except ExpansionBlockedError as exc:
                raise PolicyBlockedError("MARKETPLACE_CONTRACT_NOT_CURRENT", str(exc)) from exc
            except FreshnessTransitionError as exc:
                raise InputValidationError(
                    "MARKETPLACE_FRESHNESS_TRANSITION_FORBIDDEN", str(exc)
                ) from exc
            except CapabilityInvariantError as exc:
                raise InputValidationError("MARKETPLACE_CAPABILITY_INVARIANT", str(exc)) from exc
            if always_record or after != before:
                row = self._save(session, marketplace_key, row, overlays, after)
                self._audit.append(
                    AuditEntry(
                        event_type=AuditEventType.MARKETPLACE_CAPABILITY_CHANGED,
                        action=event,
                        actor=actor,
                        outcome=outcome,
                        target_ref=_target_ref(marketplace_key),
                        before=safe_payload(**_axes(before)),
                        after=safe_payload(**_axes(after)),
                        details=safe_payload(
                            marketplace_key=marketplace_key, event=event, **details
                        ),
                    ),
                    session=session,
                )
                logger.info(
                    "marketplace.capability.changed",
                    extra=safe_payload(
                        marketplace_key=marketplace_key, event=event, **_axes(after)
                    ),
                )
            updated_at = row.updated_at if row else None
        return MarketplaceCapabilityView.of(marketplace_key, after, updated_at)

    def _save(
        self,
        session: Session,
        marketplace_key: str,
        row: MarketplaceCapability | None,
        stored: Sequence[MarketplaceWorkflowOverlay],
        state: CapabilityState,
    ) -> MarketplaceCapability:
        now = self._clock.now()
        if row is None:
            row = MarketplaceCapability(marketplace_key=marketplace_key, created_at=now)
            session.add(row)
        row.auth = state.auth
        row.auth_verified_at = state.auth_verified_at
        row.write_scope_status = state.write_scope.status
        row.evidence_strength = state.write_scope.evidence_strength
        row.write_status = state.write
        row.contract_freshness = state.contract_freshness
        row.freshness_recorded_at = state.freshness_recorded_at
        row.error_class = state.error_class
        row.remote_outcome = state.remote_outcome
        row.session_generation_floor = state.session_generation_floor
        row.updated_at = now
        session.flush()
        # An unchanged overlay keeps its row (and its entered_at); any other is replaced.
        kept = {o.scope for o in state.overlays}
        for old in stored:
            if WorkflowScope(old.workflow_scope) not in kept or _overlay(old) not in state.overlays:
                session.delete(old)
        session.flush()
        unchanged = {_overlay(o) for o in stored}
        for overlay in state.overlays:
            if overlay not in unchanged:
                session.add(
                    MarketplaceWorkflowOverlay(
                        marketplace_key=marketplace_key,
                        workflow_scope=overlay.scope,
                        workflow_state=overlay.state,
                        reason_code=overlay.reason_code,
                        session_generation=overlay.session_generation,
                        entered_at=now,
                    )
                )
        session.flush()
        return row

    # ------------------------------------------------------------------ lifecycle

    def normalize_on_startup(self) -> int:
        """A new process holds no proof: a persisted READY is demoted before any request.

        This is a process boundary, not evidence, so it is logged rather than audited (as the
        supplier connections are). No provider request is made.
        """
        changed = 0
        with self._db.write() as session:
            for row in session.scalars(select(MarketplaceCapability)).all():
                key = row.marketplace_key
                _, overlays = self._load(session, key)
                before = _state(row, overlays)
                after = on_process_start(before)
                if after == before:
                    continue
                self._save(session, key, row, overlays, after)
                changed += 1
                logger.info(
                    "marketplace.capability.changed",
                    extra=safe_payload(marketplace_key=key, event="PROCESS_START", **_axes(after)),
                )
        return changed
