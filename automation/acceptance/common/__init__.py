"""The M4 offline acceptance harness (Issue #80 PR-F, kickoff 5739459941).

It proves, on a fresh dedicated root, that synthetic source truth traverses the real M4 production
owners end to end: current source revision, canonical Product (ProductGroup) and membership,
composition and Item, BASE_PRODUCT and SOURCE_OFFER bindings, pricing snapshots, derived image
lineage with operator selection and exact-binary QA, base and pricing readiness, durable read-back,
a process restart, and source revision changes with their staleness and refresh.

Nothing here is an owner. Every canonical row is written by a production service: COLLECT's run,
revision and source-asset stores for the synthetic source truth, then the M4 materializer, pricing,
image and readiness services. The harness reads state back through production read services and
uses read-only SQL only for immutability evidence. It has no provider capability: no supplier,
marketplace, AI, OCR or browser code is constructed or importable while it runs.
"""
