"""Adaptive Collector P2: the profile and validation persistence owner (ADR-0017 §3, §7).

The only writer of the six tables migration 0024 creates. It persists immutable,
content-addressed profile revisions with their lineage, the append-only lifecycle of an EPR,
operator-captured validation samples (local only) and validation runs with their exact freshness.
It recomputes every digest when it reads a row back, and refuses anything that does not match.

``VALIDATED`` is never stored: it is derived from a ``PASS`` run for the exact freshness tuple.
Nothing in the COLLECT runtime calls this owner yet, and it writes no ``ProductFactsRevision``.
"""
