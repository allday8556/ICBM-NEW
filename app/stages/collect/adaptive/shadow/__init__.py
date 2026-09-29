"""Adaptive Collector P3: the shadow foundation (ADR-0017 §10–§11; Issue #110 `5824551569`).

A non-canonical owner beside COLLECT. It holds the per-supplier shadow switch and the only way
into ``SHADOW``, answers a run's frozen shadow decision at its first product-read reservation,
compares the Adaptive engine with the canonical extractor over the one document a run already
read, and keeps the raw shadow records, the append-only evidence ledger and the evidence windows
Phase C will read.

It never fetches, never writes a canonical row, never touches a revision, pointer, run, job,
audit event or ReviewItem, and has no AI, OCR or network capability. No supplier is enabled by
default, and nothing here opens a window by itself.
"""
