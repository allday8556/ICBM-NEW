"""Provider-zero Coupang Open API contracts.

No module in this package opens a socket or grants endpoint adoption. C-AUTH-1 owns the signing,
credential/context, error-classification and fake-transport boundaries. Catalog, products, orders,
claims, logistics, customer service, settlement, promotions, Rocket Growth, and CGF Lite add
fake-only request/response contracts on top of that boundary. Contract-bound promotion, Rocket
Growth, and CGF Lite callers remain account-gated; the latter two additionally expose stable
operator-safe blocked states.
"""
