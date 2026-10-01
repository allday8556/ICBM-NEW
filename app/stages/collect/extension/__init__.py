"""The extension capture transport, server side (ADR-0019; Issue #126 E1).

The operator's own Chrome, through the first-party extension under ``ui/extension/``, captures one
product page on a click and sends the product-scoped result here over the paired loopback. This
package is the ingest owner: it authenticates the sender, applies the ceilings, judges the target,
binds the capture to the reviewed ``BrowserCapturePolicy``, and only then opens a canonical
collection run for it.

E1 is **compare only**. An accepted capture is final-scanned, turned into the same ``DocumentView``
the direct-URL path builds, read by the supplier's canonical extractor in memory and handed to the
Adaptive dry run. Nothing is appended: no ``ProductFactsRevision``, no source asset, no Adaptive
row. The run settles ``NO_REVISION`` (or ``FAILED``), and ``RECORDED`` is unreachable.

Nothing here sends a request to a supplier, and nothing here stores captured page material: the
capture lives in a bounded in-process buffer until its job consumes it (ADR-0002 Option A).
"""
