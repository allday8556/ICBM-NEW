"""The M3 REAL acceptance campaign ``m3-accept-01`` (Issue #52 rulings 5711123764, 5711187191).

This package orchestrates; it does not collect. Every product read and every image request goes
through the production ``ProductCollectionService`` and its policed gateway, the session comes from
the M1 connection owner, and every asset and revision is written by generic COLLECT core. What
lives here is the campaign around that path: an immutable manifest, a durable ledger that reserves
each external request before it can be sent, the PASS-A / WAITING_FOR_PACING / PASS-B state
machine, and a sanitized closeout.

Nothing here parses a supplier document, classifies an image, hashes a byte or appends a revision.
"""
