"""Marketplace adapters (first implementation: Naver SmartStore, M2/M5).

The package no longer has a generic ``base.py``. Its ``MarketplaceAdapter`` Protocol declared only
an ``identity`` property and was never imported by any module, test or current document (fresh scan,
Issue #151 reconciliation). The adapter boundary it anticipated is owned by the REGISTER provider
ports of ADR-0014 in ``app/stages/register/provider.py`` (``CreateSender``, ``ReadbackSource``,
``ProviderAssetSource``, ``DuplicateLookupSource``, ``ReconcileLookup`` and the others there), and
``identity.py`` in this package keeps ``MarketplaceIdentity``.
"""
