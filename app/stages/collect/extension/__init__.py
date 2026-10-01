"""The extension capture transport, server side (ADR-0019; Issue #126 E1, then E2).

The operator's own Chrome, through the first-party extension under ``ui/extension/``, captures one
product page on a click and sends the product-scoped result here over the paired loopback. This
package is the ingest owner: it authenticates the sender, applies the ceilings, judges the target,
binds the capture to the reviewed ``BrowserCapturePolicy``, and only then opens a canonical
collection run for it.

From E2 an accepted capture is **recorded**. It is final-scanned, turned into the same
``DocumentView`` the direct-URL path builds, and handed to the collection owner, which records it
exactly as it records a direct read: the supplier's canonical extractor is the revision writer, the
server fetches the images itself, and the frozen shadow decision follows. The run settles
``RECORDED``, ``NO_REVISION`` when the identity is unresolved, or ``FAILED``.

This package writes nothing itself and sends no request to a supplier: the revision, its source
assets and the image reads are the collection owner's. It stores no captured page material either:
the capture lives in a bounded in-process buffer until its job consumes it (ADR-0002 Option A).
"""
