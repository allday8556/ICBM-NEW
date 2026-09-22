"""Settings write path for the registration target policy (Gate 1 G1-A; ADR-0015 §2).

This is the only Settings surface with a save contract. The server validates the whole revision,
creates its identity, fingerprint and audit record, and makes it current; a route never computes
any of that. Every other Settings field stays read-only because no save contract exists for it.

No route here reaches a provider or a marketplace: a target policy is local configuration.
"""

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.core.correlation import get_correlation_id, new_correlation_id
from app.register.target_policy import (
    AppendRevisionRequest,
    TargetPolicyAccountsView,
    TargetPolicyView,
)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


@router.get("/target-policies/{marketplace_key}")
def target_policies(container: ContainerDep, marketplace_key: str) -> TargetPolicyAccountsView:
    """Every canonical account of the marketplace with its current target policy, if any."""
    return container.target_policies.accounts(marketplace_key)


@router.get("/target-policies/{marketplace_key}/{marketplace_account_id}")
def target_policy(
    container: ContainerDep, marketplace_key: str, marketplace_account_id: str
) -> TargetPolicyView:
    return container.target_policies.policy(marketplace_key, marketplace_account_id)


@router.post("/target-policies/{marketplace_key}/{marketplace_account_id}/revisions")
def append_revision(
    container: ContainerDep,
    marketplace_key: str,
    marketplace_account_id: str,
    request: AppendRevisionRequest,
) -> TargetPolicyView:
    """Append one server-created revision and make it current, or change nothing."""
    return container.target_policies.append(
        marketplace_key,
        marketplace_account_id,
        request,
        correlation_id=get_correlation_id() or new_correlation_id(),
    )
