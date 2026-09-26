"""The pre-LIVE safety state, as 등록관리 shows it (ADR-0018 §9; Gate 3 area 3).

§9's populated acceptance exercises the registration surface's **protected-write state and grant
state**. This is their read-only projection from the owners themselves: the protected-write brake,
every grant of every account the registration surface holds, the ASSET readiness each ASSET grant
is judged by (§10: derived, read-only, never permission), and the two durable proofs that do not
depend on a unit — evidence retention (§8) and visual acceptance (§9).

It decides nothing and writes nothing. A readiness here is the same derivation the send-time stack
makes; showing it authorizes no mutation, and nothing here can record a proof.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from app.live.model import MutationStage
from app.live.stack import StageReadiness
from app.live.store import GrantRecord, LiveAuthorityStore
from app.register.store import RegistrationStore


class AssetReadiness(Protocol):
    def readiness(self, grant_id: str) -> StageReadiness: ...


def _time(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class LiveStatusService:
    def __init__(
        self,
        *,
        store: LiveAuthorityStore,
        registrations: RegistrationStore,
        assets: AssetReadiness,
        retention_ready: Callable[[], bool],
        visual_recorded: Callable[[], bool],
    ) -> None:
        self._store = store
        self._registrations = registrations
        self._assets = assets
        self._retention_ready = retention_ready
        self._visual_recorded = visual_recorded

    def status(self) -> dict[str, Any]:
        brake = self._store.brake()
        accounts = sorted(
            {(d.marketplace_key, d.marketplace_account_id) for d in self._registrations.drafts()}
        )
        grants: list[GrantRecord] = []
        with self._store.reading() as unit:
            for marketplace_key, account in accounts:
                for stage in MutationStage:
                    grants.extend(unit.grants(stage, marketplace_key, account))
        return {
            "brake": {
                "state": brake.state.value,
                # No row is the fail-closed default: ENGAGED, never an unknown or released brake.
                "recorded": brake.recorded,
                "generation": brake.generation,
                "reason_code": brake.reason_code,
                "changed_at": _time(brake.changed_at),
            },
            "grants": [self._grant(grant) for grant in grants],
            "proofs": {
                "evidence_retention_ready": self._retention_ready(),
                "visual_acceptance_recorded": self._visual_recorded(),
            },
        }

    def _grant(self, grant: GrantRecord) -> dict[str, Any]:
        view: dict[str, Any] = {
            "grant_id": grant.grant_id,
            "stage": grant.stage.value,
            "state": grant.state.value,
            "marketplace_key": grant.marketplace_key,
            "marketplace_account_id": grant.marketplace_account_id,
            "unit_ref": grant.preparation_revision_id or grant.intent_id,
            "budget_used": grant.budget_used,
            "budget_max": grant.budget_max,
            "not_before": _time(grant.not_before),
            "expires_at": _time(grant.expires_at),
            "authorization_ref": grant.authorization_ref,
            "readiness": None,
        }
        if grant.stage is MutationStage.ASSET:
            readiness = self._assets.readiness(grant.grant_id)
            view["readiness"] = {
                "verdict": readiness.verdict.value,
                "missing": list(readiness.missing),
            }
        return view


__all__ = ["LiveStatusService"]
