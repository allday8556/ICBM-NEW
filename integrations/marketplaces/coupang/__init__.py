"""Provider-zero Coupang Open API contracts.

No module in this package opens a socket or grants endpoint adoption. C-AUTH-1 owns the signing,
credential/context, error-classification and fake-transport boundaries. Catalog, products, orders,
and claims add fake-only request/response contracts on top of that boundary.
"""
