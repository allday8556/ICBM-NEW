"""The Adaptive Collector Phase C campaign harness (Issue #110 C0 `5826469852`).

An acceptance-style harness, not an application surface. It is the only non-test caller of the
Phase C operator actions — capture requests and sample finalization, the shadow switch, evidence
windows and mismatch resolutions — and it runs only with the ICBM application stopped: every
command that touches the live ``--data-root`` holds the ADR-0006 data-directory lease for the
whole command, and composes the production owners only after ownership is proven. It never
submits a product collection; collections remain the operator's ordinary path.

Its own record is an append-only, hash-chained campaign ledger in an operator-supplied
``--campaign-root`` outside the repository and outside every ICBM data directory. Stages are
authorized by recorded architect authorizations, their ceilings are frozen at campaign creation,
and every action that can affect real evidence needs the campaign's typed approval phrase.
"""
