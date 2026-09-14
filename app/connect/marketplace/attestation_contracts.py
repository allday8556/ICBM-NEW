"""Read shape of SMARTSTORE-A0-PERMISSION handling (M2 PR-C).

It keeps apart what the evidence says, whether it is current, and what it did to capability truth,
so "stored but not promoted" never collapses into "permission unknown". The three-layer status
projection with its glyphs belongs to the UI (PR-D).
"""

from datetime import datetime

from pydantic import BaseModel

from app.connect.marketplace.attestation import (
    ApiGroup,
    EvidenceFreshness,
    EvidenceSource,
    Invalidation,
    Promotion,
)
from app.connect.marketplace.capability import (
    ContractFreshness,
    EvidenceStrength,
    WriteScopeStatus,
)
from app.connect.marketplace.contracts import WriteScopeView


class AttestationRecordView(BaseModel):
    """The latest recorded evidence envelope. No fingerprint, client_id or secret."""

    attestation_seq: int
    evidence_source: EvidenceSource
    evidence_strength: EvidenceStrength
    observed_at: datetime
    required_groups: list[ApiGroup]
    observed_groups: list[ApiGroup]
    endpoint_mapping_revision: str
    attested_status: WriteScopeStatus
    recorded_by: str


class AttestationEvaluationView(BaseModel):
    write_scope: WriteScopeView
    freshness_status: EvidenceFreshness | None
    invalidations: list[Invalidation]


class PermissionAttestationView(BaseModel):
    marketplace_key: str
    # Input: the required groups come from the permission contract, never from the operator.
    required_groups: list[ApiGroup]
    selectable_groups: list[ApiGroup]
    group_labels: dict[ApiGroup, str]
    recording_available: bool
    # Why a new attestation cannot be recorded right now (nothing would be stored).
    recording_refusal: Invalidation | None
    max_age_days: int | None
    # Evidence, its current evaluation, and its effect on capability truth.
    attestation: AttestationRecordView | None
    evaluation: AttestationEvaluationView | None
    promotion: Promotion
    contract_freshness: ContractFreshness
    capability_write_scope: WriteScopeView
