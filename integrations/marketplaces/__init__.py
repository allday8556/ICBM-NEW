"""Marketplace adapters (first implementation: Naver SmartStore, M2/M5).

The package no longer has a generic ``base.py``. Its ``MarketplaceAdapter`` Protocol declared only
an ``identity`` property; no module or test imported it and no current contract relied on it (fresh
scan, Issue #151 reconciliation). ``documents/reference/PATH_MIGRATION_MAP.md`` records its removal.
The adapter boundary it anticipated is owned by the REGISTER provider ports of ADR-0014 in
``app/stages/register/provider.py`` (``CreateSender``, ``ReadbackSource``, ``ProviderAssetSource``,
``DuplicateLookupSource``, ``ReconcileLookup`` and the others there), and ``identity.py`` in this
package keeps ``MarketplaceIdentity``.

The Protocol cited the ROADMAP "Marketplace adapter rule". That rule names no interface file ("Core
product logic stays platform-neutral"; "Each marketplace implements the same registration/operation
adapter boundaries"), so it needs no edit: those boundaries are the ADR-0014 ports above.
"""
