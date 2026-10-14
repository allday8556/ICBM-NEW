"""Provider-zero contracts for Coupang Rocket Growth.

All seven official Rocket Growth endpoints are account-gated. This module retains their exact
method/path contracts and exposes an operator-safe blocked state, but it deliberately cannot build,
sign, or send a provider request. Program enrollment and the precise seller/account contract must
be proven in a later adoption slice. No HTTP, socket, credential, provider-call, or LIVE authority
is granted here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from integrations.marketplaces.coupang.auth import AuthContext, Market, Method
from integrations.marketplaces.coupang.fake_transport import FakeTransport

_SELLER_API_V1 = "/v2/providers/seller_api/apis/api/v1/marketplace"
_RG_OPEN_API_V1 = "/v2/providers/rg_open_api/apis/api/v1/vendors"


class CoupangRocketGrowthContractError(ValueError):
    """A local Rocket Growth contract request is invalid."""


class CoupangRocketGrowthAccountGate(PermissionError):
    """Rocket Growth was requested without proven program enrollment and entitlement."""


class RocketGrowthEndpoint(StrEnum):
    LIST_CATEGORIES = "LIST_CATEGORIES"
    MODIFY_PRODUCT = "MODIFY_PRODUCT"
    CREATE_PRODUCT = "CREATE_PRODUCT"
    LIST_PRODUCTS = "LIST_PRODUCTS"
    READ_PRODUCT = "READ_PRODUCT"
    LIST_INVENTORY = "LIST_INVENTORY"
    LIST_ORDERS = "LIST_ORDERS"


class RocketGrowthEffect(StrEnum):
    READ = "READ"
    MUTATION = "MUTATION"


@dataclass(frozen=True)
class RocketGrowthEndpointSpec:
    endpoint: RocketGrowthEndpoint
    method: Method
    path_template: str
    effect: RocketGrowthEffect
    account_gated: bool = True


ROCKET_GROWTH_ENDPOINTS: tuple[RocketGrowthEndpointSpec, ...] = (
    RocketGrowthEndpointSpec(
        RocketGrowthEndpoint.LIST_CATEGORIES,
        Method.GET,
        f"{_SELLER_API_V1}/meta/display-categories",
        RocketGrowthEffect.READ,
    ),
    RocketGrowthEndpointSpec(
        RocketGrowthEndpoint.MODIFY_PRODUCT,
        Method.PUT,
        f"{_SELLER_API_V1}/seller-products",
        RocketGrowthEffect.MUTATION,
    ),
    RocketGrowthEndpointSpec(
        RocketGrowthEndpoint.CREATE_PRODUCT,
        Method.POST,
        f"{_SELLER_API_V1}/seller-products",
        RocketGrowthEffect.MUTATION,
    ),
    RocketGrowthEndpointSpec(
        RocketGrowthEndpoint.LIST_PRODUCTS,
        Method.GET,
        f"{_SELLER_API_V1}/seller-products",
        RocketGrowthEffect.READ,
    ),
    RocketGrowthEndpointSpec(
        RocketGrowthEndpoint.READ_PRODUCT,
        Method.GET,
        f"{_SELLER_API_V1}/seller-products/{{sellerProductId}}",
        RocketGrowthEffect.READ,
    ),
    RocketGrowthEndpointSpec(
        RocketGrowthEndpoint.LIST_INVENTORY,
        Method.GET,
        f"{_RG_OPEN_API_V1}/{{vendorId}}/rg/inventory/summaries",
        RocketGrowthEffect.READ,
    ),
    RocketGrowthEndpointSpec(
        RocketGrowthEndpoint.LIST_ORDERS,
        Method.GET,
        f"{_RG_OPEN_API_V1}/{{vendorId}}/rg/orders",
        RocketGrowthEffect.READ,
    ),
)
_SPECS = {spec.endpoint: spec for spec in ROCKET_GROWTH_ENDPOINTS}


@dataclass(frozen=True)
class RocketGrowthBlockedState:
    """Stable, non-actionable state for operator surfaces while entitlement is unproven."""

    endpoint: RocketGrowthEndpoint
    code: str = "COUPANG_ROCKET_GROWTH_ACCOUNT_GATED"
    title: str = "Rocket Growth program access required"
    detail: str = (
        "Confirm Coupang Rocket Growth enrollment and the exact seller/account contract before "
        "enabling this endpoint. Credentials alone are not proof of entitlement."
    )
    required_evidence: tuple[str, ...] = (
        "rocket_growth_program_enrollment",
        "seller_account_contract",
    )
    recommendation_provenance: str = (
        "documents/contracts/platforms/coupang/ENDPOINT_INVENTORY.md#rocket-growth--7"
    )
    can_execute: bool = False


class CoupangRocketGrowthClient:
    """Seven fail-closed endpoint specifications with no request-building capability."""

    def __init__(self, context: AuthContext, transport: FakeTransport) -> None:
        if context.market is not Market.KR:
            raise CoupangRocketGrowthContractError(
                "Rocket Growth endpoints are documented for KR only"
            )
        self._context = context
        self._transport = transport

    def blocked_state(self, endpoint: RocketGrowthEndpoint) -> RocketGrowthBlockedState:
        if endpoint not in _SPECS:
            raise CoupangRocketGrowthContractError("unknown Rocket Growth endpoint")
        return RocketGrowthBlockedState(endpoint)

    def require_program_entitlement(self, endpoint: RocketGrowthEndpoint) -> NoReturn:
        state = self.blocked_state(endpoint)
        raise CoupangRocketGrowthAccountGate(
            f"{state.endpoint.value} requires proven Rocket Growth program enrollment and "
            "seller/account contract; no request was built or sent"
        )


__all__ = [
    "ROCKET_GROWTH_ENDPOINTS",
    "CoupangRocketGrowthAccountGate",
    "CoupangRocketGrowthClient",
    "CoupangRocketGrowthContractError",
    "RocketGrowthBlockedState",
    "RocketGrowthEffect",
    "RocketGrowthEndpoint",
    "RocketGrowthEndpointSpec",
]
