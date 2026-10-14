"""Overseas purchase agency (구매대행, ADR-0038): the FX snapshot owner and the settings card.

- ``fx``: Korea Eximbank's daily TTS per currency, recorded immutable, its FX-03 staleness rule,
  the operator's API key (OS secret store only) and the refresh;
- ``settings``: the versioned 해외 구매대행 settings (§5).

A supporting capability: it records rates and settings and prices nothing. The landed cost that
reads them is the pricing owner's (O2), and the cross-border projection the marketplace adapter's
(O3).
"""
