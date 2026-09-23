"""Settings write paths for the registration target policy (Gate 1 G1-A; ADR-0015 §2) and the
operator-reviewed category metadata (Gate 1 G1-B; ADR-0015 §3).

These are the only Settings surfaces with a save contract. The server validates the whole
revision, creates its identity, fingerprint, review provenance and audit record, and makes it
current; a route never computes any of that. Every other Settings field stays read-only because no
save contract exists for it.

No route here reaches a provider or a marketplace: both are local, operator-recorded truth.
"""

from fastapi import APIRouter

from app.api.deps import ContainerDep
from app.core.correlation import get_correlation_id, new_correlation_id
from app.register.category_metadata import (
    CategoryMetadataListView,
    CategoryMetadataView,
    RecordRevisionRequest,
)
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


@router.get("/category-metadata/{marketplace_key}")
def category_metadata_entries(
    container: ContainerDep, marketplace_key: str
) -> CategoryMetadataListView:
    """Every recorded category of the marketplace with its current revision and history."""
    return container.category_metadata.entries(marketplace_key)


@router.get("/category-metadata/{marketplace_key}/{taxonomy_revision}/{category_id}")
def category_metadata(
    container: ContainerDep, marketplace_key: str, taxonomy_revision: str, category_id: str
) -> CategoryMetadataView:
    return container.category_metadata.metadata(marketplace_key, taxonomy_revision, category_id)


@router.post("/category-metadata/{marketplace_key}/{taxonomy_revision}/{category_id}/revisions")
def record_category_metadata(
    container: ContainerDep,
    marketplace_key: str,
    taxonomy_revision: str,
    category_id: str,
    request: RecordRevisionRequest,
) -> CategoryMetadataView:
    """Record one server-created revision and make it current, or change nothing."""
    return container.category_metadata.record(
        marketplace_key,
        taxonomy_revision,
        category_id,
        request,
        correlation_id=get_correlation_id() or new_correlation_id(),
    )
