"""Provider-zero contract for Coupang CGF Lite order-label retrieval.

The single indexed CGF Lite endpoint is account-gated for KR and TW. This module retains its
exact method/path contract and exposes an operator-safe blocked state, but deliberately cannot
build, sign, or send a provider request. Program enrollment and the precise seller/account
contract must be proven in a later adoption slice. No HTTP, socket, credential, provider-call, or
LIVE authority is granted here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from integrations.marketplaces.coupang.auth import AuthContext, Market, Method
from integrations.marketplaces.coupang.fake_transport import FakeTransport

_OPEN_API_V1 = "/v2/providers/openapi/apis/api/v1/vendors"


class CoupangCgfContractError(ValueError):
    """A local CGF Lite contract request is invalid."""


class CoupangCgfAccountGate(PermissionError):
    """CGF Lite was requested without proven program enrollment and entitlement."""


class CgfEndpoint(StrEnum):
    PRINT_ORDER_LABEL = "PRINT_ORDER_LABEL"


class CgfEffect(StrEnum):
    DOCUMENT_READ = "DOCUMENT_READ"


@dataclass(frozen=True)
class CgfEndpointSpec:
    endpoint: CgfEndpoint
    method: Method
    path_template: str
    effect: CgfEffect
    markets: tuple[Market, ...] = (Market.KR, Market.TW)
    account_gated: bool = True


CGF_ENDPOINTS: tuple[CgfEndpointSpec, ...] = (
    CgfEndpointSpec(
        CgfEndpoint.PRINT_ORDER_LABEL,
        Method.POST,
        f"{_OPEN_API_V1}/{{vendorId}}/label/printOrderLabel",
        CgfEffect.DOCUMENT_READ,
    ),
)
_SPECS = {spec.endpoint: spec for spec in CGF_ENDPOINTS}


@dataclass(frozen=True)
class CgfBlockedState:
    """Stable, non-actionable state for operator surfaces while entitlement is unproven."""

    endpoint: CgfEndpoint
    code: str = "COUPANG_CGF_LITE_ACCOUNT_GATED"
    title: str = "CGF Lite program access required"
    detail: str = (
        "Confirm Coupang CGF Lite enrollment and the exact seller/account contract before "
        "enabling order-label retrieval. Credentials alone are not proof of entitlement."
    )
    required_evidence: tuple[str, ...] = (
        "cgf_lite_program_enrollment",
        "seller_account_contract",
    )
    recommendation_provenance: str = (
        "documents/contracts/platforms/coupang/ENDPOINT_INVENTORY.md#cgf--cgf-lite--1"
    )
    can_execute: bool = False


class CoupangCgfClient:
    """One fail-closed endpoint specification with no request-building capability."""

    def __init__(self, context: AuthContext, transport: FakeTransport) -> None:
        spec = _SPECS[CgfEndpoint.PRINT_ORDER_LABEL]
        if context.market not in spec.markets:
            raise CoupangCgfContractError("CGF Lite order labels are indexed for KR and TW only")
        self._context = context
        self._transport = transport

    def blocked_state(self, endpoint: CgfEndpoint) -> CgfBlockedState:
        if endpoint not in _SPECS:
            raise CoupangCgfContractError("unknown CGF Lite endpoint")
        return CgfBlockedState(endpoint)

    def require_program_entitlement(self, endpoint: CgfEndpoint) -> NoReturn:
        state = self.blocked_state(endpoint)
        raise CoupangCgfAccountGate(
            f"{state.endpoint.value} requires proven CGF Lite program enrollment and "
            "seller/account contract; no request was built or sent"
        )


__all__ = [
    "CGF_ENDPOINTS",
    "CgfBlockedState",
    "CgfEffect",
    "CgfEndpoint",
    "CgfEndpointSpec",
    "CoupangCgfAccountGate",
    "CoupangCgfClient",
    "CoupangCgfContractError",
]
