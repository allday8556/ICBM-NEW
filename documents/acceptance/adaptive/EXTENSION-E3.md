# Extension capture transport E3 — the list queue

- Status: **ACCEPTED — the real KM통상 acceptance passed on exact main `091133b6`** (§5.2; queue
  `ecdd0927-a4f9-42d2-a7e6-f98d3ae69725`, 27 products found, 25 `RECORDED`, 1 `SKIPPED`, 1
  `FAILED` by a host operating error disclosed there). A green PR, a green CI or a synthetic test
  run accepts nothing on its own.
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
   loaded and returns only the links the operator can see that fully match that form, as their
   scheme, host and path (`ui/extension/lib/discover.js`). A hidden anchor is never discovered. Nothing of the list page itself, and no credentials, query or
   fragment, is sent.
2. **The bounds are declared twice.** The supplier's `CollectionProfile` declares its
   `QueueLimits`; the operator declares the queue's number of products and its interval inside
   them. A missing or out-of-range bound refuses before the queue exists; nothing is defaulted.
3. **Every read is issued by ICBM, durably, before it happens.** The service worker asks for the
   next read and waits as long as ICBM says. When ICBM issues one, the worker navigates the
   operator's own tab to that one URL, captures it with the unchanged E1 cut and sends it with its
   single-use ticket through the unchanged ingest.
4. **A queue goes on past an item that fails** (the user's rule of 2026-10-02): an expired read, a
   refused capture or a `FAILED` run leaves its item as it ended, never reissued, and the queue
   reads the next item.

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
| a refused or expired read and a `FAILED` run leave their item as it ended and the queue goes on | the same |
| the same-product interval counts an extension capture; one issued read per supplier | the same |
| a queue read by read in a real Chromium, with nothing of the list page sent | `tests/integration/collect/extension/test_extension_e2e.py` |
| a queue without its bounds is refused and reads nothing | the same |

## 5. The real acceptance

**Not authorized by this record.** It is a real supplier read, so it needs the user's own decision
and the user's own physical click. Claude never browses the supplier.

The grant names, before anything runs:

- the exact main the acceptance runs on;
- one KM통상 list page, opened by the user;
- the number of products and the interval, within KM's declared limits (500 products, at least
  10 s apart; the user's bounds of 2026-10-02);
- whether collected products are skipped.

The operator's procedure:

1. Open the granted list page in the paired Chrome.
2. In the side panel choose `이 페이지의 상품 목록 찾기`, then declare the granted bounds.
3. Start the queue once. There is no retry: a product that fails is reported as it failed, and
   the queue goes on.

The record then states, from ICBM and never from the extension:

- the queue's own state;
- each item's state;
- each run's outcome, revision and code.

### 5.1 Granted attempts

The user granted, on 2026-10-02: the list page `https://kmretail.co.kr/product/list.html?cate_no=23`,
all its products (27), 10 s apart, collected products skipped; and, before the second attempt, the
bounds 500 products a queue and a 10 s floor (PR #194). Evidence root:
`C:\Users\user\ICBM-acceptance\e3-km-exact-main-e2854818` (`GRANT.md`, `e3-km-cate23-01\RESULT.md`,
`counts-ready.json`, `counts-after.json`, the server log).

- **e3-km-cate23-01, first attempt, exact main `e285481` (before PR #195): stopped.** Discovery
  returned 29 links where the operator saw 27 — the page held anchors it did not show. 349 was
  `SKIPPED` by the skip rule. The first item, product 237, was one of the unseen links; the
  storefront answered an error page, the browser sent nothing, the read expired (counted) and the
  queue stopped at its first item, as AC-30 of that main required. The user then ruled (PR #195):
  discover only the links the operator can see, and a queue goes on past an item that fails. The
  queue was cancelled by the user.

### 5.2 The acceptance

- **Exact main** `091133b602c14cb90f108bdc6d86a73698bbae7d` (PR #188, #189, #190, #193, #194,
  #195; POST_MERGE_VERIFY PASS, tree `96279004585c2e70dcaca8e4cbd7e2b465cc3f5d` = the audited head
  of #195), `DRY_RUN`. Capture policy `kmretail-capture-2`, digest
  `74670a991bf12686b837928436ebf6bae67cdb0516d5299c3eb22c1762707679`. KM queue limits 500 / 1000 /
  10 s / 120 s.
- **Queue** `ecdd0927-a4f9-42d2-a7e6-f98d3ae69725`, declared 2026-10-02 14:37:38Z by the user's
  click: 27 products, 10 s, skip collected. **Discovery returned 27 links — exactly the operator's
  list.** Last read issued 15:18:01Z; `FINISHED` by 15:18:29Z. 26 reads issued, one at a time, in
  list order, at least 10 s apart and each only after the previous run had settled.
- **Items:** 26 `CAPTURED`, 1 `SKIPPED` (349, collected on this data root before). Nothing was
  reissued or retried; no product was read twice.
- **Runs** of the 26 captured items, all `EXTENSION`, all with `product_read_at` NULL (no server
  product read): **25 `RECORDED`**, each with its own `ProductFactsRevision` (facts status
  `REVIEW_REQUIRED`, as 349's in E2: images, minimum sale price and detail description are review
  items), source product, materialized Product and review items; **1 `FAILED`** — item 1, product
  355, `EXTENSION_CAPTURE_BUFFER_MISSING`.
- **The failure was the host's, not the queue's.** The host (Claude) had moved the acceptance
  checkout to the new main without upgrading the data root to Track B's migration 0036, so the
  server started without its job worker and the first capture waited in the in-process buffer; the
  host upgraded the data root and restarted the server, and the buffered capture was gone with the
  old process (a capture is never durable and never replayed, ruling `5906712259` N-1). The queue
  went on, as the user's rule says. The extension's loop stopped while ICBM was unreachable, and
  the user resumed the same queue from the panel (`열려 있는 대기열` → `재개`); no second queue was
  declared. Product 355 is left to the operator's single click.
- **Supplier traffic (server log, 14:37Z–15:19Z):** 303 `IMAGE_REQUEST`, all HTTP 200, retry
  count 0, under the 30-per-run cap (the largest run made 15): `onewbio.diskn.com` ×227,
  `kmretail.co.kr` ×76 — only the KM profile's image hosts. **0 product-page requests by the
  server.** No egress block. Claude never opened the supplier.
- **Data root after the run:** `collection_runs` 27, `product_facts_revisions` 26,
  `source_assets` 169, `product_groups` 26, `source_products` 26, `review_items` 93,
  `extension_queues` 2, `extension_queue_items` 56. No marketplace table changed; no LIVE.
- **Security:** the §6.1 gate refused nothing; one run carried gate notes (kinds and boundaries
  only); no captured value is logged. The member-name byte scan of E2's record was not repeated
  (the name is not held by the host); the gate's member-identity rules are unchanged from the
  accepted E2 main.
- **Verdict: ACCEPTED.** The queue did what §8.1 says on a real list page: the operator's 27
  visible products, server-issued reads one at a time at the declared interval, the skip rule, 25
  ordinary `EXTENSION` runs through the unchanged pipeline, and a failed item left as it ended
  while the queue went on.
