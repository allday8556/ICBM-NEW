"""Read shape of marketplace capability truth (CAPABILITY_MAPPING §17 #16, PR-B portion).

Every axis is its own field, taken from the domain state as is; nothing is pre-combined for
display. The three-layer projection and its glyphs belong to the UI (PR-D).
"""

from datetime import datetime

from pydantic import BaseModel

from app.connect.marketplace.capability import (
    AuthStatus,
    CapabilityState,
    ContractFreshness,
    EvidenceGrade,
    EvidenceStrength,
    PauseReason,
    RemoteOutcome,
    WorkflowScope,
    WorkflowState,
    WriteScopeStatus,
    WriteStatus,
)
from app.core.errors import ErrorClass


class WriteScopeView(BaseModel):
    status: WriteScopeStatus
    evidence_strength: EvidenceStrength | None
    evidence_grade: EvidenceGrade | None


class WriteView(BaseModel):
    status: WriteStatus


class WorkflowOverlayView(BaseModel):
    workflow_state: WorkflowState
    workflow_scope: WorkflowScope
    reason_code: PauseReason | None


class MarketplaceCapabilityView(BaseModel):
    marketplace_key: str
    auth: AuthStatus
    auth_verified_at: datetime | None
    write_scope: WriteScopeView
    write: WriteView
    contract_freshness: ContractFreshness
    workflow: list[WorkflowOverlayView]
    error_class: ErrorClass | None
    remote_outcome: RemoteOutcome | None
    updated_at: datetime | None

    @classmethod
    def of(
        cls, marketplace_key: str, state: CapabilityState, updated_at: datetime | None
    ) -> "MarketplaceCapabilityView":
        return cls(
            marketplace_key=marketplace_key,
            auth=state.auth,
            auth_verified_at=state.auth_verified_at,
            write_scope=WriteScopeView(
                status=state.write_scope.status,
                evidence_strength=state.write_scope.evidence_strength,
                evidence_grade=state.write_scope.evidence_grade,
            ),
            write=WriteView(status=state.write),
            contract_freshness=state.contract_freshness,
            workflow=[
                WorkflowOverlayView(
                    workflow_state=o.state, workflow_scope=o.scope, reason_code=o.reason_code
                )
                for o in state.overlays
            ],
            error_class=state.error_class,
            remote_outcome=state.remote_outcome,
            updated_at=updated_at,
        )
