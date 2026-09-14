"""CONNECT API. Supplier (Issue #7): a password goes in; it never comes back out. Marketplace
capability (M2 PR-B): every axis as its own field; the operator records reviewed contract
freshness. SmartStore operator actions (M2 PR-E, instructions §8A): credential replacement,
account observation, the explicit first binding and workflow resolution — each a thin typed
route over the owning service, behind the loopback binding and the client-header CSRF guard, with
the configured operator as the audited actor."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, SecretStr

from app.api.deps import ContainerDep
from app.connect.contracts import StoredLoginView, SupplierConnectionSummary
from app.connect.marketplace.attestation import ApiGroup
from app.connect.marketplace.attestation_contracts import PermissionAttestationView
from app.connect.marketplace.capability import ContractFreshness, Resolution, WorkflowScope
from app.connect.marketplace.contracts import MarketplaceCapabilityView
from app.connect.smartstore.service import ConnectResult
from app.jobs.records import JobRecord

router = APIRouter(tags=["connect"])


class CredentialsRequest(BaseModel):
    # Validation errors must never echo what the operator typed.
    model_config = ConfigDict(hide_input_in_errors=True)

    username: str
    # Omitted: keep the stored password. The form shows a stored password only as a masked
    # state and sends a password only after the operator chooses to change it.
    password: SecretStr | None = None


class AutoConnectRequest(BaseModel):
    enabled: bool


class ContractFreshnessRequest(BaseModel):
    contract_freshness: ContractFreshness


@router.get("/api/v1/connect/suppliers")
def list_suppliers(container: ContainerDep) -> list[SupplierConnectionSummary]:
    return container.connect.supplier_connections()


@router.get("/api/v1/connect/suppliers/{supplier_key}/credentials")
def stored_login(supplier_key: str, container: ContainerDep) -> StoredLoginView:
    """The saved login ID and whether a password is stored — never the password itself."""
    return container.connect.stored_login(supplier_key)


@router.put("/api/v1/connect/suppliers/{supplier_key}/credentials")
def save_credentials(
    supplier_key: str, body: CredentialsRequest, container: ContainerDep
) -> SupplierConnectionSummary:
    """Store the login in the OS secret store. The response reports only that it is stored."""
    return container.connect.save_credentials(
        supplier_key,
        username=body.username,
        password=body.password.get_secret_value() if body.password is not None else None,
        actor=container.config.operator_actor,
    )


@router.post("/api/v1/connect/suppliers/{supplier_key}/test", status_code=202)
def request_connection_test(supplier_key: str, container: ContainerDep) -> JobRecord:
    """Queue the protected-read proof (session reuse first, a login only if needed)."""
    return container.connect.request_connection_test(
        supplier_key, actor=container.config.operator_actor
    )


@router.post("/api/v1/connect/suppliers/{supplier_key}/resume")
def resume_authentication(supplier_key: str, container: ContainerDep) -> SupplierConnectionSummary:
    """Audited operator intervention: the only way out of PAUSED."""
    return container.connect.resume(supplier_key, actor=container.config.operator_actor)


@router.put("/api/v1/connect/suppliers/{supplier_key}/auto-connect")
def set_auto_connect(
    supplier_key: str, body: AutoConnectRequest, container: ContainerDep
) -> SupplierConnectionSummary:
    return container.connect.set_auto_connect(
        supplier_key, enabled=body.enabled, actor=container.config.operator_actor
    )


@router.get("/api/v1/connect/marketplaces/capabilities")
def marketplace_capabilities(container: ContainerDep) -> list[MarketplaceCapabilityView]:
    """Capability truth of every marketplace with an adopted capability contract."""
    return container.marketplace_capability.capabilities()


@router.get("/api/v1/connect/marketplaces/{marketplace_key}/capability")
def marketplace_capability(
    marketplace_key: str, container: ContainerDep
) -> MarketplaceCapabilityView:
    return container.marketplace_capability.capability(marketplace_key)


class PermissionAttestationRequest(BaseModel):
    # Only what the operator saw; ICBM supplies the time, application, requirement and revision.
    observed_groups: list[ApiGroup]


@router.get("/api/v1/connect/marketplaces/{marketplace_key}/permission-attestation")
def permission_attestation(
    marketplace_key: str, container: ContainerDep
) -> PermissionAttestationView:
    """SMARTSTORE-A0-PERMISSION: the recorded evidence, its current evaluation, and what it did
    to capability truth (applied, not current, or waiting on the contract freshness)."""
    return container.permission_attestation.attestation(marketplace_key)


@router.post("/api/v1/connect/marketplaces/{marketplace_key}/permission-attestation")
def record_permission_attestation(
    marketplace_key: str, body: PermissionAttestationRequest, container: ContainerDep
) -> PermissionAttestationView:
    """The local operator records which API groups Commerce API Center shows. It is operator-
    attested evidence, never provider measurement, and it makes no SmartStore call."""
    return container.permission_attestation.attest(
        marketplace_key, body.observed_groups, actor=container.config.operator_actor
    )


@router.post("/api/v1/connect/marketplaces/{marketplace_key}/contract-freshness")
def record_contract_freshness(
    marketplace_key: str, body: ContractFreshnessRequest, container: ContainerDep
) -> MarketplaceCapabilityView:
    """The local operator records a reviewed contract-freshness determination (CAPABILITY_MAPPING
    F8). It states that ICBM's adopted-contract review was completed — it is not provider
    evidence — and it makes no SmartStore call. Every recording, a same-value one included, is
    persisted with ``freshness_recorded_at`` and audited with the operator as actor (F7)."""
    return container.marketplace_capability.record_contract_freshness(
        marketplace_key, body.contract_freshness, actor=container.config.operator_actor
    )


class WorkflowResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_scope: WorkflowScope
    resolution: Resolution


@router.post("/api/v1/connect/marketplaces/{marketplace_key}/workflow-resolution")
def resolve_workflow(
    marketplace_key: str, body: WorkflowResolutionRequest, container: ContainerDep
) -> MarketplaceCapabilityView:
    """Explicit, audited operator resolution of one overlay (CAPABILITY_MAPPING §9, §10). Only the
    resolution the overlay's view names is accepted, and it never makes anything READY."""
    return container.marketplace_capability.resolve(
        marketplace_key,
        body.workflow_scope,
        body.resolution,
        actor=container.config.operator_actor,
    )


# ---------------------------------------------------------------- SmartStore operator actions

SMARTSTORE = "/api/v1/connect/marketplaces/smartstore"


class SmartStoreCredentialsRequest(BaseModel):
    # A whole replacement bundle. Validation errors never echo what was typed, and nothing else
    # (a store id, an account override) is accepted.
    model_config = ConfigDict(hide_input_in_errors=True, extra="forbid")

    client_id: str
    client_secret: SecretStr


class SmartStoreCredentialsView(BaseModel):
    """Whether a credential bundle is committed, and under which generation — never its content."""

    configured: bool
    credential_generation: int | None


class SmartStoreBindRequest(BaseModel):
    # Only the identity the operator was shown and confirmed; no override of any kind.
    model_config = ConfigDict(extra="forbid")

    confirmed_account_uid: str


class SmartStoreObservationView(BaseModel):
    """One CONNECT pass as the operator's own screen needs it. ``bound`` stays false until the
    explicit first binding: observing an account never binds it (ACCOUNT_IDENTITY §5)."""

    bound: bool
    observed_account_uid: str
    observed_account_id: str | None
    capability: MarketplaceCapabilityView

    @classmethod
    def of(cls, result: ConnectResult) -> "SmartStoreObservationView":
        return cls(
            bound=result.bound,
            observed_account_uid=result.observed_account_uid,
            observed_account_id=result.observed_account_id,
            capability=result.capability,
        )


@router.get(f"{SMARTSTORE}/credentials")
def smartstore_credentials(container: ContainerDep) -> SmartStoreCredentialsView:
    configured, generation = container.smartstore.credential_status()
    return SmartStoreCredentialsView(configured=configured, credential_generation=generation)


@router.put(f"{SMARTSTORE}/credentials")
def save_smartstore_credentials(
    body: SmartStoreCredentialsRequest, container: ContainerDep
) -> SmartStoreCredentialsView:
    """Commit a replacement credential bundle as a new credential generation (AUTH §18). The
    secret goes to the OS secret store and never comes back out."""
    generation = container.smartstore.save_credentials(
        body.client_id,
        body.client_secret.get_secret_value(),
        actor=container.config.operator_actor,
    )
    return SmartStoreCredentialsView(configured=True, credential_generation=generation)


@router.post(f"{SMARTSTORE}/connect")
def observe_smartstore_account(container: ContainerDep) -> SmartStoreObservationView:
    """One CONNECT pass: token, durable session commit, seller-account read, capability evidence.
    It returns the observed account to the operator's screen and never binds a first account."""
    return SmartStoreObservationView.of(container.smartstore.connect())


@router.post(f"{SMARTSTORE}/bind")
def bind_smartstore_account(
    body: SmartStoreBindRequest, container: ContainerDep
) -> SmartStoreObservationView:
    """The explicit first binding of the account the operator confirmed. The service reads the
    account again, refuses a different one, and commits the binding only under the freshness
    decision that authorizes it; a failure leaves nothing bound (PR-A)."""
    return SmartStoreObservationView.of(
        container.smartstore.bind_account(
            body.confirmed_account_uid, actor=container.config.operator_actor
        )
    )
