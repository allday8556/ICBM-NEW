"""The durable stage proofs the safety stack reads (ADR-0018 §5, §7, §8, §9).

- **restore proof** (§7, area 2): a PASSED restore drill of exactly this stage and target digest
  at the current schema head. The target digest is the stack's own, so a proof is stale as soon
  as any of the state it proved moves.
- **evidence retention** (§8, area 2): a PASSED retention proof of exactly the live checks.
- **canary eligibility** (§5) and **visual acceptance** (§9): no owner exists yet — area 3 and a
  later eligibility record — so both stay unproven here, whatever else is recorded.
"""

from collections.abc import Callable

from app.live.model import MutationStage
from app.live.retention import RetentionProofService
from app.live.store import LiveAuthorityStore


class DurableStageProofs:
    def __init__(
        self,
        *,
        store: LiveAuthorityStore,
        retention: RetentionProofService,
        schema_head: Callable[[], str | None],
    ) -> None:
        self._store = store
        self._retention = retention
        self._head = schema_head

    def canary_non_regulated(self, stage: MutationStage, unit_ref: str) -> bool:
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
        return False
