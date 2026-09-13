from typing import Protocol

from integrations.marketplaces.identity import MarketplaceIdentity


class MarketplaceAdapter(Protocol):
    """Boundary every marketplace integration implements (ROADMAP §7 adapter rule).

    Core product logic stays platform-neutral; marketplace payload specifics stay behind this
    interface. M0 declares identity only; CONNECT/REGISTER/OPERATE operations are defined with
    the first implementation (SmartStore, M2/M5).
    """

    @property
    def identity(self) -> MarketplaceIdentity: ...
