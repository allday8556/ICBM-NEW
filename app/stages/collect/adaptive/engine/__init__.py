"""Adaptive Collector offline core (ADR-0017, Issue #110, production slice P1).

A second implementation of the COLLECT parser seam: one generic engine interprets an immutable,
digested profile bundle over a document it is handed, and answers the three questions of
``SupplierCollection`` — identity, fields, image references. Validation replays operator-captured
samples offline.

P1 is pure and offline. Nothing here reads a supplier, opens a session, touches the database,
persists a profile or a validation run, or writes a ``ProductFactsRevision``, and nothing in the
COLLECT runtime calls it yet. Every one of those steps needs its own authorized slice.
"""
