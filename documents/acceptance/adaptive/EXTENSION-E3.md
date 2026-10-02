# Extension capture transport E3 — the list queue

- Status: **PENDING — the real KM통상 acceptance has not run.** It needs the user's own grant and
  the user's own click (§5). A green PR, a green CI or a synthetic test run accepts nothing on its
  own.
- Issue: #126. Contract: `documents/decisions/adr/0019-extension-primary-collection-transport.md`
  §8.1 and AC-27 to AC-31 (merged as PR #188).
- Builds on E1 and E2 (`documents/acceptance/adaptive/EXTENSION-E1.md`, `EXTENSION-E2.md`). The
  cut, the sender, the ceilings, the capture policy, the §6.1 security gate and the E2 pipeline are
  unchanged. Every queued product is an ordinary `EXTENSION` run.

## 1. What E3 is

The operator finds a supplier list page's products in the side panel and declares a queue. ICBM
decides every read.

1. **Discovery reads nothing.** The side panel asks ICBM for the supplier's reviewed product path
   form and its declared queue limits. The extension then reads the list page the operator already
   loaded and returns only links that fully match that form, as their scheme, host and path
   (`ui/extension/lib/discover.js`). Nothing of the list page itself, and no credentials, query or
   fragment, is sent.
2. **The bounds are declared twice.** The supplier's `CollectionProfile` declares its
   `QueueLimits`; the operator declares the queue's number of products and its interval inside
   them. A missing or out-of-range bound refuses before the queue exists; nothing is defaulted.
3. **Every read is issued by ICBM, durably, before it happens.** The service worker asks for the
   next read and waits as long as ICBM says. When ICBM issues one, the worker navigates the
   operator's own tab to that one URL, captures it with the unchanged E1 cut and sends it with its
   single-use ticket through the unchanged ingest.
4. **A queue stops, and never skips forward,** at the first item that does not end `RECORDED` or
   `NO_REVISION`: an expired read, a refused capture or a `FAILED` run.

The server half is `app/stages/collect/extension/queue.py` with migration 0035 (PR #189); the
extension half is the discovery, the worker's queue loop and the side panel's queue board.

## 2. What E3 is not

- Not a crawl. The extension reads only the list page the operator opened and only the product
  pages ICBM issued, one at a time, in the operator's own tab.
- No list-page topology is added to a supplier: reconnaissance never observed one.
- No read is retried or reissued, and nothing is resent.
- No new field, image or revision rule: each product is recorded exactly as a single click is.

## 3. Owners

| what | owner |
| --- | --- |
| the queue, its items, every issued read | `app/stages/collect/extension/queue.py` (migration 0035) |
| the queue limits | the supplier's `CollectionProfile` (`QueueLimits`) |
| link judgement | `check_target` and the §6.1 locator rule (`gate.locator_holds_secret`) |
| discovery in the page | `ui/extension/lib/discover.js` |
| the queue loop | `ui/extension/service_worker.js` |
| the queue board | `ui/extension/sidepanel.html`, `sidepanel.js`, `sidepanel.css` |
| each product's run and revision | the unchanged E1 ingest and E2 pipeline |

## 4. What is verified provider-zero

| claim | where |
| --- | --- |
| no declared limits, no queue; a missing or out-of-range bound refuses with zero rows | `tests/integration/collect/extension/test_extension_queue.py` |
| every refused link is counted and never stored or logged | the same |
| issue, wait, capture, settle and done; the ticket's digest stored, never the ticket | the same |
| a refused or expired read and a `FAILED` run stop the queue without skipping forward | the same |
| the same-product interval counts an extension capture; one issued read per supplier | the same |
| a queue read by read in a real Chromium, with nothing of the list page sent | `tests/integration/collect/extension/test_extension_e2e.py` |
| a queue without its bounds is refused and reads nothing | the same |

## 5. The real acceptance

**Not authorized by this record.** It is a real supplier read, so it needs the user's own decision
and the user's own physical click. Claude never browses the supplier.

The grant names, before anything runs:

- the exact main the acceptance runs on;
- one KM통상 list page, opened by the user;
- the number of products and the interval, within KM's declared limits (20 products, at least
  10 s apart);
- whether collected products are skipped.

The operator's procedure:

1. Open the granted list page in the paired Chrome.
2. In the side panel choose `이 페이지의 상품 목록 찾기`, then declare the granted bounds.
3. Start the queue once. There is no retry: a stopped queue is reported as it stopped.

The record then states, from ICBM and never from the extension:

- the queue's own state and stop reason;
- each item's state;
- each run's outcome, revision and code.
