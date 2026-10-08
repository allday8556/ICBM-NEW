# ADR-0025 — M6.5 fulfillment contract: the supplier order record, tracking capture, the SmartStore dispatch stage and the delivery read-back

Status: **ACCEPTED** 2026-10-08. It is the M6.5 kickoff contract and lands with its own PR.

Decision owners:
- **Product directions:** the owner, in Issue #219.
  - Kickoff: "6.5가자" after the M6 acceptance (`6052458221`).
  - Directions `6053008136`:
    - the operator types the carrier and tracking number in;
    - ICBM sends the SmartStore dispatch, through LIVE approval, with the first real send approved separately when a real order exists;
    - without a real order, the acceptance records that and proves the path offline.
- **Already decided by canon:** the supplier order of the first vertical is manual and canonically recorded (ROADMAP §8.5, ARCHITECTURE §12, ARCHITECT_REVIEW A1).
- **Implementation choices:** the implementation agent, under ADR-0022 and rule 14.

M6 is accepted (`documents/acceptance/milestones/M6.md`; owner acceptance `6052458221`), so M6.5 is next in ROADMAP §12 order.

What it authorizes:
- this contract;
- the provider-zero slices of §9, each its own PR;
- the read-only amendment of §6 (the delivery read-back);
- the adoption of the dispatch endpoint by its own slice (§5.1) under ENDPOINT_MATRIX §16.

What it does not authorize:
- **No real dispatch.** A real send needs the owner's LIVE approval for that exact order (ADR-0018; `6053008136`).
- **No supplier write.** ICBM never places, pays for or cancels a supplier order.
- **No claim handling:** no cancellation, return or exchange response.
- **No transfer of order data outside the local data root.**

Sources:
- `documents/roadmap/ROADMAP.md` §8.5 and Phase 5 acceptance;
- `documents/architecture/ARCHITECTURE.md` §4 OPERATE and §12;
- `documents/decisions/architect-reviews/ARCHITECT_REVIEW_CLAUDE_ADDITIONS.md` A1 and §7;
- ADR-0011 §2 (an order-data lifecycle contract before long-lived persistence);
- ADR-0013 (canonical identity and order-line resolution);
- ADR-0018 §2–§4, §3.5 and §8 (the LIVE grant, the brake, the DELETE stage, evidence retention);
- ADR-0023 §5–§7 (order ingest and the order-data lifecycle);
- ADR-0024 §4 (orders of adopted listings);
- the dispatch and delivery research packet, Issue #219 `6053086881` (Commerce API `2.90.1`);
- rules 05, 07 and 14.

Recorded by: Claude Code (Track B). The number was confirmed free on `main` and in every open branch immediately before writing.
Date: 2026-10-08

---

## 1. Scope

M6.5 turns an ingested order into a fulfilled one (ROADMAP §8.5):

```text
Order (M6, ADR-0023)
→ canonical Item and source product (resolution, or the adoption link of ADR-0024)
→ SupplierOrder: the operator orders from the supplier by hand and records it
→ tracking: the operator types the carrier and tracking number in
→ dispatch: ICBM sends the SmartStore shipment (a protected write)
→ delivery read-back: the order read shows the carrier, tracking number and delivery state
```

Out of M6.5:
- supplier ordering automation and reading tracking from the supplier (a later adapter capability; `6053008136`);
- dispatch of more than one product order per call, or of a delivery method other than `DELIVERY`;
- a tracking change after a dispatch (the reference states no such endpoint: §5.1);
- claims, settlement and actual margin.

## 2. Owners

| owner | holds | never holds |
| --- | --- | --- |
| **Fulfillment** (`app/stages/operate/fulfillment.py`) | one `SupplierOrder` per fulfillable product order: its frozen canonical identity, the supplier order reference, the purchase amount, the carrier and tracking number, and their append-only history | the order itself (Orders owns it), the shipping record's plaintext |
| **Dispatch** (§5; M65-C) | append-only dispatch attempts, each with its grant, outcome and read-back verification | a second dispatch of an applied or unknown attempt |
| **Orders** (ADR-0023) | the order, its resolution, the shipping record and, from §6, the delivery fields of the order read | a supplier order |

The shipping record is decrypted only through the Orders owner's audited open (ADR-0023 §7), here for the supplier order: the operator copies the address into the supplier's order form.

## 3. The supplier order record

- **Fulfillable order.** A product order is fulfillable only when it names one canonical Item:
  - its ADR-0023 resolution is `MATCHED`; or
  - it is `UNMATCHED` with an ADR-0024 adoption link.

  Any other order (`ITEM_UNMATCHED`, `CONFLICT`, unresolved, or `UNMATCHED` without a link) is refused with `OPERATE_ORDER_NOT_FULFILLABLE`. It is never matched by name.
- **Status.** A supplier order is first recorded only for an order whose latest status is `PAYED` (결제완료) and has no claim. Any other status is refused with `OPERATE_ORDER_NOT_PAYED`. A recorded supplier order can still be corrected after the order moves on (it is business history), but tracking is captured only while the order is `PAYED` (§4).
- **Identity.** The record copies the Item, supplier and source product from the resolution or the link when it is first written. Those columns never change (trigger).
- **Fields:**
  - the supplier order reference (the supplier's order number, required);
  - the purchase amount in KRW (required, a whole number of won);
  - the time the operator placed it.
- **One per product order.** In the first vertical one supplier order fulfils exactly one product order. A combined supplier order for several product orders is later work.
- **Revision and history.** Every write carries the revision it read. A stale revision is refused with `409`. Every write appends a history entry (what changed, by whom, under which correlation id), and the history is append-only (triggers).
- **Audit.** Recording and amending are audited by product-order id as `SUPPLIER_ORDER_RECORDED`, never by content.

## 4. Tracking capture

- The operator enters a **carrier code** and a **tracking number**. Neither is read from the supplier (`6053008136`).
- **Carrier codes** are the documented `deliveryCompanyCode` enumeration (packet `6053086881` B-2). They are pinned in the SmartStore adapter (`integrations/marketplaces/smartstore/delivery_companies.py`) and served to the screen, and any other code is refused before anything is stored. The reference's one row whose code column holds Korean text (`GS더프레시` / `GSTHEFRESH`) is left out, because which of the two is the code is not stated.
- **The tracking number** is 1–50 characters of digits, Latin letters and hyphens. This is an ICBM policy: the reference states only a size, about 100 bytes.
- **Capture needs a supplier order.** A tracking number exists only on a recorded supplier order, and only while the order is `PAYED` without a claim.
- **Amending:** allowed until a dispatch attempt for the order opens (§5). From then on, the carrier and tracking number are frozen.
- **Audit:** tracking capture is audited as `ORDER_TRACKING_CAPTURED` by product-order id.
- A tracking number is not personal data. The carrier and tracking number are stored in clear.

## 5. The dispatch stage — the fourth mutation stage

The SmartStore dispatch is a marketplace write. It follows ADR-0018 exactly as the DELETE stage does (§3.5), as a new `MutationStage` `DISPATCH`. It is never part of a canary's ASSET → CREATE order.

- **What may be dispatched:** exactly one fulfillable `PAYED` product order with a recorded supplier order and captured tracking, under delivery method `DELIVERY`, with its dispatch time set when it is sent. Nothing else. There is no bulk dispatch, although the provider accepts up to 30.
- **The DISPATCH grant:**
  - issued only by a protected operator command, `icbm live issue-dispatch-grant`, like every grant (ADR-0018 §3.2);
  - it binds the exact product order and the supplier order revision whose carrier and tracking number it will send;
  - it has a finite window and a budget of exactly 1;
  - it is refused while any dispatch of that order is in flight, `APPLIED_PROVEN`, confirmed, in `CONFLICT`, or `UNKNOWN`; after a `REJECTED` attempt, only as §5's verification allows.
- **The layers, checked at send time** in the one unit that opens the attempt and spends the grant:
  - the execution mode is `LIVE`;
  - the brake is `RELEASED`;
  - the exact live DISPATCH grant matches;
  - the dispatch endpoint is adopted;
  - evidence retention is proven;
  - the 주문 판매자 group is attested.

  The canary-only rows of ADR-0018 §10 (eligibility, restore drill, visual acceptance, residual risk) do not apply to a shipment, as with DELETE (rule §14.5.1).
- **The durable attempt owner:** `operate_dispatch_attempts`, append-only.
  - An attempt is opened `STARTED` before any byte is sent, and ended exactly once.
  - **`APPLIED_PROVEN`:** only when the documented `200` answer lists the product order in `successProductOrderIds`.
  - **`REJECTED`:** when the documented `200` answer lists the product order in `failProductOrderInfos` (and not in `successProductOrderIds`), with its documented code kept. This is the provider's own per-order statement that this request failed. It is still verified by a read-back before a new grant (below).
  - **`NOT_APPLIED_PROVEN`:** only on the transmission-precluded whitelist.
  - **`UNKNOWN`:** everything else, for example another status, a `200` without the order in either list, a timeout or a redirect.
  - **An `UNKNOWN` dispatch is never resent, and no new grant is ever issued for its order** (GPT audit, PR #262). The reference states no idempotency or re-dispatch contract, so nothing observed after it — a missing tracking number or a `PAYED` status included — can prove that it was not applied: the provider may not yet show an applied dispatch.
- **Verification:** after any attempt that was not `NOT_APPLIED_PROVEN`, the product order is read back through the adopted detail read (§6).
  - The delivery's carrier and tracking number equal the attempt's: this confirms the dispatch (`DISPATCH_CONFIRMED`).
  - No tracking number, and the status still `PAYED`:
    - after a `REJECTED` attempt, the provider's per-order failure and this read-back together show the order undispatched. Only this opens the way to a new grant;
    - after an `UNKNOWN` attempt, it proves nothing and records nothing. The order stays `확인 필요` until a read-back shows a tracking number (the attempt's, which confirms it, or another, which is `CONFLICT`). The operator finishes such an order in the SmartStore seller center, and ICBM only reads the result.
  - Any other carrier or tracking number: `CONFLICT`, shown for the operator, never repaired.
  - A failed or unreadable read-back records nothing.
- **What it never does:** it never changes the order's resolution, the supplier order's identity or any evidence, and it never deletes local data.

### 5.1 Dispatch endpoint adoption (M65-C)

`SMARTSTORE_ORDER_DISPATCH` is adopted by its own slice under ENDPOINT_MATRIX §16, on packet `6053086881`:
- `POST /v1/pay-order/seller/product-orders/dispatch`;
- body `{"dispatchProductOrders": [{productOrderId, deliveryMethod, deliveryCompanyCode, trackingNumber, dispatchDate}]}`, with one element;
- `dispatchDate` as KST ISO 8601;
- the per-order success and fail lists;
- `400` and `500` with `code` and `message`;
- `429` `GW.RATE_LIMIT` and `GW.QUOTA_LIMIT`.

What the packet reports as **NOT STATED**:
- the API group;
- which statuses may be dispatched;
- the `dispatchDate` range;
- re-dispatch behaviour and idempotency;
- a tracking-change endpoint.

The adoption pins none of these. Its rules stand on what is stated:
- one attempt per grant;
- `UNKNOWN` is never resent and opens no new grant;
- only a read-back that shows a tracking number resolves an `UNKNOWN` attempt;
- only a documented per-order failure plus a read-back that shows the order undispatched opens a new grant.

The group is taken as 주문 판매자 (the reference files the page under 주문 > 발주/발송 처리), and the first runtime answer is recorded as `R0` evidence. Whether a place-order confirmation (발주 확인) must precede a dispatch is not stated either (codes `104442`, `104443`). If runtime evidence shows it must, its adoption is a separate amendment. M6.5 does not guess it.

## 6. Delivery read-back (amends ADR-0023 §7)

ADR-0023 §7 kept no carrier, tracking number or delivery state ("tracking is M6.5"). This contract widens the order allow-list by exactly these members of the detail read's `delivery` object (packet `6053086881` D):
- `deliveryCompany`;
- `trackingNumber`;
- `deliveryStatus`;
- `sendDate`;
- `pickupDate`;
- `deliveredDate`;
- `isWrongTrackingNumber`.

Everything else stays out, including `wrongTrackingNumberType`, which is free text.

- **Storage:** the members are stored in clear on the order as its latest delivery state. A change is appended to the order's status history.
- **Not personal data.** These members are not part of the shipping record, and ADR-0023 §7's retention and masking are unchanged.
- **Mapping revision.** The allow-list change is a new mapping revision of the order reads, so the operator's permission attestation is re-recorded (ADR-0023 §5).
- **Delivery states.** The dispatch confirmation of §5 and the screen read these members. `DELIVERY_COMPLETION` (or the order status `DELIVERED`) is the delivered state, and `isWrongTrackingNumber` true is shown as a review item for the operator.

## 7. Order data lifecycle

Unchanged from ADR-0023 §7, with three additions:
- **Supplier order records and dispatch attempts** are business history and are kept. They hold no recipient data.
- **Opening the shipping record** for the supplier order uses the existing audited open (`ORDER_SHIPPING_OPENED`). The plaintext is never stored by the Fulfillment owner and never logged.
- **After retention** deletes the shipping record, the supplier order and its tracking number stay, and the address cannot be opened again.

## 8. Screens

- **주문관리:** each order's fulfillment state (공급사 주문 전 → 공급사 주문됨 → 송장 입력됨 → 발송처리 중/완료/확인 필요 → 배송중 → 배송완료). The detail panel has:
  - the supplier order form (주문번호, 구매가);
  - "배송지 복사", which opens the shipping record through the audited route;
  - the carrier select and tracking number field;
  - from M65-C, the "발송처리" action. It shows what still blocks a send (mode, brake, grant, attestation) and never sends without every layer.
- **Dashboard (M65-C):** "발송 대기" counts orders with captured tracking and no confirmed dispatch, only while the Orders owner is `CONNECTED` (M6-12).

## 9. Implementation order

Each step is its own PR (batched where small), audited, with CI.

1. **M65-A:** this contract, the Fulfillment owner (migration, supplier order, tracking capture, history), the carrier list, the routes and the 주문관리 fulfillment panel. Provider-zero.
   - **Implementation:**
     - **Storage:** migration `0052` holds two tables.
       - `operate_supplier_orders`: one row per product order. The identity columns are immutable, and every update must be the next revision (trigger). Rows are never deleted.
       - `operate_supplier_order_history`: append-only, unique by order and revision.
     - **Service:** `FulfillmentService`.
     - **Routes:**
       - `GET /api/v1/operate/carriers`;
       - `GET /api/v1/operate/orders/{id}/fulfillment`;
       - `PUT /api/v1/operate/orders/{id}/supplier-order`, with `expected_revision` (`null` only for the first record);
       - `PUT /api/v1/operate/orders/{id}/tracking`;
       - each order in `GET /api/v1/operate/orders` carries its `fulfillment_state`.
     - **Screen:** the 주문관리 "처리" column opens the panel.
2. **M65-B:** the delivery read-back of §6: the widened allow-list, the order's delivery columns and the new mapping revision. The read endpoint is unchanged.
3. **M65-C:** the dispatch adoption (§5.1) and the DISPATCH stage (§5): the grant command, the attempt owner, the send-time admission, the verification and the "발송처리" action.
4. **M6.5 acceptance:** on the exact merged main, recorded in `documents/acceptance/milestones/M6.5.md`.

## 10. Acceptance (ROADMAP Phase 5)

When an order exists, its fulfillment record resolves to the same canonical product and SKU:
- the supplier order reference;
- the tracking;
- the marketplace shipment update;
- the delivery read-back.

If no real order exists, the acceptance records that, and the whole path is proven offline (`6053008136`). The first real dispatch is then a separate approval when an order arrives.

## Invariants

```text
M65-01  a supplier order exists only for a fulfillable PAYED order; its canonical identity is copied once and never changes
M65-02  ICBM never writes to a supplier: the supplier order is placed by hand and only recorded
M65-03  a carrier code is one of the documented codes; a tracking number exists only on a recorded supplier order
M65-04  carrier and tracking freeze when a dispatch attempt opens
M65-05  a dispatch is a protected write: LIVE, brake RELEASED, the exact DISPATCH grant (budget 1), the adopted endpoint, retention proven, the order group attested
M65-06  one product order per dispatch call; an UNKNOWN dispatch is never resent and opens no new grant; a new grant follows only a documented per-order failure confirmed undispatched by a read-back
M65-07  only the delivery members of §6 are added to the order allow-list; the shipping record and its lifecycle are unchanged
M65-08  the shipping record's plaintext is opened only by the audited route and never stored or logged by fulfillment
M65-09  every supplier order and tracking write is revisioned, appended to history and audited by id
M65-10  a fulfillment count is shown only when the Orders owner is connected; there is no hard-coded zero
```
