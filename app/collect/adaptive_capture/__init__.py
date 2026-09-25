"""Adaptive Collector Phase C, stage C0: the in-memory ValidationSample capture owner.

Issue #110 `5826469852` item 3–5. An ordinary operator collection may be asked, ahead of time and
only through the Phase C harness, to keep a sanitized *capture candidate* of the one document it
reads anyway. The request is consumed and the decision frozen at the run's first product-read
reservation; the default is off. The candidate is cut in memory by the accepted capture sanitizer
after the canonical revision has committed; the page body is never kept. Later the operator
records a scope and the expected facts over the candidate, and a new immutable ValidationSample is
finalized through the P2 owner.

It never fetches, never changes a canonical row, and has no AI, OCR or network capability.
"""
