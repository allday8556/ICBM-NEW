"""Marketplace presentation identity.

v29 embedded a platform registry as a *visual prototype mechanism* (UI_SOURCE_OF_TRUTH). At
runtime, identity is owned here, next to the adapters, and reaches the UI through the shell
contract. Logos are the user-supplied v29 placeholder marks, extracted unmodified.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MarketplaceIdentity:
    key: str
    label: str
    brand_color: str
    logo_asset: str | None


# Order follows ROADMAP §9 marketplace sequence.
MARKETPLACE_IDENTITIES: tuple[MarketplaceIdentity, ...] = (
    MarketplaceIdentity("smartstore", "스마트스토어", "#03C75A", "smartstore.png"),
    MarketplaceIdentity("coupang", "쿠팡", "#F0462D", "coupang.png"),
    MarketplaceIdentity("st11", "11번가", "#E33659", "st11.png"),
    MarketplaceIdentity("gmarket", "G마켓", "#22A84F", "gmarket.png"),
    MarketplaceIdentity("auction", "옥션", "#E5342B", "auction.png"),
)
