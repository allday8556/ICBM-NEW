"""M3 REAL acceptance campaigns — ``m3-accept-01``, replaced by ``m3-accept-02`` (Issue #52 rulings
5711123764, 5711187191, 5714750891).

This package orchestrates; it does not collect. Every product read and every image request goes
through the production ``ProductCollectionService`` and its policed gateway, the session comes from
the M1 connection owner, and every asset and revision is written by generic COLLECT core. What
lives here is the campaign around that path: an immutable manifest, a durable ledger that reserves
each external request before it can be sent, the PASS-A / WAITING_FOR_PACING / PASS-B state
machine, and a sanitized closeout.

Nothing here parses a supplier document, classifies an image, hashes a byte or appends a revision.
"""
