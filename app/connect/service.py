from collections.abc import Sequence

from app.connect.contracts import (
    MarketplaceConnectionState,
    MarketplaceConnectionSummary,
    SupplierConnectionSummary,
)
from integrations.marketplaces.identity import MarketplaceIdentity


class ConnectService:
    """CONNECT stage owner.

    SupplierConnection (M1) and MarketplaceAccount (M2) persistence does not exist in M0, so no
    connection can exist: suppliers are an empty list and every marketplace is NOT_CONNECTED.
    """

    def __init__(self, marketplaces: Sequence[MarketplaceIdentity]) -> None:
        self._marketplaces = tuple(marketplaces)

    def supplier_connections(self) -> list[SupplierConnectionSummary]:
        return []

    def marketplace_connections(self) -> list[MarketplaceConnectionSummary]:
        return [
            MarketplaceConnectionSummary(
                marketplace_key=m.key, connection_state=MarketplaceConnectionState.NOT_CONNECTED
            )
            for m in self._marketplaces
        ]

    def connected_supplier_count(self) -> int:
        return len(self.supplier_connections())

    def connected_marketplace_count(self) -> int:
        return sum(
            1
            for c in self.marketplace_connections()
            if c.connection_state is not MarketplaceConnectionState.NOT_CONNECTED
        )
