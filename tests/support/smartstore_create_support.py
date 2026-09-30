"""Test-only CREATE sender whose projection the test declares (M5 CREATE adoption slice).

The production :class:`SmartStoreCreateSender` takes no projection: it always sends from
``product.project`` over the frozen Snapshot, and at this adoption every such projection carries
named gaps, so every CREATE is refused before a transport exists. To exercise what the sender does
*after* that gate — the registry-gated caller, the response contract and the outcome
classification — a test declares a gap-free :class:`~integrations.marketplaces.smartstore.product.
WireProjection` around a real, validated document. This subclass lives only under ``tests/``; the
container wires the base class.
"""

from collections.abc import Callable, Mapping
from typing import Any

from app.stages.register.model import ListingShape
from integrations.marketplaces.smartstore import product
from integrations.marketplaces.smartstore.execution import BearerSource, SmartStoreCreateSender


def declared(document: Any, gaps: tuple[str, ...] = ()) -> product.WireProjection:
    """A real WireProjection around ``document`` that declares exactly ``gaps``."""
    identity = getattr(document, "listing_identity", "")
    return product.WireProjection(
        encoding_version=product.WIRE_ENCODING_VERSION,
        listing_shape=ListingShape.SEPARATE_LISTINGS,
        codes=product.SellerCodes(
            seller_management_code=product.seller_management_code(identity) if identity else "",
            option_codes=(),
            listing_identity=identity,
        ),
        document=document,
        image_references=(),
        gaps=gaps,
    )


class DeclaredProjectionSender(SmartStoreCreateSender):
    """A CREATE sender whose projection comes from the test instead of the Snapshot."""

    def __init__(
        self,
        *,
        caller: Any,
        bearer: BearerSource,
        projection: Callable[[Mapping[str, Any]], object],
    ) -> None:
        super().__init__(caller=caller, bearer=bearer)
        self._declared = projection

    def _projection(self, payload: Mapping[str, Any]) -> object:
        return self._declared(payload)
