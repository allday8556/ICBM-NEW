"""The 해외 구매대행 settings routes (ADR-0038 §3, §5): Settings › 공통 › 해외구매대행.

- ``GET``/``PUT /api/v1/settings/overseas``: the settings card. A save names the revision it was
  edited from; a moved one is refused (``OVERSEAS_SETTINGS_REVISION_MOVED``), never merged.
- ``GET``/``PUT``/``DELETE /api/v1/settings/overseas/fx-key``: the operator's Korea Eximbank API
  key. Write-only: it goes to the OS secret store and is never returned; a read says only whether
  it is configured.
- ``GET /api/v1/settings/overseas/fx`` and ``/fx/{currency}``: the newest TTS snapshot of each
  currency with its FX-03 reason.
- ``POST /api/v1/settings/overseas/fx/refresh``: 「지금 갱신」, one audited read of Korea
  Eximbank's publication.

Every state-changing request carries the client header (the global CSRF guard), and the configured
operator is the audited actor. Nothing here prices anything (O2) or reaches a marketplace.
"""

from fastapi import APIRouter

from app.capabilities.overseas.fx import (
    FxKeyView,
    FxRateView,
    FxRefreshView,
    FxStatusView,
    SetFxKeyRequest,
)
from app.capabilities.overseas.settings import (
    OverseasSettingsView,
    SaveOverseasSettingsRequest,
)
from app.interface.api.deps import ContainerDep
from app.platform.core.correlation import get_correlation_id, new_correlation_id

router = APIRouter(prefix="/api/v1/settings/overseas", tags=["settings"])


def _cid() -> str:
    return get_correlation_id() or new_correlation_id()


@router.get("")
def overseas_settings(container: ContainerDep) -> OverseasSettingsView:
    """The current settings: the newest revision, or the defaults."""
    return container.overseas_settings.current()


@router.put("")
def save_overseas_settings(
    body: SaveOverseasSettingsRequest, container: ContainerDep
) -> OverseasSettingsView:
    """Append one audited revision, or change nothing."""
    return container.overseas_settings.save(
        body, actor=container.config.operator_actor, correlation_id=_cid()
    )


@router.get("/fx-key")
def fx_key(container: ContainerDep) -> FxKeyView:
    """Whether the Korea Eximbank key is configured — never the key."""
    return container.fx.key_status()


@router.put("/fx-key")
def set_fx_key(body: SetFxKeyRequest, container: ContainerDep) -> FxKeyView:
    """Write the key to the OS secret store (audited without it)."""
    return container.fx.set_key(body, actor=container.config.operator_actor, correlation_id=_cid())


@router.delete("/fx-key")
def delete_fx_key(container: ContainerDep) -> FxKeyView:
    """Remove the key from the OS secret store (audited)."""
    return container.fx.delete_key(actor=container.config.operator_actor, correlation_id=_cid())


@router.get("/fx")
def fx_rates(container: ContainerDep) -> FxStatusView:
    """The newest snapshot of each currency on today's KST day, and its FX-03 reason."""
    return container.fx.status()


@router.get("/fx/{currency}")
def fx_rate(currency: str, container: ContainerDep) -> FxRateView:
    """The newest snapshot of one currency; an unknown currency is 404."""
    return container.fx.rate(currency)


@router.post("/fx/refresh")
def refresh_fx(container: ContainerDep) -> FxRefreshView:
    """「지금 갱신」: read the publication now (audited)."""
    return container.fx.refresh(actor=container.config.operator_actor, correlation_id=_cid())
