"""The adopted SmartStore origin-product DELETE (ADR-0018 §3.5).

``DELETE /v2/products/origin-products/{originProductNo}``, one product per call (the provider
offers no bulk delete). The caller decides the remote outcome from the request's own transmission
evidence: ``NOT_APPLIED_PROVEN`` only for the transmission-precluded whitelist, ``UNKNOWN`` for
everything else — a timeout, a lost response, a 5xx, a 4xx after the handoff (the provider refuses
a deletion while an order or claim is open, or a product under a sale ban), an unexpected redirect,
a 200 that is not the documented response. ``APPLIED_PROVEN`` only for the documented success.
Nothing is ever resent here; whether an unknown deletion happened is decided only by a read-back.
"""

from typing import Any

from app.stages.connect.marketplace.capability import RemoteOutcome
from app.stages.register.deletion import DeleteHandoff
from integrations.marketplaces.smartstore.caller import (
    ProductDeleteRequest,
    SmartStoreCallError,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.execution import BearerSource
from integrations.marketplaces.smartstore.registry import ADOPTED, EndpointId
from integrations.marketplaces.smartstore.transmission import Phase

_ENDPOINT = EndpointId.SMARTSTORE_PRODUCT_DELETE_V2


class SmartStoreDeleteSender:
    def __init__(self, caller: SmartStoreEndpointCaller, bearer: BearerSource) -> None:
        self._caller = caller
        self._bearer = bearer

    def endpoint_adopted(self) -> bool:
        """The adoption fact only — the stack's endpoint-adoption layer."""
        return _ENDPOINT in ADOPTED

    def send(self, *, marketplace_product_id: str) -> DeleteHandoff:
        bearer = self._bearer()
        if bearer is None:
            return _local_refusal("SMARTSTORE_SESSION_UNAVAILABLE")
        try:
            response = self._caller.call(
                EndpointId.SMARTSTORE_PRODUCT_DELETE_V2,
                ProductDeleteRequest(
                    bearer.access_token,
                    bearer.credential_generation,
                    bearer.session_generation,
                    marketplace_product_id,
                ),
            )
        except SmartStoreCallError as failure:
            return DeleteHandoff(
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
        return DeleteHandoff(
            remote_outcome=RemoteOutcome.APPLIED_PROVEN,
            response_status=response.http_status,
            details={"endpoint_id": _ENDPOINT.value},
        )


def _local_refusal(code: str, **details: Any) -> DeleteHandoff:
    """Refused before any transport existed: ``LOCAL_PREFLIGHT`` is transmission-precluded."""
    return DeleteHandoff(
        remote_outcome=RemoteOutcome.NOT_APPLIED_PROVEN,
        error_code=code,
        details={
            "endpoint_id": _ENDPOINT.value,
            "transmission_phase": Phase.LOCAL_PREFLIGHT.value,
            **details,
        },
    )


__all__ = ["SmartStoreDeleteSender"]
