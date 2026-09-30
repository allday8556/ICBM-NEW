# Extension capture transport E1 — one click, compare only

- Status: **PENDING — implemented provider-zero; the one real KM통상 acceptance is not run and is
  not authorized** (§5). A green PR, a green CI or a synthetic test run accepts nothing.
- Issue: #126. Contract: `documents/decisions/adr/0019-extension-primary-collection-transport.md`
  (E1 of §10).
- Authority (Issue #126 comments):
  - the architect ruling `5906290729` (B-1 … B-11);
  - the architect ruling `5906712259` (N-1 … N-3);
  - the owner amendment `5907095955`, which approves the E1 specification `5907009512` in full,
    by its body SHA-256 `bb9906ccd61cac270908ad64de50be98a645bbec74313be5c3e5d577bed2fcf4`.
- Start condition met: PR #159 merged as main `4d913bb9268ee2d2760d0d7e334f54c7a3b9b3ef`, its
  POST_MERGE_VERIFY passed (PR #159 comment `5906920106`), and no other core slice was
  open.

## 1. What E1 is

One click in the operator's own Chrome captures the one product page in the active tab, cut to the
product scope, and sends it to the paired ICBM over the loopback. ICBM authenticates the sender,
applies the ceilings, opens a canonical collection run, final-scans exactly what arrived, builds
the same `DocumentView`, runs the canonical KM extractor in memory and the Adaptive dry run, and
settles the run.

**E1 appends nothing.** A successful run is `NO_REVISION` with the code `EXTENSION_COMPARE_ONLY`;
a failure after the run is opened is `FAILED`; `RECORDED` is unreachable.

## 2. What E1 is not

- no `ProductFactsRevision`, no pointer change, no source asset, no Adaptive row;
- no list crawl, no second supplier, no server supplier request for an extension run;
- no `ACTIVE`, no cutover, no image byte relay, no AI, OCR or vision, no new `EvidenceKind`;
- no general `BrowserCapturePolicy` editor, and no change to
  `documents/contracts/ui/UI_SOURCE_OF_TRUTH.md`;
- the stopped campaign `phase-c-kmretail-01` and its data root are not read, reused or touched.

## 3. Owners

| concern | owner |
| --- | --- |
| client | `ui/extension/` — MV3, plain ES modules, no build step, no dependency |
| ingest, pairing, replay cache, buffer, policy loader | `app/stages/collect/extension/` |
| routes | `app/interface/api/routes/collect_extension.py` |
| KM capture policy | `integrations/suppliers/kmretail/browser_capture_policy.json` |
| Adaptive dry run | `app/stages/collect/adaptive/shadow/dry_run.py` |
| provenance | migration `0032_collect_transport_provenance` |
| pairing commands | `icbm extension pair | rotate | revoke` |

## 4. What is verified provider-zero

Every item below is a test in the repository. None of them contacts a supplier.

| claim | where |
| --- | --- |
| refusals open no run: loopback, client header, origin, pairing, stale timestamp, replay, body digest, each ceiling, evidence, target, policy | `tests/integration/collect/extension/test_extension_ingest_api.py` |
| an accepted capture opens and settles exactly one run; every other table is untouched | the same file |
| ceilings are checked after authentication and before a run exists | the same file |
| the in-process buffer and the non-idempotent job: the five tests of ruling N-1 | `tests/integration/collect/extension/test_extension_capture_job.py` |
| `NO_BUNDLE`, and an enabled bundle compared in memory with nothing written | the same file |
| migration 0032: additive, triggers intact, no backfill, provenance never identity | `tests/integration/collect/extension/test_transport_provenance.py` |
| the C1 regression pair, the policy cut, the same facts on both transports, the preconditions and the bounds, in a real browser | `tests/integration/collect/extension/test_extension_capture_browser.py` |
| the unpacked extension end to end against the real application | `tests/integration/collect/extension/test_extension_e2e.py` |
| the policy owner, the pairing, the replay cache, the capture structure | `tests/unit/collect/extension/` |
| the contract pins | `tests/contracts/test_extension_e1_contract.py`, `tests/contracts/test_repository_rules.py` |

Two facts a reader needs:

- **The two transports quote different evidence for the same facts.** The KM capture policy keeps
  only three `<head>` elements (ruling B-10), so an extension `DocumentView` has no `og:title`,
  `og:image` or `product:price:amount` to quote. On the synthetic fixture the field statuses and
  values are equal on both transports. A later comparison of an `EXTENSION` revision with a
  `DIRECT_URL` one reports the difference under the existing `EVIDENCE_DRIFT` rules. That is a
  decision for E2, where an `EXTENSION` revision is first written; E1 writes none.
- **The end-to-end test needs a browser that loads an unpacked extension.** It ran on the system
  Edge on the development machine. Where no such browser exists the three tests skip and say why.

## 5. The one real acceptance — not authorized

E1 is accepted only by one real KM통상 capture on the exact merged main, under a separate user
grant (ruling B-9). The grant names:

- the exact product URL and identity;
- the exact E1 HEAD;
- a new acceptance root and campaign id under `%USERPROFILE%\\ICBM-acceptance\\`;
- exactly one operator page and one click;
- zero automated supplier reads;
- the fixed E1 ceilings.

Operator procedure (E1 specification §11.2):

1. The operator has the **final product URL** open in the tab.
2. If the tab arrived there through a redirect, the operator **reloads the final URL directly** and
   only then clicks.
3. The extension checks `location.href == navigation entry.name` and `redirectCount == 0` before
   any capture.
4. If a precondition is not met, the extension refuses before the capture starts: nothing is read,
   cut or sent, no run is opened, and **the authorized click is not consumed**.
5. The authorized click is consumed only by a capture that actually starts.

The operator opens the page and clicks. Claude never browses the supplier.

Acceptance artifact (E1 specification §2.3): the `collection_run_id`, `NO_BUNDLE`, the canonical
extractor result, the zero-write proof and the security and capture evidence. `NO_BUNDLE` is never
worded as a comparison PASS or as `VALIDATED`.

## 6. Incident during implementation: two unintended requests to the supplier host

On 2026-09-30, while the browser tests were being written, **two HTTP requests reached
`kmretail.co.kr`**. Neither was authorized.

| # | what sent it | request | observed |
| --- | --- | --- | --- |
| 1 | a test that answered a navigation with a routed `302` | `GET https://kmretail.co.kr/product/synthetic-sample/9001/` — an invented path | the page reported status 404 |
| 2 | a probe run to find the cause | `GET https://kmretail.co.kr/final` — an invented path | the page reported status 404 |

- **Cause.** Playwright does not offer the follow-up request of a routed redirect to the route
  handler, so the browser followed the redirect on the real network.
- **What was sent.** One plain `GET` each, from a fresh headless browser profile: no session, no
  cookie, no credential, no ICBM data. Every subresource request was still answered or aborted by
  the test.
- **What was read.** No product page and no product data; both paths do not exist.
- **Correction.** Every browser these tests launch now resolves no host name at all
  (`--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1`), so a request the test did not
  answer fails in the resolver. The redirect test uses a real loopback server. A test proves the
  block (`localhost` no longer resolves, and a routed redirect is not followed), and a contract
  test refuses a browser launch in these tests without it.
- These two requests are outside the E1 zero counts of §5, which describe the acceptance run; they
  are recorded here so that they are never mistaken for it or hidden by it.
