"""The adopted SmartStore order dispatch (M6.5-C; ADR-0025 §5.1).

``POST /v1/pay-order/seller/product-orders/dispatch`` with one ``dispatchProductOrders`` element.
The caller decides the remote outcome from the request's own transmission evidence:
``NOT_APPLIED_PROVEN`` only for the transmission-precluded whitelist, ``UNKNOWN`` for everything
else — a timeout, a lost response, a 4xx or 5xx after the handoff, a redirect, a 200 that is not
the documented answer. A documented 200 is handed on with how it listed this product order; the
OPERATE owner decides from that list alone. Nothing is ever resent here.
"""

from datetime import datetime
from typing import Any

from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.operate.dispatch import DispatchHandoff
from integrations.marketplaces.smartstore.caller import (
    OrderDispatchRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.execution import BearerSource
from integrations.marketplaces.smartstore.orders import kst
from integrations.marketplaces.smartstore.registry import ADOPTED, EndpointId
from integrations.marketplaces.smartstore.transmission import Phase

_ENDPOINT = EndpointId.SMARTSTORE_ORDER_DISPATCH


class SmartStoreDispatchSender:
    def __init__(self, caller: SmartStoreEndpointCaller, bearer: BearerSource) -> None:
        self._caller = caller
        self._bearer = bearer

    def endpoint_adopted(self) -> bool:
        """The adoption fact only — the stack's endpoint-adoption layer."""
        return _ENDPOINT in ADOPTED

    def send(
        self,
        *,
        product_order_id: str,
        carrier_code: str,
        tracking_number: str,
        dispatch_date: datetime,
    ) -> DispatchHandoff:
        bearer = self._bearer()
        if bearer is None:
            return _local_refusal("SMARTSTORE_SESSION_UNAVAILABLE")
        try:
            response = self._caller.call(
                EndpointId.SMARTSTORE_ORDER_DISPATCH,
                OrderDispatchRequest(
                    bearer.access_token,
                    bearer.credential_generation,
                    bearer.session_generation,
                    product_order_id,
                    carrier_code,
                    tracking_number,
                    kst(dispatch_date),
                ),
            )
        except SmartStoreCallError as failure:
            return DispatchHandoff(
                remote_outcome=failure.remote_outcome,
                response_status=failure.http_status,
                error_code=failure.code,
                details={
                    "endpoint_id": _ENDPOINT.value,
                    "transmission_phase": failure.phase.value,
                    "failure_layer": failure.classification.layer.value,
                    "provider_code": failure.classification.provider_code,
                },
            )
        # The documented answer arrived: the OPERATE owner reads the listing, never this status.
        return DispatchHandoff(
            remote_outcome=RemoteOutcome.APPLIED_PROVEN,
            listed=response.listed,
            fail_code=response.fail_code,
            response_status=response.http_status,
            details={"endpoint_id": _ENDPOINT.value},
        )


def _local_refusal(code: str, **details: Any) -> DispatchHandoff:
    """Refused before any transport existed: ``LOCAL_PREFLIGHT`` is transmission-precluded."""
    return DispatchHandoff(
        remote_outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
        error_code=code,
        details={
            "endpoint_id": _ENDPOINT.value,
            "transmission_phase": Phase.LOCAL_PREFLIGHT.value,
            **details,
        },
    )


__all__ = ["SmartStoreDispatchSender"]
