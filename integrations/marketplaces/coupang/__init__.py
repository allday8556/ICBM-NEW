"""Coupang Open API contracts plus one bounded credentialed read canary.

C-AUTH-1 owns the signing, credential/context, error-classification and fake-transport boundaries.
Catalog, products, orders, claims, logistics, customer service, settlement, promotions, Rocket
Growth, and CGF Lite remain fake-only. C-LIVE-R1 adds exactly one non-generic KR ``GET`` canary;
it accepts no caller-selected target or mutation and is guarded by identity-bound approval,
IP/clock preflight, one-shot use and explicit provider-only egress.
"""
