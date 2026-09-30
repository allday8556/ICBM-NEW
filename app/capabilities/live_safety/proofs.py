"""The durable stage proofs the safety stack reads (ADR-0018 §5, §7, §8, §9).

- **restore proof** (§7, area 2): a PASSED restore drill of exactly this stage and target digest
  at the current schema head. The target digest is the stack's own, so a proof is stale as soon
  as any of the state it proved moves.
- **evidence retention** (§8, area 2): a PASSED retention proof of exactly the live checks.
- **visual acceptance** (§9, area 3): a reviewed record of exactly the code this process runs, at
  the current schema head (``app.capabilities.live_safety.visual``); any change of executed or
  served code makes it
  stale.
- **canary eligibility** (§5, §5.1): the highest eligibility record of exactly the lineage the
  stack derived for this stage unit, ``PROVEN_OUTSIDE`` with every bound identity and the review
  packet digest equal (``app.capabilities.live_safety.eligibility``). Any drift changes the
  lineage, so an earlier record no longer matches; no lineage, no record or an unreadable owner
  is unproven.
- **residual-risk acceptance** (§6.1, G3-30): an explicit user and architect acceptance recorded in
  GitHub. It has no durable owner and none is authorized here, so it is never proven in process and
  stays unproven, whatever grant, brake, drill, retention or visual record exists.
"""

from collections.abc import Callable

from app.capabilities.live_safety.eligibility import CanaryEligibilityService, EligibilityBinding
from app.capabilities.live_safety.model import MutationStage
from app.capabilities.live_safety.retention import RetentionProofService
from app.capabilities.live_safety.store import LiveAuthorityStore
from app.capabilities.live_safety.visual import VisualAcceptanceService


class DurableStageProofs:
    def __init__(
        self,
        *,
        store: LiveAuthorityStore,
        retention: RetentionProofService,
        visual: VisualAcceptanceService,
        eligibility: CanaryEligibilityService,
        schema_head: Callable[[], str | None],
    ) -> None:
        self._eligibility = eligibility
        self._store = store
        self._retention = retention
        self._visual = visual
        self._head = schema_head

    def canary_non_regulated(
        self, stage: MutationStage, unit_ref: str, binding: EligibilityBinding | None
    ) -> bool:
        return self._eligibility.proven(stage, unit_ref, binding)

    def residual_risk_accepted(self) -> bool:
        # §6.1: not recorded (§10), and the decision is recorded in GitHub rather than in the
        # application, so it has no durable owner here. Nothing in process may answer True, so the
        # §10 layer refuses every mutation of both stages at this main.
        return False

    def restore_proof(self, stage: MutationStage, target_digest: str) -> bool:
        head = self._head()
        if not target_digest or not head:
            return False
        with self._store.reading() as unit:
            return unit.passed_drill(stage, target_digest, head)

    def evidence_retention_ready(self) -> bool:
        return self._retention.ready()

    def visual_acceptance_recorded(self) -> bool:
        return self._visual.recorded()
