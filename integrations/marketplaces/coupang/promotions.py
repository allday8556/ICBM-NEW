"""Provider-zero contracts for Coupang Promotions & Coupons.

The three common contract/budget reads execute only through ``FakeTransport``. The remaining
eighteen official cashback and coupon endpoints are retained as account-gated specifications and
fail closed before a request can be prepared or sent. This module grants no HTTP, socket,
credential, provider-call, entitlement, or LIVE authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NoReturn
from urllib.parse import urlencode

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Market,
    Method,
    RequestTarget,
)
from integrations.marketplaces.coupang.fake_transport import FakeRequest, FakeTransport

_FMS_V1 = "/v2/providers/fms/apis/api/v1/vendors"
_FMS_V2 = "/v2/providers/fms/apis/api/v2/vendors"
_OPENAPI_V4 = "/v2/providers/openapi/apis/api/v4/vendors"
_COUPONS = "/v2/providers/marketplace_openapi/apis/api/v1"
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class CoupangPromotionContractError(ValueError):
    """A local request or retained fake response violates the official contract."""


class CoupangPromotionAccountGate(PermissionError):
    """A contract-bound promotion caller was requested without proven entitlement."""


class PromotionEndpoint(StrEnum):
    LIST_CONTRACTS = "LIST_CONTRACTS"
    READ_CONTRACT = "READ_CONTRACT"
    LIST_BUDGETS = "LIST_BUDGETS"
    APPLY_BOOK_CASHBACK = "APPLY_BOOK_CASHBACK"
    READ_BOOK_CASHBACK = "READ_BOOK_CASHBACK"
    REMOVE_BOOK_CASHBACK = "REMOVE_BOOK_CASHBACK"
    READ_DOWNLOADABLE_COUPON_REQUEST = "READ_DOWNLOADABLE_COUPON_REQUEST"
    CREATE_DOWNLOADABLE_COUPON = "CREATE_DOWNLOADABLE_COUPON"
    EXPIRE_DOWNLOADABLE_COUPON = "EXPIRE_DOWNLOADABLE_COUPON"
    ADD_DOWNLOADABLE_COUPON_ITEM = "ADD_DOWNLOADABLE_COUPON_ITEM"
    READ_DOWNLOADABLE_COUPON = "READ_DOWNLOADABLE_COUPON"
    DISABLE_INSTANT_COUPON = "DISABLE_INSTANT_COUPON"
    READ_INSTANT_COUPON_REQUEST = "READ_INSTANT_COUPON_REQUEST"
    CREATE_INSTANT_COUPON = "CREATE_INSTANT_COUPON"
    ADD_INSTANT_COUPON_ITEM = "ADD_INSTANT_COUPON_ITEM"
    LIST_INSTANT_COUPONS_BY_ORDER = "LIST_INSTANT_COUPONS_BY_ORDER"
    LIST_INSTANT_COUPONS_BY_STATUS = "LIST_INSTANT_COUPONS_BY_STATUS"
    LIST_INSTANT_COUPON_ITEMS = "LIST_INSTANT_COUPON_ITEMS"
    READ_INSTANT_COUPON = "READ_INSTANT_COUPON"
    READ_INSTANT_COUPON_ITEM = "READ_INSTANT_COUPON_ITEM"
    READ_INSTANT_COUPON_ITEM_BY_VENDOR_ITEM = "READ_INSTANT_COUPON_ITEM_BY_VENDOR_ITEM"


@dataclass(frozen=True)
class PromotionEndpointSpec:
    endpoint: PromotionEndpoint
    method: Method
    path_template: str
    account_gated: bool


def _spec(
    endpoint: PromotionEndpoint, method: Method, path: str, *, account_gated: bool = True
) -> PromotionEndpointSpec:
    return PromotionEndpointSpec(endpoint, method, path, account_gated)


PROMOTION_ENDPOINTS: tuple[PromotionEndpointSpec, ...] = (
    _spec(
        PromotionEndpoint.LIST_CONTRACTS,
        Method.GET,
        f"{_FMS_V2}/{{vendorId}}/contract/list",
        account_gated=False,
    ),
    _spec(
        PromotionEndpoint.READ_CONTRACT,
        Method.GET,
        f"{_FMS_V2}/{{vendorId}}/contract",
        account_gated=False,
    ),
    _spec(
        PromotionEndpoint.LIST_BUDGETS,
        Method.GET,
        f"{_FMS_V1}/{{vendorId}}/budgets",
        account_gated=False,
    ),
    _spec(
        PromotionEndpoint.APPLY_BOOK_CASHBACK,
        Method.POST,
        f"{_OPENAPI_V4}/{{vendorId}}/products/items/cashback",
    ),
    _spec(
        PromotionEndpoint.READ_BOOK_CASHBACK,
        Method.GET,
        f"{_OPENAPI_V4}/{{vendorId}}/products/items/cashback",
    ),
    _spec(
        PromotionEndpoint.REMOVE_BOOK_CASHBACK,
        Method.DELETE,
        f"{_OPENAPI_V4}/{{vendorId}}/products/items/cashback",
    ),
    _spec(
        PromotionEndpoint.READ_DOWNLOADABLE_COUPON_REQUEST,
        Method.GET,
        f"{_COUPONS}/coupons/transactionStatus",
    ),
    _spec(PromotionEndpoint.CREATE_DOWNLOADABLE_COUPON, Method.POST, f"{_COUPONS}/coupons"),
    _spec(PromotionEndpoint.EXPIRE_DOWNLOADABLE_COUPON, Method.POST, f"{_COUPONS}/coupons/expire"),
    _spec(PromotionEndpoint.ADD_DOWNLOADABLE_COUPON_ITEM, Method.PUT, f"{_COUPONS}/coupon-items"),
    _spec(
        PromotionEndpoint.READ_DOWNLOADABLE_COUPON, Method.GET, f"{_COUPONS}/coupons/{{couponId}}"
    ),
    _spec(
        PromotionEndpoint.DISABLE_INSTANT_COUPON,
        Method.PUT,
        f"{_FMS_V1}/{{vendorId}}/coupons/{{couponId}}",
    ),
    _spec(
        PromotionEndpoint.READ_INSTANT_COUPON_REQUEST,
        Method.GET,
        f"{_FMS_V1}/{{vendorId}}/requested/{{requestedId}}",
    ),
    _spec(PromotionEndpoint.CREATE_INSTANT_COUPON, Method.POST, f"{_FMS_V2}/{{vendorId}}/coupon"),
    _spec(
        PromotionEndpoint.ADD_INSTANT_COUPON_ITEM,
        Method.POST,
        f"{_FMS_V1}/{{vendorId}}/coupons/{{couponId}}/items",
    ),
    _spec(
        PromotionEndpoint.LIST_INSTANT_COUPONS_BY_ORDER,
        Method.GET,
        f"{_FMS_V2}/{{vendorId}}/{{orderId}}/coupons",
    ),
    _spec(
        PromotionEndpoint.LIST_INSTANT_COUPONS_BY_STATUS,
        Method.GET,
        f"{_FMS_V2}/{{vendorId}}/coupons",
    ),
    _spec(
        PromotionEndpoint.LIST_INSTANT_COUPON_ITEMS,
        Method.GET,
        f"{_FMS_V1}/{{vendorId}}/coupons/{{couponId}}/items",
    ),
    _spec(PromotionEndpoint.READ_INSTANT_COUPON, Method.GET, f"{_FMS_V2}/{{vendorId}}/coupon"),
    _spec(
        PromotionEndpoint.READ_INSTANT_COUPON_ITEM,
        Method.GET,
        f"{_FMS_V1}/{{vendorId}}/coupons/{{couponId}}/items/{{couponItemId}}",
    ),
    _spec(
        PromotionEndpoint.READ_INSTANT_COUPON_ITEM_BY_VENDOR_ITEM,
        Method.GET,
        f"{_FMS_V1}/{{vendorId}}/coupons/{{couponId}}/items/{{vendorItemId}}",
    ),
)
_SPECS = {spec.endpoint: spec for spec in PROMOTION_ENDPOINTS}


@dataclass(frozen=True)
class FrozenEnvelope:
    body: bytes = field(repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FrozenEnvelope:
        try:
            body = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError) as exc:
            raise CoupangPromotionContractError(
                "promotion response must be canonical JSON"
            ) from exc
        return cls(body)

    def value(self) -> dict[str, object]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True)
class PromotionResult:
    endpoint: PromotionEndpoint
    envelope: FrozenEnvelope

    def data(self) -> object:
        return self.envelope.value().get("data")


def _contract_id(value: object, *, allow_free: bool) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise CoupangPromotionContractError("contractId must be an integer")
    if value > 0 or (allow_free and value == -1):
        return value
    qualifier = "a positive integer or -1" if allow_free else "a positive integer"
    raise CoupangPromotionContractError(f"contractId must be {qualifier}")


def _query(pairs: Sequence[tuple[str, object]]) -> str:
    return urlencode([(key, str(value)) for key, value in pairs if value is not None])


class CoupangPromotionClient:
    """Three common reads plus eighteen fail-closed account-gated specifications."""

    def __init__(
        self,
        authenticator: Authenticator,
        context: AuthContext,
        transport: FakeTransport,
    ) -> None:
        if context.market is not Market.KR:
            raise CoupangPromotionContractError("Promotion endpoints are documented for KR only")
        self._authenticator = authenticator
        self._context = context
        self._transport = transport

    def _vendor_path(self, version: str) -> str:
        root = _FMS_V1 if version == "v1" else _FMS_V2
        return f"{root}/{self._context.credentials.vendor_id}"

    def _read(self, endpoint: PromotionEndpoint, path: str, *, query: str = "") -> PromotionResult:
        target = RequestTarget(Method.GET, path, query)
        response = self._transport.send(
            FakeRequest(target, self._authenticator.headers(self._context, target), b"")
        )
        if response.status_code != 200 or not isinstance(response.body, dict):
            raise CoupangPromotionContractError(f"Coupang HTTP {response.status_code}")
        code = response.body.get("code")
        data = response.body.get("data")
        if code not in (200, "200", "SUCCESS") or (
            isinstance(data, dict) and data.get("success") is False
        ):
            raise CoupangPromotionContractError("Coupang promotion response was not successful")
        return PromotionResult(endpoint, FrozenEnvelope.from_mapping(response.body))

    def list_contracts(self) -> PromotionResult:
        return self._read(
            PromotionEndpoint.LIST_CONTRACTS,
            f"{self._vendor_path('v2')}/contract/list",
        )

    def read_contract(self, contract_id: int) -> PromotionResult:
        return self._read(
            PromotionEndpoint.READ_CONTRACT,
            f"{self._vendor_path('v2')}/contract",
            query=_query((("contractId", _contract_id(contract_id, allow_free=False)),)),
        )

    def list_budgets(
        self, *, contract_id: int | None = None, target_month: str | None = None
    ) -> PromotionResult:
        normalized_contract = (
            None if contract_id is None else _contract_id(contract_id, allow_free=True)
        )
        if target_month is not None and _MONTH.fullmatch(target_month) is None:
            raise CoupangPromotionContractError("targetMonth must use a valid YYYY-MM month")
        return self._read(
            PromotionEndpoint.LIST_BUDGETS,
            f"{self._vendor_path('v1')}/budgets",
            query=_query((("contractId", normalized_contract), ("targetMonth", target_month))),
        )

    def require_account_entitlement(self, endpoint: PromotionEndpoint) -> NoReturn:
        spec = _SPECS[endpoint]
        if not spec.account_gated:
            raise CoupangPromotionContractError(
                f"{endpoint.value} is a common read, not an account-gated endpoint"
            )
        raise CoupangPromotionAccountGate(
            f"{endpoint.value} requires proven Coupang contract entitlement; no request was sent"
        )


__all__ = [
    "PROMOTION_ENDPOINTS",
    "CoupangPromotionAccountGate",
    "CoupangPromotionClient",
    "CoupangPromotionContractError",
    "PromotionEndpoint",
    "PromotionEndpointSpec",
    "PromotionResult",
]
