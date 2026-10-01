# Extension capture transport E2 — one click, recorded

- Status: **PENDING — implemented; no real KM통상 acceptance is run**. A green PR, a green CI or a
  synthetic test run accepts nothing.
- Issue: #126. Contract: `documents/decisions/adr/0019-extension-primary-collection-transport.md`
  (E2 of §10: "the existing KM extractor consumes the extension's `DocumentView` as the revision
  writer").
- Builds on E1 (`documents/acceptance/adaptive/EXTENSION-E1.md`). Everything E1 fixed about the
  client, the sender, the ceilings, the policy, the buffer and the final gate is unchanged.

## 1. What E2 is

An accepted extension capture is recorded. After the server's own structure check and final gate,
the capture becomes the same `DocumentView` a direct read produces, and the collection owner
records it through the one pipeline after capture (ADR-0019 §2):

1. the run's shadow and capture decisions are frozen when its job starts, as a direct run's are;
2. the supplier's identity and role rules read the document;
3. the server fetches the product's images itself, through the collection gateway and under the
   supplier's image limits (ADR-0019 §7);
4. the supplier's canonical extractor writes the `ProductFactsRevision`, carrying the run's
   `transport_kind`, `capture_policy_revision` and `capture_policy_digest`;
5. the run settles `RECORDED` and is handed on exactly as a direct run is;
6. the frozen shadow decision follows, in its own unit, and never reaches the run.

An unresolved identity is `NO_REVISION` with the parser's own reason, as on the direct path. Any
failure after the run is opened is `FAILED`.

## 2. What E2 is not

- no second pipeline: no extractor, identity rule, image rule or store of its own;
- no server product read for an extension run: its budget allows none and it reserves none;
- no image byte relay from the browser, no list crawl, no second supplier;
- no replay of a capture: the job stays non-idempotent with one attempt, and an attempt that
  appended its revision and died before settling is finished from that revision;
- Adaptive is not a writer: no `ACTIVE`, no cutover, no AI, OCR or vision, no new `EvidenceKind`;
- no schema change: the provenance columns are E1's migration;
- no change to the extension client, its manifest, the routes or
  `documents/contracts/ui/UI_SOURCE_OF_TRUTH.md`.

## 3. Owners

| concern | owner |
| --- | --- |
| recording a captured document, recovery, settling | `app/stages/collect/collection.py` (`record_captured_document`, `recover_recorded`, `settle`) |
| freezing the decisions of a captured run | `app/stages/collect/runs.py` (`freeze_captured_run`) |
| the ingest owner, which hands the document over and writes nothing itself | `app/stages/collect/extension/service.py` |
| composition | `app/container.py` |

The E1 zero-write dry run (`app/stages/collect/adaptive/shadow/dry_run.py`) is removed: the run
takes the pipeline's own shadow.

## 4. What is verified provider-zero

Every item is a test in the repository. None contacts a supplier: the collection gateway is a
script that sends nothing, and every test browser is loopback-only.

| claim | where |
| --- | --- |
| an accepted capture is recorded: one run, one revision carrying the transport provenance, no server product read, the images fetched by the server | `tests/integration/collect/extension/test_extension_capture_job.py`, `tests/integration/collect/extension/test_extension_ingest_api.py` |
| the five tests of ruling N-1 still hold: a missing buffer fails and is never retried, an interrupted attempt is never replayed | `tests/integration/collect/extension/test_extension_capture_job.py` |
| an attempt that appended its revision and died is finished from that revision, with no second revision | the same file |
| a second capture of the same product appends the next revision of the same source product | the same file |
| without an enabled bundle an extension run has no shadow; with one it is shadowed as a direct run is, after the canonical write | the same file |
| an unresolved identity is `NO_REVISION` with the parser's reason; a refused final gate is `FAILED` with no revision | `tests/integration/collect/extension/test_extension_ingest_api.py`, `tests/integration/collect/extension/test_extension_capture_browser.py` |
| the real extension, one click, reads back a `RECORDED` run | `tests/integration/collect/extension/test_extension_e2e.py` |
| the ingest owner calls no store and no gateway: the recorder is its only way to a revision | `tests/contracts/test_extension_e1_contract.py` |
| a production budget allows one product read, and only a captured run's allows none | `tests/contracts/test_repository_rules.py` |

## 5. The real acceptance — not run

One real KM통상 product page, captured by the operator's own click in their own Chrome and
recorded. It is a real supplier read (the page in the operator's browser, and the image fetches by
the server), so it needs the user's own decision and the user's physical click. Claude never
browses the supplier. Nothing here authorizes it, and this record stays `PENDING` until it is run
and its evidence — the `collection_run_id`, the revision, the provenance, the image reads and the
fresh-session repeat — is recorded here.
