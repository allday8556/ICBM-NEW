"""Test seams for the pre-LIVE safety owners (ADR-0018; Gate 3 area 1). Never production.

- :class:`AdmittingAuthority` stands in for the safety stack where a test exercises the REGISTER
  CREATE state machine behind it (provider-zero fakes only). It admits and records; the real
  stack's refusals are exercised by ``tests/integration/test_g3a_live_authority.py``.
- :class:`PermittedMode` and :class:`ProvenProofs` let a test reach the deeper layers of the real
  :class:`~app.live.stack.SafetyStack` (grant, attempt owner, replay fence). Production wires the
  execution-mode owner (``M0_DRY_RUN_ONLY``) and :class:`~app.live.stack.UnprovenStageProofs`.
- :class:`ScriptedSender` is a provider-zero ASSET sender that records every call.
"""

from dataclasses import dataclass, field
from typing import Any

from app.core.execution import ExecutionMode
from app.live.assets import TransmissionPrecluded, UploadSendResult
from app.live.model import MutationRefused, MutationStage
from app.live.stack import CandidateState
from app.system.execution_mode import ExecutionModeState

SMARTSTORE_WIRE = ("POST", "api.commerce.naver.com", "/external/v1/product-images/upload")


class AdmittingAuthority:
    """Admits every CREATE the state machine reaches and records it. Tests only."""

    def __init__(self) -> None:
        self.admitted: list[tuple[str, int]] = []
        self.refusals: list[str] = []

    def admit_create(
        self,
        session: Any,
        *,
        intent: Any,
        attempt_no: int,
        endpoint_adopted: bool,
        actor: str,
        correlation_id: str,
    ) -> None:
        self.admitted.append((intent.intent_id, attempt_no))

    def record_refusal(
        self,
        refusal: MutationRefused,
        *,
        stage: MutationStage,
        target_ref: str,
        actor: str,
        correlation_id: str,
    ) -> None:
        self.refusals.append(refusal.code)


class PermittedMode:
    """An execution mode that permits live writes. No production module can build one."""

    def state(self) -> ExecutionModeState:
        return ExecutionModeState(
            mode=ExecutionMode.LIVE, live_writes_permitted=True, policy="TEST"
        )


class ProvenProofs:
    """Every §5/§7/§8/§9 prerequisite proven, unless a test names one as missing."""

    def __init__(self, *, missing: frozenset[str] = frozenset()) -> None:
        self.missing = missing
        self.restore_targets: list[str] = []

    def canary_non_regulated(self, stage: MutationStage, unit_ref: str) -> bool:
        return "eligibility" not in self.missing

    def restore_proof(self, stage: MutationStage, target_digest: str) -> bool:
        self.restore_targets.append(target_digest)
        return "restore" not in self.missing

    def evidence_retention_ready(self) -> bool:
        return "retention" not in self.missing

    def visual_acceptance_recorded(self) -> bool:
        return "visual" not in self.missing


@dataclass
class FixedCandidates:
    """The candidate preflight the stack reads, set by the test."""

    states: dict[str, CandidateState] = field(default_factory=dict)

    def current(self, preparation_revision_id: str) -> CandidateState:
        return self.states.get(
            preparation_revision_id,
            CandidateState(preparation_revision_id, current=False, ready=False, fingerprint=None),
        )


@dataclass
class ScriptedSender:
    """A provider-zero ASSET sender: each call pops the next scripted result."""

    marketplace_key: str = "smartstore"
    wire_value: tuple[str, str, str] = SMARTSTORE_WIRE
    label: str = "m5-image-upload-r1"
    wired: bool = True
    adopted: bool = True
    script: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def available(self) -> bool:
        return self.wired

    def endpoint_adopted(self) -> bool:
        return self.adopted

    def wire(self) -> tuple[str, str, str]:
        return self.wire_value

    def contract_label(self) -> str:
        return self.label

    def send(self, *, content: bytes, file_name: str, media_type: str) -> UploadSendResult:
        self.calls.append({"content": content, "file_name": file_name, "media_type": media_type})
        outcome = self.script.pop(0) if self.script else None
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            raise TransmissionPrecluded("nothing scripted")
        assert isinstance(outcome, UploadSendResult)
        return outcome
