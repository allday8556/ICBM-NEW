"""Adaptive Collector Phase B prototype (Issue #110, ADR-0017 §13).

Isolated, disposable and fixture-only. It proves the ADR-0017 behaviours on synthetic pages and is
never wired into COLLECT, never persisted, and never promoted into production code: production
code is written fresh after its own authorization.
"""
