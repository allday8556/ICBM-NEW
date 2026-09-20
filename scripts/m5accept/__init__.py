"""The M5 offline acceptance harness (Issue #89 PR-F §A, kickoff 5750366733).

It proves, on a fresh dedicated root, that the M5 registration vertical behaves as ADR-0014 says
when it is driven through the **real production owners** — the registration store and its
triggers, the preflight and Snapshot builder, the execution owner over the M0 job system, the
execution-scope brake, and the M4 product, pricing and image owners that feed them.

Nothing here is an owner. Every durable row is written by a production service; the harness reads
state back through production reads and uses read-only SQL only for immutability evidence.

**It is offline and provider-zero.** The CREATE, image-upload and product-search contracts are
NOT_ADOPTED, so the run drives the execution owner with local fakes for those seams only, and its
guards refuse every HTTP client, browser, AI, OCR and supplier transport for the life of the run.
The report states the measured counters, including a marketplace mutation count of zero.

A PASS here is **not** M5 acceptance: it is offline evidence for one commit. The bounded real
canary stays a separate, explicitly authorized campaign (ADR-0014 §24).
"""
