# ADR-0023 — M6 OPERATE contract: listing-state sync, supplier stock recheck, read-only order ingest and the order-data lifecycle

Status: **ACCEPTED** 2026-10-07. It is the M6 kickoff contract and lands with its own PR.

Decision owners:
- **Product directions:** the owner, in Issue #219.
  - Kickoff (original): "문제없으면 병합하고 m6ㄱㄱ".
  - Product directions `6033662015`: automatic cadence plus "지금 동기화"; sold-out shown for the operator to decide; periodic re-collect of ACTIVE-registered source products only; order data "배송에 필요한 정보까지 저장".
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

M5 is accepted (`documents/acceptance/milestones/M5.md` §10; owner acceptance `6033126992`), so the precondition "M6 does not start before M5 acceptance" holds (ROADMAP §14, ADR-0018 G3-20).

What it authorizes:
- this contract;
- the provider-zero implementation slices of §9, each its own PR;
- the read-only adoptions of §6, each by its own slice under ENDPOINT_MATRIX §16.

What it does not authorize:
- **No marketplace mutation of any kind:** no status, price, stock or shipment write.
- **No real supplier order.**
- **No transfer of order data outside the local data root.**

Sources:
- `documents/roadmap/ROADMAP.md` §8 (Phase 5 OPERATE and its acceptance) and §12;
- `documents/rules/12-first-vertical.md`;
- `documents/architecture/ARCHITECTURE.md` §OPERATE ownership;
- ADR-0004 (no live price update by a source change alone);
- ADR-0010 §10 (stock judge);
- ADR-0011 §2 (an M6 raw-payload lifecycle contract before any long-lived persistence);
- ADR-0013 (canonical identity, order-line resolution, substitution);
- ADR-0014 §11, §14, §15, §23, §24;
- ADR-0016 (ReviewKind, `NOT_WIRED`);
- ADR-0018 (protected writes);
- rules 05, 07 and 14.

Recorded by: Claude Code (Track B). The number was confirmed free on `main` and in every open branch immediately before writing.
Date: 2026-10-07

---

## 1. Scope

M6 makes a confirmed SmartStore registration an operated listing. It covers three things, all read-only towards the marketplace:

1. **Listing-state sync** (ROADMAP §8.1): the published status, display status, sale price and stock quantity of every `ACTIVE` registration, the last synchronization time, and external deletion.
2. **Supplier stock recheck** (§8.2): the current supplier availability of every source product bound to an `ACTIVE` registration, judged ON_SALE / SOLD_OUT / REVIEW_REQUIRED, shown on 품절 for the operator.
3. **Order ingest** (§8.3): SmartStore product orders, each line resolved to its `MarketplaceRegistrationItem` → canonical Item → source product and supplier, with the shipping data fulfillment needs.

Out of M6:
- every marketplace write: status, sale stop, stock, price, order confirmation, shipment and claim responses;
- inquiries and claims (§8.4): later OPERATE slices;
- fulfillment, supplier order and tracking: M6.5;
- automatic source substitution (ADR-0013 §8): later;
- settlement and actual margin.

## 2. Owners

| owner | holds | never holds |
| --- | --- | --- |
| **ListingSync** (`app/stages/operate/listing/`) | append-only listing-state observations per registration (sanitized read-back fields, observed time, result); the registration's current operated state (derived) | a second registration, an Intent change, a price decision |
| **StockRecheck** (`app/stages/operate/stock/`) | the recheck schedule and the outcome of each recheck (which collection run, which revision, the judged availability) | supplier facts (COLLECT owns them), M4 readiness (M4 owns it) |
| **Orders** (`app/stages/operate/orders/`) | product orders and lines, their resolution to canonical identity, the order-status history, the encrypted shipping record and its lifecycle | buyer data beyond §7, payment data, a second product identity |

REGISTER keeps the registration and its lifecycle. M6 calls `record_readback` and `record_external_absence` on the registration owner. COLLECT keeps collection, and M6 calls its public submit command; it never imports supplier adapters. M4 keeps readiness.

## 3. Listing-state sync

- **Read:** the adopted `SMARTSTORE_ORIGIN_PRODUCT_READ_V2`, by the registration's `originProductNo`, with the CONNECT owner's committed bearer. No new endpoint is needed.
- **Observation:**
  - Each read appends one observation with exactly these sanitized fields: `statusType`, `channelProductDisplayStatusType`, `salePrice`, `stockQuantity`, `sellerManagementCode`, plus the observed time and result.
  - Each read calls `record_readback`.
  - A read that fails records the failure class and nothing else. An unreadable result proves nothing.
- **External deletion:** Under ADR-0014 §14, only provider evidence moves a registration to `EXTERNALLY_REMOVED`, never an operator's word. That evidence is either the documented `statusType = DELETE` or an HTTP 404 on the origin-product read of a registration. Until proven, the registration stays `ACTIVE` with duplicate protection.
- **Drift:** these are shown as the server's comparison, never repaired:
  - a sale price different from the frozen Snapshot price;
  - a status other than `SALE`/`ON`;
  - a stock quantity of 0 while the source is on sale.

  M6 never writes the marketplace (ADR-0004).
- **Cadence:**
  - A server policy value (default 30 minutes), changeable in Settings, drives a durable periodic job over the `ACTIVE` registrations.
  - "지금 동기화" runs the same job now.
  - One job runs at a time.
  - Reads are bounded per run, and a provider rate limit pauses the run without failing registrations.

### 3.1 Automatic token renewal (owner decision `6034380699`)

The SmartStore token lives about three hours, and renewal used to be the operator's "계정 확인". The owner decided on automatic renewal ("토큰 자동 갱신이지").

A CONNECT session keeper (`app/stages/connect/smartstore/keeper.py`) runs CONNECT's own pass (`connect()`) under these conditions:
- **When:** soon after startup, then every minute, whenever `auto_renewal_due()` holds. That means a configured renewal margin, committed credentials, a bound account, no open AUTHENTICATION review, and no current committed bearer.
- **What it does:** it issues the token inside the provider window, commits the new session, re-proves the identity, serializes the pass, and retires the old session only after the new one is durable. This is exactly AUTH.md §15 and §17 Case B, which needs no user approval.

Failure handling:
- A failed or unproven pass backs off (5 to 30 minutes).
- A mismatch or a refused credential opens the existing AUTHENTICATION review, and the keeper waits for the operator.

There is no second token owner. Configuration: `ICBM_SMARTSTORE_AUTO_RENEW` (default on), with no effect without `ICBM_SMARTSTORE_RENEWAL_MARGIN_S`.

## 4. Supplier stock recheck

- **Targets:** only source products bound to an `ACTIVE` registration (owner decision). A product with no active registration is never rechecked by M6.
- **Mechanism:** the COLLECT owner's public submit command, re-collecting by the source identity it already holds.
  - Only that one source product is collected. The recheck uses the existing transport and pacing, so nothing new is ever fetched.
  - A run produces an immutable revision exactly as an operator collection does (ARCHITECTURE: re-collection is a new revision).
- **Not a debug loop:** this is the operating recheck the owner decided. ADR-0010's "never an unattended debug loop" stays in force for development and diagnosis. The recheck has a server policy cadence (default 6 hours), a per-run cap, supplier pacing, and a stop on consecutive failures. Each limit is a policy value.
- **Judge:** ADR-0010 §10 and rule 06 §6.3 decide the judgment:
  - BUY/CART gives ON_SALE;
  - SOLD OUT with no purchase path gives SOLD_OUT;
  - anything else gives REVIEW_REQUIRED.

  Availability is never proven by a supplier write.
- **Output:**
  - The judged state of each registered source product is shown on 품절, with its last recheck.
  - A STOCK `ReviewItem` producer (`operate.stock`) is wired, so the kind leaves `NOT_WIRED` only after its first full reconciliation (ADR-0016 §7).
  - **The operator decides.** M6 never changes the marketplace listing (owner decision).
- **Track A boundary:** the collection code is Track A's. M6-B calls only COLLECT's public command, `ProductCollectionService.submit(supplier_key, product_url)`, and its `run` read.
  - The URL is the `source_url` that the source product's current ProductFactsRevision recorded, so no new COLLECT command and no Track A edit is needed.
  - A re-collection is an ordinary run: COLLECT's target check, its same-product interval and its supplier pacing apply unchanged, and a refusal is recorded as `REFUSED`.
- **Implementation (M6-B):**
  - **Storage:** migration `0049` (`operate_stock_rechecks`, at most one pending per source). The pending slot is reserved (`SUBMITTING`) before COLLECT is asked, so a second request never launches a second re-collection. A reservation a killed process left is released as `FAILED` at the next start.
  - **Owner:** `StockRecheckService` (`app/stages/operate/stock.py`), with a periodic round of default 6 hours, at most 20 sources per round, and a stop after 3 refusals in a row.
  - **Review work:** the `operate.stock` producer emits STOCK `LISTED_SOURCE_SOLD_OUT` on the source-identity scope, which is COLLECT's shape. A stock field under review stays COLLECT's own condition. With both STOCK producers current, STOCK is an authoritative count (ADR-0016 §7).
  - **Interface:** `GET /api/v1/operate/stock`, `POST /api/v1/operate/stock/recheck`, and the 품절확인 panel.

## 5. Order ingest

- **Endpoints:** the product-order read endpoints of SmartStore's 주문 판매자 group, adopted read-only by their own slice (§6):
  - the changed product-order statuses by time window, to find what changed;
  - the product-order detail query by ids, to read it.
- **Capability:** an `ORDER_READ` capability check (the operator-attested `주문 판매자` group, ROADMAP §4.2) gates ingest. Without it, orders are `NOT_CONNECTED` and the 주문관리 count is not a zero.
- **Resolution:**
  - Each product order resolves by its product identities to the `MarketplaceRegistration` and its `MarketplaceRegistrationItem` (the frozen `registration_item_key`), then to the canonical Item and its current source binding (ADR-0013 order-line resolution).
  - An order of a product ICBM did not register is kept as `UNMATCHED`. It is never attached by name.
  - Resolution is immutable once recorded. A later binding change never rewrites it.
- **Idempotency:** the provider's product-order id is the key. A re-read appends a status history entry and never creates a second order.
- **Cadence:** a server policy value (default 10 minutes), plus "지금 동기화". The time-window cursor is durable, and every window overlaps the last one so a late change is never missed.

## 6. Endpoint adoption

Each new read endpoint is adopted by its own slice under `ENDPOINT_MATRIX.md` §16:
- official evidence captured;
- the request contract;
- a deny-by-default retention allow-list;
- the error classification;
- local refusal before adoption.

The order allow-list follows §7, and nothing outside it is retained.

## 7. Order data lifecycle (ADR-0011 §2)

The owner decided to store what shipping needs. This is the lifecycle contract ADR-0011 §2 requires.

- **Kept in clear:**
  - the provider product-order and order ids;
  - the order status and its timestamps;
  - product and option identities, quantity, unit and total amounts;
  - the delivery method and the claim status code.
- **Kept encrypted (the shipping record):** recipient name, recipient phone, and shipping address (base, detail, zip code), plus the delivery memo.
  - The key is held by the OS secret store, like credentials. The database holds ciphertext only.
  - It is decrypted only for the order detail view and, in M6.5, the supplier order.
- **Never kept:** buyer account ids, emails, payment details, and any field outside the allow-list.
- **Masking:** lists and logs show the name and phone masked (`김*수`, `010-****-1234`). Audit details never carry them.
- **Retention:**
  - The shipping record is deleted 90 days after the order reaches a terminal state: purchase-decided, cancelled or returned. The deletion is recorded.
  - The order record (clear fields) is kept as business history.
  - The 90 days is a policy value the owner may change.
- **Evidence:** what is stored, decrypted and deleted is audited by id, never by content.
- **Fixtures:** tests never use real order data (rule 07 §7.3).

## 8. Screens

- **등록관리 / 판매 상태:** each registration's operated state, last sync, drift and external deletion, plus "지금 동기화".
- **품절확인:** each registered source product's judged availability and last recheck. The operator's decision is a link to the listing, since M6 has no marketplace write.
- **주문관리:** orders with their resolution (product, option, source product and supplier), status and masked recipient. The detail view shows the shipping record.
- **Dashboard and analytics:** counts come from the owners. A count is shown only when its owner is connected and current. The hard-coded zeros of `app/stages/operate/service.py` are removed.

## 9. Implementation order

Each step is its own PR (batched where small), audited, with CI.

1. **M6-A:** the ListingSync owner, its migration, the periodic job, "지금 동기화" and the 판매 상태 view. Provider calls use only the adopted read-back.
2. **M6-B:** the StockRecheck owner, its schedule over the COLLECT owner's public command (Track A supplies it), the stock judge, the STOCK producer and 품절.
3. **M6-C:** order endpoint evidence and adoption (read-only), the `ORDER_READ` capability, and the §7 allow-list.
4. **M6-D:** the Orders owner, its migration, the encrypted shipping record and its lifecycle job, ingest, resolution and 주문관리.
5. **M6 acceptance:** ROADMAP Phase 5 acceptance on the exact merged main, recorded in `documents/acceptance/milestones/M6.md`.

## 10. Acceptance (ROADMAP Phase 5)

For the first registered SmartStore product:
- published-status read-back works;
- the supplier stock recheck resolves to the same product;
- a read-only order retrieval maps to the same canonical product. If no real order exists, the acceptance records that, and the mapping is proven offline;
- operation sync creates no duplicate product identity.

## Invariants

```text
M6-01  M6 performs no marketplace write: listing status, price, stock, order, shipment and claim stay read-only
M6-02  a listing-state read is the adopted origin-product read only; a failed or unreadable read proves nothing
M6-03  only provider evidence (statusType DELETE or a 404 of the origin product) moves a registration to EXTERNALLY_REMOVED
M6-04  drift is shown, never repaired; a source price change never updates the marketplace by itself
M6-05  the stock recheck re-collects only source products bound to an ACTIVE registration, through the COLLECT owner, within its cadence, cap and pacing
M6-06  the stock judge is ADR-0010 §10's; availability is never proven by a supplier write
M6-07  a sold-out judgment changes no listing; the operator decides
M6-08  an order line resolves through the frozen registration item to the canonical Item; an unregistered product's order is UNMATCHED and never attached by name
M6-09  the provider product-order id is the idempotency key; a re-read never creates a second order
M6-10  shipping data is stored encrypted, decrypted only for its two uses, masked everywhere else and deleted on its retention
M6-11  no order data leaves the local data root; fixtures never carry real order data
M6-12  a count is shown only when its owner is connected and current; there is no hard-coded zero
M6-13  token renewal is CONNECT's own pass only, for a bound account with a configured margin; an AUTHENTICATION review stops it
```
