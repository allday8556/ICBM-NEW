"""The SmartStore side of registration execution (M5 PR-E).

CREATE and product search remain NOT_ADOPTED. IMAGE UPLOAD is separately adopted, but is not wired
to this registration execution module: it has its own one-call adapter and no durable owner.
The production seams here are shaped by those boundaries:

* :class:`SmartStoreCreateSender` reports unavailable and, if called anyway, raises before any
  transport;
* :class:`SmartStoreReadback` is real: it calls the adopted origin read-back through the registry
  caller and returns only the retained, sanitized response (PR-D's profile);
* :class:`SmartStoreReconcileLookup` reports unavailable, because no product-search contract is
  adopted; an UNKNOWN CREATE therefore stays unresolved rather than being fabricated into an
  absence (ADR-0014 §10).

Nothing here decides retry, state or evidence: that is the domain owner's
(``app.register.execution``). This module only hands over what the provider contract allows.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

from app.core.errors import AppError
from app.register.provider import CreateHandoff
from integrations.marketplaces.smartstore.caller import (
    ProductReadRequest,
    SmartStoreEndpointCaller,
)
from integrations.marketplaces.smartstore.registry import ADOPTION_GAPS, EndpointId

MARKETPLACE_KEY: Final = "smartstore"
# The committed CONNECT session a read-back is made with, or None when there is none.
BearerSource = Callable[[], Any]


class CreateNotAdoptedError(AppError):
    """A CREATE was requested while the provider contract is not adopted."""

    def __init__(self) -> None:
        super().__init__(
            "SMARTSTORE_CREATE_NOT_ADOPTED",
            "SmartStore product CREATE is not adopted: "
            + ADOPTION_GAPS[EndpointId.SMARTSTORE_PRODUCT_CREATE_V2],
            details={"endpoint_id": EndpointId.SMARTSTORE_PRODUCT_CREATE_V2.value},
        )


class ReconcileLookupNotAdoptedError(AppError):
    """A reconcile lookup was requested while no lookup contract is adopted."""

    def __init__(self) -> None:
        super().__init__(
            "SMARTSTORE_RECONCILE_LOOKUP_NOT_ADOPTED",
            "SmartStore product search is not adopted: "
            + ADOPTION_GAPS[EndpointId.SMARTSTORE_PRODUCT_SEARCH],
            details={"endpoint_id": EndpointId.SMARTSTORE_PRODUCT_SEARCH.value},
        )


class SmartStoreCreateSender:
    """The production CREATE seam. It holds no transport, because it may open none."""

    def available(self) -> bool:
        return False

    def send(
        self, *, payload: Mapping[str, Any], idempotency_key: str, listing_identity: str
    ) -> CreateHandoff:
        # Local, before transport, before any marketplace mutation: nothing was applied, and the
        # caller never reaches an Attempt because the send gate asks ``available()`` first.
        raise CreateNotAdoptedError()


class SmartStoreReadback:
    """The adopted origin-product read-back (PR-D), returning the retained response only."""

    def __init__(self, caller: SmartStoreEndpointCaller, bearer: "BearerSource") -> None:
        self._caller = caller
        self._bearer = bearer

    def available(self) -> bool:
        """Whether a committed session exists to read with. Production has none wired yet, so a
        read-back is unavailable rather than unsafe; PR-F wires the real session."""
        return self._bearer() is not None

    def read(self, *, marketplace_product_id: str) -> Mapping[str, Any]:
        bearer = self._bearer()
        if bearer is None:
            raise AppError(
                "SMARTSTORE_SESSION_UNAVAILABLE",
                "no committed SmartStore session can read a product back",
            )
        result = self._caller.call(
            EndpointId.SMARTSTORE_ORIGIN_PRODUCT_READ_V2,
            ProductReadRequest(
                bearer.access_token,
                bearer.credential_generation,
                bearer.session_generation,
                marketplace_product_id,
            ),
        )
        return dict(result.retained)


class SmartStoreReconcileLookup:
    """The reconcile lookup seam. Unavailable while product search is not adopted."""

    def available(self) -> bool:
        return False

    def find(self, *, marketplace_account_id: str, listing_identity: str) -> Mapping[str, Any]:
        raise ReconcileLookupNotAdoptedError()


__all__ = [
    "MARKETPLACE_KEY",
    "BearerSource",
    "CreateNotAdoptedError",
    "ReconcileLookupNotAdoptedError",
    "SmartStoreCreateSender",
    "SmartStoreReadback",
    "SmartStoreReconcileLookup",
]
