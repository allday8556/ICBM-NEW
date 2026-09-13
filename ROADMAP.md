# ICBM-NEW Roadmap

> Status: NEW CANONICAL PROJECT
> Legacy repository: `allday8556/ICBM-PROJECT` = archive/reference only
> Development rule: **no legacy code, owner, DB schema, patch chain, test harness, marketplace ID, or runtime behavior is automatically inherited.**

## 0. Product goal

ICBM-NEW has one job:

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
```

1. Connect supplier sites and sales channels.
2. Collect source-product facts accurately.
3. Store one canonical product record in the integrated product database.
4. Convert and register that product to each connected marketplace.
5. Track sales state, stock, orders, inquiries, claims, and marketplace read-back.

Everything else — AI, OCR, learning, category matching, pricing, alerts, analytics — is a supporting capability inside this flow, not a separate top-level system.

---

## 1. Project reset rules

### 1.1 Clean start

- Existing marketplace products were deleted by the user before ICBM-NEW starts.
- Legacy marketplace product IDs are not imported.
- Legacy integrated-DB rows are not imported.
- Legacy collection results are not imported.
- Legacy runtime state, patch numbers, T0/R2/CP tracks, old owner maps, and old test assumptions are not inherited.
- Old repository code may be inspected only when the architect explicitly approves a narrow reference case. Default is **do not reuse**.

### 1.2 Roles

```text
GitHub       = single work hub / Source of Truth
ChatGPT      = architecture, contracts, audit, review, acceptance decisions
Claude Code  = implementation, tests, commits, PR updates
User         = product decisions and protected/destructive approvals
```

Chat is not the durable work log. Plans, implementation directives, reviews, evidence, and acceptance state belong in this repository.

### 1.3 UI rule

The current UI rebuild is the visual/product shell. Functional implementation is built fresh underneath it.

```text
UI action → application contract → service → adapter/integration → read-back → UI state
```

The UI must not reproduce business rules independently.

---

# 2. Target architecture

```text
SUPPLIER SIDE
Credential / Session / Login
        ↓
Supplier Adapter
        ↓
Collector
        ↓
Fact Normalizer
        ↓

============================
      PRODUCT CORE
   Canonical Product DB
============================
        ↓
AI Enrichment / Pricing / Readiness
        ↓
Registration Core
   ├─ SmartStore Adapter
   ├─ Coupang Adapter
   └─ 11st Adapter
        ↓
Marketplace APIs
        ↓
Operation Sync
   ├─ sales state
   ├─ stock / sold-out
   ├─ orders
   ├─ inquiries
   └─ claims
        ↓
Dashboard / Analytics / Insight
```

The canonical product ID is the spine of the application. Collection, registration, orders, stock, inquiries, analytics, and marketplace read-back must all resolve back to the same ICBM product.

---

# 3. Phase 0 — Foundation

Goal: create a small, explicit application skeleton before any marketplace-specific work.

## Deliverables

- application/runtime skeleton
- configuration system
- database bootstrap/migrations
- secret storage boundary
- structured logging
- health/readiness endpoint
- error contract
- test layout
- UI-to-service routing contract
- adapter interfaces for suppliers and marketplaces

## Initial module boundaries

```text
app/
  connect/
  collect/
  products/
  register/
  operate/

integrations/
  suppliers/
  marketplaces/

ui/
tests/
docs/
```

## Acceptance

- clean install starts with an empty DB
- no dependency on ICBM-PROJECT runtime files
- no legacy DB migration required
- one deterministic health check passes

---

# 4. Phase 1 — CONNECT

Goal: reliably connect one supplier and one marketplace before expanding anything.

## 4.1 Supplier connection

First vertical target: **K홀세일**.

Functions:

- supplier profile
- login-required flag
- encrypted credential storage
- session/cookie lifecycle
- login-state detection
- automatic login
- connection test
- site analysis entry point
- collection-profile persistence

Contract:

```text
SupplierConnection
  supplier_key
  base_url
  auth_required
  auth_state
  session_state
  profile_state
  last_verified_at
```

A credential UI badge is never proof of a valid connection. The connection is valid only when the runtime can authenticate and read a protected supplier page.

## 4.2 Marketplace connection

First vertical target: **Naver SmartStore**.

Functions:

- API credential storage
- API permission test
- seller/account identity read-back
- product API capability check
- order API capability check
- inquiry/claim capability check where available

## Phase 1 acceptance

```text
K홀세일 protected page read = PASS
SmartStore account/API read-back = PASS
fresh restart/session reconnect = PASS
```

No collection or registration phase is considered complete until this connection layer is stable.

---

# 5. Phase 2 — COLLECT

Goal: turn one supplier product page into structured source facts without inventing data.

Pipeline:

```text
Discover product
→ Open detail page
→ Extract evidence
→ Normalize source facts
→ Validate
→ Save ProductFacts
```

## Required facts

```text
source_product_id
source_url
supplier_key
original_name
member/wholesale price
supplier shipping fee
minimum_sale_price
quantity tiers
options / atomic SKUs
representative images
detail images
detail description / HTML facts
brand
manufacturer
origin
product-information notice facts
stock / sold-out evidence
```

## Evidence rule

Raw source facts and evidence stay separate from enrichment.

```text
Source evidence → ProductFacts
                    │
                    └─ ambiguous → rule/AI review → confirmed or REVIEW_REQUIRED
```

AI must not fabricate source facts.

## Sold-out contract

- active BUY/CART → on sale
- SOLD OUT with no active purchase path → sold out
- mixed evidence → review required
- hidden/disabled/review/body badges alone are not authoritative

## Phase 2 acceptance

For the first K홀세일 test product:

- same page collected repeatedly produces stable identity
- price/shipping/options/images/stock evidence are read back from DB
- raw evidence is inspectable
- browser restart does not corrupt the collection profile

---

# 6. Phase 3 — PRODUCT DB

Goal: make the integrated product database the single product truth for all downstream work.

## Canonical structure

```text
Product
├─ Source Facts
├─ Source Options / SKUs
├─ Enrichment
├─ Pricing
├─ Registration Readiness
├─ Marketplace Registrations
└─ Operational State
```

## Core principle

No downstream screen owns a second copy of product truth.

Registration, orders, stock, inquiries, analytics, and AI all reference the canonical ICBM product ID.

## Pricing contract

Inputs:

- purchase price
- supplier shipping
- platform fee
- ad cost policy
- target net margin
- minimum_sale_price

Canonical rule:

```text
if minimum_sale_price exists:
    final sale price = minimum_sale_price
    price_basis = minimum_sale_price
else:
    final sale price = target-margin calculated price
```

Do **not** restore the old `max(target margin price, minimum sale price)` rule.

Quantity tiers remain original `(quantity, total price)` facts. Do not infer per-unit price and do not manufacture `minimum_sale_price × quantity` source facts.

## Option contract

Atomic supplier SKU identity is preserved. Same weight does not allow flattening when count/grade differs.

## Phase 3 acceptance

- one collected product has one canonical product ID
- all source facts and normalized values read back correctly
- pricing is computed in one owner only
- UI displays server-owned pricing basis/readiness rather than re-deciding it

---

# 7. Phase 4 — REGISTER

Goal: register a canonical product to SmartStore from scratch and verify marketplace read-back.

Pipeline:

```text
Canonical Product
→ Enrichment
→ Platform category mapping
→ Platform required fields
→ Pricing
→ Registration readiness
→ Payload builder
→ Marketplace CREATE
→ Marketplace read-back
→ Save marketplace identity
```

## Enrichment

AI may assist with:

- product name
- tags/search keywords
- category ranking
- option translation/normalization

AI output never replaces unsupported factual source data.

## Registration readiness

One readiness service validates:

- required source facts
- category
- required options
- product-information notice
- shipping/returns settings
- images
- selling price
- prohibited/sold-out policy
- platform-specific required fields

## Marketplace adapter rule

Core product logic stays platform-neutral.

```text
Registration Core
  → SmartStore Adapter
  → Coupang Adapter
  → 11st Adapter
```

Adding a marketplace must not require rewriting supplier collection or canonical product logic.

## Phase 4 acceptance

First complete vertical:

```text
K홀세일
→ collect 1 real product
→ integrated DB
→ SmartStore readiness PASS
→ SmartStore CREATE
→ marketplace read-back
→ returned marketplace product ID saved against the same ICBM product
```

This complete vertical must work twice consecutively before adding a second supplier or marketplace.

---

# 8. Phase 5 — OPERATE

Goal: after registration, keep the marketplace product tied to its source product and operational state.

## 8.1 Sales-state sync

Marketplace read-back tracks:

- active/inactive status
- current sale price
- marketplace identity
- registration errors
- last synchronization time

## 8.2 Stock / sold-out

```text
Supplier recheck
→ stock evidence
→ stock judge
→ on-sale / sold-out / review-required
→ user/policy decision
→ marketplace status update
```

The sold-out screen is a workflow over the relationship between source product and published product, not an independent product system.

## 8.3 Orders

```text
Marketplace Order
→ Marketplace Product ID
→ ICBM Product ID
→ Source Product / Supplier
```

Order management must therefore know the exact source product, option/SKU, supplier URL, and acquisition facts.

## 8.4 Inquiry / claims

Connected marketplace events feed:

- inquiries
- cancellations
- exchanges
- returns

They remain linked to order and canonical product identity.

## Phase 5 acceptance

For the first registered SmartStore product:

- published status read-back works
- supplier stock recheck resolves to the same product
- test order/read-only order retrieval maps to the same canonical product when available
- no duplicate product identity is created by operation sync

---

# 9. Phase 6 — Expand horizontally

Expansion happens only after the first vertical is fully accepted.

## Supplier order

1. K홀세일
2. U-PICK
3. KM통상
4. 건강산
5. 업푸르트
6. 1688
7. Rakuten / future suppliers

Each supplier implements the same supplier-adapter contract. Site-specific extraction belongs inside that adapter/profile, not in global product logic.

## Marketplace order

1. Naver SmartStore
2. Coupang
3. 11st
4. future marketplaces such as Gmarket/Auction where required

Each marketplace implements the same registration/operation adapter boundaries.

## Expansion gate

A new adapter cannot change canonical Product/Pricing/Operation contracts merely to accommodate one site. Contract changes require architecture review first.

---

# 10. Phase 7 — AI, Analytics, Shopping Insight

These are added after the transaction spine works.

## AI

Task-oriented services:

- collection evidence review
- product/MD enrichment
- category ranking
- platform-policy assistance
- CS draft assistance
- sold-out evidence assistance

AI does not become a parallel database or workflow engine.

## Analytics

Analytics is a read model over operational data:

- sales
- orders
- net margin
- registration success rate
- sold-out rate
- supplier quality
- category profitability
- traffic/conversion where available

## Shopping Insight

```text
External trend data
+ ICBM sales data
+ traffic data
+ margin data
→ sourcing candidate
→ supplier search
→ collect
→ canonical Product DB
```

Insight recommendations should be able to enter the normal collection pipeline instead of ending as isolated text recommendations.

---

# 11. UI mapping

The rebuilt UI maps to the architecture as follows:

| UI | Runtime responsibility |
|---|---|
| Dashboard | overall status/read models and navigation |
| Collection Management | supplier connection + collection execution + evidence review |
| Integrated DB | canonical Product truth |
| Registration Management | readiness + platform payload + create/read-back |
| Order Management | marketplace orders mapped to canonical products |
| Inquiry Management | marketplace CS events |
| Sold-out Confirmation | supplier stock evidence → published-product action |
| AI Shopping Insight | sourcing recommendations into normal collection flow |
| Analytics | read-only aggregation of real operation data |
| Settings | common policy + supplier/marketplace credentials/configuration |

The screen structure does not define business ownership. Services/contracts do.

---

# 12. Development sequence

Do not parallelize the core before the first vertical closes.

```text
M0 Foundation
   ↓
M1 K홀세일 CONNECT
   ↓
M2 SmartStore CONNECT
   ↓
M3 K홀세일 single-product COLLECT
   ↓
M4 Canonical PRODUCT DB + pricing
   ↓
M5 SmartStore REGISTER + read-back
   ↓
M6 OPERATE read-back / stock / orders
   ↓
FIRST VERTICAL ACCEPTED
   ↓
Add suppliers one by one
   ↓
Add Coupang
   ↓
Add 11st
   ↓
Bulk registration / automation
   ↓
AI insight / analytics expansion
```

---

# 13. Definition of Done

A feature is not done because a function exists or a test is green.

For a vertical to be accepted:

1. UI action reaches the intended service.
2. Service uses the canonical contract.
3. Integration performs the real read/write where permitted.
4. Result is read back from the external system when applicable.
5. Canonical DB reflects the read-back.
6. UI reflects canonical state after reload.
7. The same flow succeeds again in a fresh session.
8. No unrelated flow regresses.

For the first production vertical:

```text
K홀세일 CONNECT
→ real product COLLECT
→ canonical PRODUCT DB
→ SmartStore REGISTER
→ marketplace READ-BACK
→ OPERATE state sync

2 consecutive complete PASS
```

Only then is the architecture considered proven.

---

# 14. Immediate next work

1. Finalize/migrate the approved new UI shell into ICBM-NEW without importing legacy functional owners.
2. Write `docs/ARCHITECTURE.md` with the new service/adapter contracts.
3. Implement Phase 0 foundation.
4. Implement K홀세일 CONNECT only.
5. Verify it in a fresh real browser/session.
6. Continue to SmartStore CONNECT and the first single-product vertical.

**No legacy patch recovery work is part of this roadmap.**
