"""Supplier CONNECT API (Issue #7). A password goes in; it never comes back out."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, SecretStr

from app.api.deps import ContainerDep
from app.connect.contracts import StoredLoginView, SupplierConnectionSummary
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
