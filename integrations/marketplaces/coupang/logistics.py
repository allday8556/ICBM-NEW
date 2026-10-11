"""Provider-zero contracts for Coupang shipping and return locations.

The seven executable Logistics endpoints are prepared and executed only through ``FakeTransport``.
Courier and postal-code rules are local reference contracts; this module opens no socket and grants
no credential, provider-call, or LIVE authority.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import NewType
from urllib.parse import urlencode

from integrations.marketplaces.coupang.auth import (
    AuthContext,
    Authenticator,
    Market,
    Method,
    RequestTarget,
)
from integrations.marketplaces.coupang.fake_transport import FakeRequest, FakeTransport

ReturnCenterCode = NewType("ReturnCenterCode", str)
OutboundShippingPlaceCode = NewType("OutboundShippingPlaceCode", int)

_V5 = "/v2/providers/openapi/apis/api/v5/vendors"
_OUTBOUND_QUERY = "/v2/providers/marketplace_openapi/apis/api/v2/vendor/shipping-place/outbound"
_RETURN_CENTER_QUERY = "/v2/providers/openapi/apis/api/v3/return/shipping-places/center-code"
_ZIP = re.compile(r"^\d{5,6}$")
_RETURN_COURIERS = frozenset({"HYUNDAI", "KGB", "EPOST", "HANJIN", "CJGLS", "ILYANG"})
COURIER_REFERENCE_VERSION = "2026-10-12"
POSTAL_REFERENCE_VERSION = "2026-10-12"


class CoupangLogisticsContractError(ValueError):
    """A local request or retained fake response violates the official contract."""


class LogisticsEndpoint(StrEnum):
    CREATE_RETURN_LOCATION = "CREATE_RETURN_LOCATION"
    CREATE_SHIPPING_LOCATION = "CREATE_SHIPPING_LOCATION"
    LIST_RETURN_LOCATIONS = "LIST_RETURN_LOCATIONS"
    QUERY_SHIPPING_LOCATIONS = "QUERY_SHIPPING_LOCATIONS"
    READ_RETURN_LOCATIONS = "READ_RETURN_LOCATIONS"
    UPDATE_RETURN_LOCATION = "UPDATE_RETURN_LOCATION"
    UPDATE_SHIPPING_LOCATION = "UPDATE_SHIPPING_LOCATION"


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
            raise CoupangLogisticsContractError(
                "logistics response must be canonical JSON"
            ) from exc
        return cls(body)

    def value(self) -> dict[str, object]:
        value = json.loads(self.body)
        assert isinstance(value, dict)
        return value


@dataclass(frozen=True)
class LogisticsResult:
    endpoint: LogisticsEndpoint
    envelope: FrozenEnvelope

    def data(self) -> object:
        return self.envelope.value().get("data")


def _positive(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CoupangLogisticsContractError(f"{name} must be a positive integer")
    return value


def _text(value: object, name: str, *, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CoupangLogisticsContractError(f"{name} must be a non-empty trimmed string")
    if maximum is not None and len(value) > maximum:
        raise CoupangLogisticsContractError(f"{name} must be at most {maximum} characters")
    return value


def _return_code(value: object) -> str:
    return _text(value, "returnCenterCode")


def _addresses(
    values: Sequence[Mapping[str, object]], *, allow_oversea: bool
) -> list[dict[str, object]]:
    if not values:
        raise CoupangLogisticsContractError("placeAddresses must not be empty")
    normalized: list[dict[str, object]] = []
    for value in values:
        address = dict(value)
        address_type = _text(address.get("addressType"), "addressType")
        allowed_types = {"JIBUN", "ROADNAME", "OVERSEA"} if allow_oversea else {"JIBUN", "ROADNAME"}
        if address_type not in allowed_types:
            allowed = ", ".join(sorted(allowed_types))
            raise CoupangLogisticsContractError(f"addressType must be one of: {allowed}")
        _text(address.get("companyContactNumber"), "companyContactNumber")
        if address.get("phoneNumber2") is not None:
            _text(address["phoneNumber2"], "phoneNumber2")
        zip_code = _text(address.get("returnZipCode"), "returnZipCode")
        if _ZIP.fullmatch(zip_code) is None:
            raise CoupangLogisticsContractError("returnZipCode must contain 5..6 digits")
        _text(address.get("returnAddress"), "returnAddress", maximum=150)
        _text(address.get("returnAddressDetail"), "returnAddressDetail", maximum=200)
        country = address.get("countryCode")
        if country is not None and len(_text(country, "countryCode")) != 2:
            raise CoupangLogisticsContractError("countryCode must contain two characters")
        normalized.append(address)
    return normalized


def _goodsflow(value: Mapping[str, object]) -> dict[str, object]:
    result = dict(value)
    courier = _text(result.get("deliverCode"), "deliverCode")
    if courier not in _RETURN_COURIERS:
        raise CoupangLogisticsContractError("deliverCode is not supported for return locations")
    _text(result.get("deliverName"), "deliverName")
    _text(result.get("contractNumber"), "contractNumber")
    if courier == "EPOST":
        _text(result.get("contractCustomerNumber"), "contractCustomerNumber")
    for name in (
        "vendorCreditFee05kg",
        "consumerCashFee10kg",
        "consumerCashFee20kg",
        "returnFee05kg",
        "returnFee10kg",
        "returnFee20kg",
    ):
        _positive(result.get(name), name)
    return result


def _query(pairs: Sequence[tuple[str, object]]) -> str:
    return urlencode([(key, str(value)) for key, value in pairs if value is not None])


class CoupangLogisticsClient:
    """Seven official Logistics endpoints over ``FakeTransport`` only."""

    def __init__(
        self,
        authenticator: Authenticator,
        context: AuthContext,
        transport: FakeTransport,
    ) -> None:
        if context.market not in {Market.KR, Market.TW}:
            raise CoupangLogisticsContractError("unknown buyer market")
        self._authenticator = authenticator
        self._context = context
        self._transport = transport

    def _call(
        self,
        endpoint: LogisticsEndpoint,
        method: Method,
        path: str,
        *,
        query: str = "",
        document: Mapping[str, object] | None = None,
    ) -> LogisticsResult:
        target = RequestTarget(method, path, query)
        body = (
            b""
            if document is None
            else json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        response = self._transport.send(
            FakeRequest(target, self._authenticator.headers(self._context, target), body)
        )
        if response.status_code != 200 or not isinstance(response.body, dict):
            raise CoupangLogisticsContractError(f"Coupang HTTP {response.status_code}")
        code = response.body.get("code")
        if code is not None and code not in (200, "200", "SUCCESS"):
            raise CoupangLogisticsContractError("Coupang logistics response was not successful")
        return LogisticsResult(endpoint, FrozenEnvelope.from_mapping(response.body))

    def _vendor_path(self) -> str:
        return f"{_V5}/{self._context.credentials.vendor_id}"

    def create_return_location(
        self,
        *,
        user_id: str,
        shipping_place_name: str,
        goodsflow_info: Mapping[str, object],
        place_addresses: Sequence[Mapping[str, object]],
    ) -> LogisticsResult:
        return self._call(
            LogisticsEndpoint.CREATE_RETURN_LOCATION,
            Method.POST,
            f"{self._vendor_path()}/returnShippingCenters",
            document={
                "vendorId": self._context.credentials.vendor_id,
                "userId": _text(user_id, "userId"),
                "shippingPlaceName": _text(shipping_place_name, "shippingPlaceName"),
                "goodsflowInfoOpenApiDto": _goodsflow(goodsflow_info),
                "placeAddresses": _addresses(place_addresses, allow_oversea=False),
            },
        )

    def create_shipping_location(
        self,
        *,
        user_id: str,
        shipping_place_name: str,
        place_addresses: Sequence[Mapping[str, object]],
        usable: bool = True,
        global_: bool = False,
        remote_infos: Sequence[Mapping[str, object]] = (),
    ) -> LogisticsResult:
        if not isinstance(usable, bool) or not isinstance(global_, bool):
            raise CoupangLogisticsContractError("usable and global must be boolean")
        return self._call(
            LogisticsEndpoint.CREATE_SHIPPING_LOCATION,
            Method.POST,
            f"{self._vendor_path()}/outboundShippingCenters",
            document={
                "vendorId": self._context.credentials.vendor_id,
                "userId": _text(user_id, "userId"),
                "shippingPlaceName": _text(shipping_place_name, "shippingPlaceName", maximum=50),
                "usable": usable,
                "global": global_,
                "placeAddresses": _addresses(place_addresses, allow_oversea=True),
                "remoteInfos": [dict(value) for value in remote_infos],
            },
        )

    def list_return_locations(self, *, page_num: int = 1, page_size: int = 10) -> LogisticsResult:
        if not 1 <= page_size <= 50:
            raise CoupangLogisticsContractError("pageSize must be 1..50")
        return self._call(
            LogisticsEndpoint.LIST_RETURN_LOCATIONS,
            Method.GET,
            f"{self._vendor_path()}/returnShippingCenters",
            query=_query((("pageNum", _positive(page_num, "pageNum")), ("pageSize", page_size))),
        )

    def query_shipping_locations(
        self,
        *,
        page_num: int | None = 1,
        page_size: int = 10,
        place_codes: Sequence[OutboundShippingPlaceCode] = (),
        place_names: Sequence[str] = (),
    ) -> LogisticsResult:
        filtered = bool(place_codes or place_names)
        if filtered and page_num is not None:
            raise CoupangLogisticsContractError("filtered shipping queries exclude pageNum")
        if not 1 <= page_size <= 50:
            raise CoupangLogisticsContractError("pageSize must be 1..50")
        codes = ",".join(str(_positive(value, "placeCode")) for value in place_codes) or None
        names = ",".join(_text(value, "placeName") for value in place_names) or None
        return self._call(
            LogisticsEndpoint.QUERY_SHIPPING_LOCATIONS,
            Method.GET,
            _OUTBOUND_QUERY,
            query=_query(
                (
                    ("placeCodes", codes),
                    ("placeNames", names),
                    ("pageNum", page_num),
                    ("pageSize", page_size),
                )
            ),
        )

    def read_return_locations(
        self, return_center_codes: Sequence[ReturnCenterCode]
    ) -> LogisticsResult:
        if not 1 <= len(return_center_codes) <= 100:
            raise CoupangLogisticsContractError("returnCenterCodes must contain 1..100 codes")
        codes = [_return_code(value) for value in return_center_codes]
        if len(set(codes)) != len(codes):
            raise CoupangLogisticsContractError("returnCenterCodes must be unique")
        return self._call(
            LogisticsEndpoint.READ_RETURN_LOCATIONS,
            Method.GET,
            _RETURN_CENTER_QUERY,
            query=_query((("returnCenterCodes", ",".join(codes)),)),
        )

    def update_return_location(
        self,
        return_center_code: ReturnCenterCode,
        *,
        user_id: str,
        place_addresses: Sequence[Mapping[str, object]],
        shipping_place_name: str | None = None,
        usable: bool | None = None,
        goodsflow_info: Mapping[str, object] | None = None,
    ) -> LogisticsResult:
        code = _return_code(return_center_code)
        document: dict[str, object] = {
            "vendorId": self._context.credentials.vendor_id,
            "returnCenterCode": code,
            "userId": _text(user_id, "userId"),
            "placeAddresses": _addresses(place_addresses, allow_oversea=False),
        }
        if shipping_place_name is not None:
            document["shippingPlaceName"] = _text(shipping_place_name, "shippingPlaceName")
        if usable is not None:
            if not isinstance(usable, bool):
                raise CoupangLogisticsContractError("usable must be boolean")
            document["usable"] = usable
        if goodsflow_info is not None:
            document["goodsflowInfoOpenApiDto"] = _goodsflow(goodsflow_info)
        return self._call(
            LogisticsEndpoint.UPDATE_RETURN_LOCATION,
            Method.PUT,
            f"{self._vendor_path()}/returnShippingCenters/{code}",
            document=document,
        )

    def update_shipping_location(
        self,
        outbound_shipping_place_code: OutboundShippingPlaceCode,
        *,
        user_id: str,
        place_addresses: Sequence[Mapping[str, object]],
        shipping_place_name: str | None = None,
        usable: bool | None = None,
        global_: bool | None = None,
        remote_infos: Sequence[Mapping[str, object]] = (),
    ) -> LogisticsResult:
        code = _positive(outbound_shipping_place_code, "outboundShippingPlaceCode")
        document: dict[str, object] = {
            "vendorId": self._context.credentials.vendor_id,
            "userId": _text(user_id, "userId"),
            "outboundShippingPlaceCode": code,
            "placeAddresses": _addresses(place_addresses, allow_oversea=True),
            "remoteInfos": [dict(value) for value in remote_infos],
        }
        if shipping_place_name is not None:
            document["shippingPlaceName"] = _text(
                shipping_place_name, "shippingPlaceName", maximum=50
            )
        for name, value in (("usable", usable), ("global", global_)):
            if value is not None:
                if not isinstance(value, bool):
                    raise CoupangLogisticsContractError(f"{name} must be boolean")
                document[name] = value
        return self._call(
            LogisticsEndpoint.UPDATE_SHIPPING_LOCATION,
            Method.PUT,
            f"{self._vendor_path()}/outboundShippingCenters/{code}",
            document=document,
        )


__all__ = [
    "COURIER_REFERENCE_VERSION",
    "POSTAL_REFERENCE_VERSION",
    "CoupangLogisticsClient",
    "CoupangLogisticsContractError",
    "FrozenEnvelope",
    "LogisticsEndpoint",
    "LogisticsResult",
    "OutboundShippingPlaceCode",
    "ReturnCenterCode",
]
