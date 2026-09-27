"""The bounded real-canary readiness result (Issue #89 PR-F §C, ADR-0014 §24).

**Derived, read-only, and never permission to write.** It says whether every proof a single real
CREATE would need is in hand for one canonical account and one provider-listing unit. It stores
nothing, authorizes nothing and changes nothing: the execution mode stays `DRY_RUN`, and a real
write remains a separate, explicitly authorized bounded campaign.

A requirement is satisfied only by a proof that exists now. A missing endpoint contract is
reported as **not adopted**, never as "not needed" and never as "absent capability": the whole
point of this result is to name what is still unproven. The bounded image upload
(`SMARTSTORE_PRODUCT_IMAGE_UPLOAD`) is `ADOPTED` since the #96 amendment; CREATE
(`SMARTSTORE_PRODUCT_CREATE_V2`) and the deterministic product lookup
(`SMARTSTORE_PRODUCT_SEARCH`) stay `NOT_ADOPTED`, so the verdict stays `BLOCKED`.

**Adoption is not the whole proof of a seam**, and this result never lets one stand in for the
other (post-merge audit of main `a10e4b79dbd3`). The origin-product read-back is adopted (PR-D),
yet a real CREATE could still not be confirmed through it: production wires no committed session
to read with, and the adopted contract proves no published state, which ADR-0014 §11 compares
exactly and `app.register.execution` refuses to invent. Both are named as their own requirements,
so the enumeration covers every proof the result claims to cover.

Nothing here reaches a provider: the adoption facts arrive through a typed port that the adapter
fills in (`app.register.provider`), and every reason is a code — no gap prose, no URL, no payload.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel


class CanaryVerdict(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class CanaryRequirement(StrEnum):
    """Every proof a bounded real canary needs before it may be considered at all (§24)."""

    CLEAN_RUNTIME = "CLEAN_RUNTIME"
    ACCOUNT_BOUND = "ACCOUNT_BOUND"
    AUTH_READY = "AUTH_READY"
    WRITE_SCOPE_PROVEN = "WRITE_SCOPE_PROVEN"
    UNIT_PREPARED = "UNIT_PREPARED"
    CREATE_ADOPTED = "CREATE_ADOPTED"
    IMAGE_UPLOAD_ADOPTED = "IMAGE_UPLOAD_ADOPTED"
    RECONCILE_PATH_ADOPTED = "RECONCILE_PATH_ADOPTED"
    READBACK_ADOPTED = "READBACK_ADOPTED"
    # Adoption alone confirms nothing: the read-back must be executable and its comparison must
    # carry the published state ADR-0014 §11 compares (execution refuses to invent either).
    READBACK_EXECUTABLE = "READBACK_EXECUTABLE"
    PUBLISHED_STATE_PROVABLE = "PUBLISHED_STATE_PROVABLE"
    NO_UNRESOLVED_CONFLICT = "NO_UNRESOLVED_CONFLICT"
    SCOPE_SENDS_ALLOWED = "SCOPE_SENDS_ALLOWED"
    SINGLE_UNIT = "SINGLE_UNIT"


# Why a requirement is not satisfied. Codes only: a gap is named, never quoted.
NOT_ADOPTED = "ENDPOINT_NOT_ADOPTED"
UNPROVEN_IN_PROCESS = "PROOF_NOT_AVAILABLE_IN_PROCESS"
ACCOUNT_UNBOUND = "ACCOUNT_NOT_BOUND"
AUTH_NOT_READY = "AUTH_NOT_READY"
WRITE_SCOPE_MISSING = "WRITE_SCOPE_NOT_PROVEN"
UNIT_NOT_PREPARED = "NO_PREPARED_INTENT"
READBACK_NOT_EXECUTABLE = "READBACK_SESSION_NOT_WIRED"
PUBLISHED_STATE_UNPROVEN = "PUBLISHED_STATE_UNPROVEN"
CONFLICT_OPEN = "UNRESOLVED_CONFLICT"
SCOPE_STOPPED = "EXECUTION_SCOPE_STOPPED"
MORE_THAN_ONE_UNIT = "MORE_THAN_ONE_UNIT_SELECTED"
NO_UNIT = "NO_UNIT_SELECTED"


class AdoptionFacts(Protocol):
    """What the marketplace adapter proves about its own endpoint contracts (PR-D §17)."""

    def adoption(self) -> Mapping[str, bool]: ...

    def gaps(self) -> Mapping[str, str]: ...


@dataclass(frozen=True)
class UnitFacts:
    """The durable facts of the one unit a canary would send. All read, none decided here."""

    account_bound: bool
    auth_ready: bool
    write_scope_proven: bool
    intent_prepared: bool
    requires_image_upload: bool
    unresolved_conflicts: int
    sends_allowed: bool
    units_selected: int
    # The read-back seam's two facts beside adoption: a session to read with, and a comparison
    # that can carry a published state. Both fail closed, so an unwired caller proves neither.
    readback_executable: bool = False
    published_state_provable: bool = False


class RequirementView(BaseModel):
    requirement: CanaryRequirement
    satisfied: bool
    reason_code: str | None = None
    # The endpoint whose adoption is missing, when that is the reason. An identifier, never prose.
    endpoint_id: str | None = None


class CanaryReadinessView(BaseModel):
    """The derived plan result. `BLOCKED` names every missing proof; it authorizes nothing."""

    verdict: CanaryVerdict
    requirements: tuple[RequirementView, ...]
    missing: tuple[CanaryRequirement, ...]
    execution_mode: str
    write_status: str

    @property
    def ready(self) -> bool:
        return self.verdict is CanaryVerdict.READY


# The endpoint each adoption-backed requirement waits for.
ENDPOINTS: Mapping[CanaryRequirement, str] = {
    CanaryRequirement.CREATE_ADOPTED: "SMARTSTORE_PRODUCT_CREATE_V2",
    CanaryRequirement.IMAGE_UPLOAD_ADOPTED: "SMARTSTORE_PRODUCT_IMAGE_UPLOAD",
    CanaryRequirement.RECONCILE_PATH_ADOPTED: "SMARTSTORE_PRODUCT_SEARCH",
    CanaryRequirement.READBACK_ADOPTED: "SMARTSTORE_ORIGIN_PRODUCT_READ_V2",
}


def evaluate(
    facts: UnitFacts,
    adoption: Mapping[str, bool],
    *,
    execution_mode: str,
    write_status: str,
    clean_runtime: bool | None = None,
) -> CanaryReadinessView:
    """Judge one bounded canary plan. Every unmet requirement is reported, not the first.

    ``clean_runtime`` is ``None`` when the caller cannot prove it — a running application cannot
    prove its own checkout, so the requirement stays unsatisfied with the code
    `PROOF_NOT_AVAILABLE_IN_PROCESS` rather than being quietly assumed. The acceptance harness,
    which does prove its checkout, passes the answer in.
    """
    checks: list[RequirementView] = [
        _requirement(
            CanaryRequirement.CLEAN_RUNTIME,
            bool(clean_runtime),
            UNPROVEN_IN_PROCESS if clean_runtime is None else "RUNTIME_NOT_CLEAN",
        ),
        _requirement(CanaryRequirement.ACCOUNT_BOUND, facts.account_bound, ACCOUNT_UNBOUND),
        _requirement(CanaryRequirement.AUTH_READY, facts.auth_ready, AUTH_NOT_READY),
        _requirement(
            CanaryRequirement.WRITE_SCOPE_PROVEN, facts.write_scope_proven, WRITE_SCOPE_MISSING
        ),
        _requirement(CanaryRequirement.UNIT_PREPARED, facts.intent_prepared, UNIT_NOT_PREPARED),
        _adoption(CanaryRequirement.CREATE_ADOPTED, adoption),
        _adoption(CanaryRequirement.RECONCILE_PATH_ADOPTED, adoption),
        _adoption(CanaryRequirement.READBACK_ADOPTED, adoption),
        _requirement(
            CanaryRequirement.READBACK_EXECUTABLE,
            facts.readback_executable,
            READBACK_NOT_EXECUTABLE,
        ),
        _requirement(
            CanaryRequirement.PUBLISHED_STATE_PROVABLE,
            facts.published_state_provable,
            PUBLISHED_STATE_UNPROVEN,
        ),
        _requirement(
            CanaryRequirement.NO_UNRESOLVED_CONFLICT,
            facts.unresolved_conflicts == 0,
            CONFLICT_OPEN,
        ),
        _requirement(CanaryRequirement.SCOPE_SENDS_ALLOWED, facts.sends_allowed, SCOPE_STOPPED),
        # A canary is one exact provider-listing unit: none named is as unready as several.
        _requirement(
            CanaryRequirement.SINGLE_UNIT,
            facts.units_selected == 1,
            MORE_THAN_ONE_UNIT if facts.units_selected > 1 else NO_UNIT,
        ),
    ]
    if facts.requires_image_upload:
        # Only a unit that must publish a provider-hosted asset waits for the upload contract.
        checks.insert(6, _adoption(CanaryRequirement.IMAGE_UPLOAD_ADOPTED, adoption))
    missing = tuple(check.requirement for check in checks if not check.satisfied)
    return CanaryReadinessView(
        verdict=CanaryVerdict.BLOCKED if missing else CanaryVerdict.READY,
        requirements=tuple(checks),
        missing=missing,
        execution_mode=execution_mode,
        write_status=write_status,
    )


def _requirement(
    requirement: CanaryRequirement, satisfied: bool, reason_code: str
) -> RequirementView:
    return RequirementView(
        requirement=requirement,
        satisfied=satisfied,
        reason_code=None if satisfied else reason_code,
    )


def _adoption(requirement: CanaryRequirement, adoption: Mapping[str, bool]) -> RequirementView:
    """An endpoint contract is satisfied only when the adapter says it is adopted."""
    endpoint = ENDPOINTS[requirement]
    satisfied = bool(adoption.get(endpoint, False))
    return RequirementView(
        requirement=requirement,
        satisfied=satisfied,
        reason_code=None if satisfied else NOT_ADOPTED,
        endpoint_id=endpoint,
    )


def required_endpoints(requirements: Sequence[RequirementView]) -> tuple[str, ...]:
    """The endpoints a plan is still waiting on, for the evidence line."""
    return tuple(
        sorted(
            {
                check.endpoint_id
                for check in requirements
                if check.endpoint_id is not None and not check.satisfied
            }
        )
    )
