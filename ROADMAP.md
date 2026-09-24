# ICBM-NEW Roadmap

> Status: NEW CANONICAL PROJECT
> Legacy repository: `allday8556/ICBM-PROJECT` = archive/reference only
> Development rule: **no legacy code, owner, DB schema, patch chain, test harness, marketplace ID, runtime behavior, or previously wired UI functionality is automatically inherited.**

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
- Existing functional UI builds are not inherited.
- Old repository code may be inspected only when the architect explicitly approves a narrow reference case. Default is **do not reuse**.

### 1.2 Roles

```text
GitHub       = single work hub / Source of Truth
ChatGPT      = architecture, contracts, audit, review, acceptance decisions
Claude Code  = implementation, tests, commits, PR updates
User         = product decisions and protected/destructive approvals
```

Chat is not the durable work log. Plans, implementation directives, reviews, evidence, and acceptance state belong in this repository.

### 1.3 UI Source of Truth

The canonical visual/product shell for ICBM-NEW is the approved standalone prototype recorded in `docs/UI_SOURCE_OF_TRUTH.md`. That document is the single authority for which prototype revision is current and for its fingerprint; this roadmap deliberately names no prototype file.

The approved prototype is treated as a **fresh UI specification**, not as a legacy-runtime integration target.

Rules:

- The current #86 UI rebuild is **not** the ICBM-NEW implementation baseline because it already contains legacy functional wiring.
- #86 may be viewed only as a historical/reference implementation when the architect explicitly allows it.
- No #86 adapter, hidden legacy DOM, old route owner, old service call, old state object, or old business-rule wiring is carried into ICBM-NEW by default.
- The standalone HTML is recreated as the new UI shell first.
- Each visible action is then connected to a newly defined ICBM-NEW application contract.
- Business rules live in services/contracts, never as duplicated UI logic.

Canonical direction:

```text
Standalone HTML UI
        ↓
NEW application contract
        ↓
NEW service
        ↓
NEW adapter / integration
        ↓
NEW canonical DB / external read-back
        ↓
UI state
```

Not allowed:

```text
#86 / ICBM-PROJECT functional owner
        ↓
copy / transplant / adapter-wrap
        ↓
ICBM-NEW
```

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
   ├─ claims
   └─ fulfillment (supplier order → tracking → shipment update)
        ↓
Dashboard / Analytics / Insight
```

The canonical product ID is the spine of the application. Collection, registration, orders, stock, inquiries, analytics, and marketplace read-back must all resolve back to the same ICBM product.

---

# 3. Phase 0 — Foundation

Goal: create a small, explicit application skeleton before any marketplace-specific work.

## Deliverables

- standalone HTML prototype reproduced as the fresh UI shell
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

Authority: **Issue #1 and `docs/acceptance/M0.md`** — M0 **ACCEPTED** on 2026-09-13 (PR #2). The gate was demonstrated from a clean checkout, not asserted:

- install → Alembic migration → application start on an empty SQLite WAL database, with no legacy DB migration
- deterministic readiness (database, WAL, schema head, job worker, secret store, DRY_RUN, egress guard)
- a deliberately failing durable job retrying on schedule into dead-letter at the configured cap
- one `correlation_id` traced across log output, Job record and AuditEvent
- a persisted, append-only protected-action AuditEvent
- all ten top-level screens rendering their EMPTY state from new application contracts, visually checked against the approved prototype without importing legacy functional wiring
- full restart, zero supplier/marketplace external calls, no dependency on ICBM-PROJECT runtime files or #86 adapters/owners
- green CI (lint/format, type check, tests, migration check, clean-checkout acceptance)

---

# 4. Phase 1 — CONNECT

Goal: reliably connect one supplier and one marketplace before expanding anything.

## 4.1 Supplier connection

First vertical target: **KM통상** (`supplier_key = kmretail`).

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
  display_name
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
KM통상 protected page read = PASS
SmartStore account/API read-back = PASS
fresh restart/session reconnect = PASS
```

No collection or registration phase is considered complete until this connection layer is stable.

---

# 5. Phase 2 — COLLECT

Goal: turn one supplier product page into structured source facts without inventing data.

M3 (Issue #52, `docs/adr/0010-supplier-generic-collect-and-product-facts-revision.md`) proves this phase for one operator-supplied KM통상 product URL at a time:
- no discovery or crawl;
- zero AI/OCR calls;
- immutable ProductFactsRevision records, but no canonical Product (M4).

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

For the first KM통상 test product:

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

The M4 contract for this structure is `docs/adr/0013-m4-canonical-product-contract.md` (Issue #80). The canonical Product is the Canonical v3.1 `ProductGroup`: one identity, never a second product root.

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

The REGISTER contract is `docs/adr/0014-smartstore-register-idempotency-readback.md` (Issue #89).

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
KM통상
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

## 8.5 Fulfillment (inside OPERATE)

Fulfillment is an OPERATE subflow, not a new top-level system:

```text
Order
→ Product/SKU
→ SupplierOrder / supplier order reference
→ tracking
→ marketplace shipment update
→ delivery read-back
→ settlement/read model where applicable
```

The first vertical uses a manual supplier order with a canonical `SupplierOrder` record (ARCHITECT_REVIEW A1). Supplier-order automation is a later adapter capability.

## Phase 5 acceptance

For the first registered SmartStore product:

- published status read-back works
- supplier stock recheck resolves to the same product
- test order/read-only order retrieval maps to the same canonical product when available
- when an order exists, its fulfillment record (supplier order reference → tracking → marketplace shipment update → delivery read-back) resolves to the same canonical product and SKU
- no duplicate product identity is created by operation sync

---

# 9. Phase 6 — Expand horizontally

Expansion happens only after the first vertical is fully accepted.

## Supplier order

1. KM통상
2. U-PICK
3. 건강산
4. 업푸르트
5. 1688
6. Rakuten / future suppliers

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

The standalone HTML prototype maps to the architecture as follows:

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
M0 Foundation                                   ACCEPTED 2026-09-13
→ M1 KM통상 CONNECT                              ACCEPTED 2026-09-13
→ M2 SmartStore CONNECT                          ACCEPTED 2026-09-15
→ M3 one-product COLLECT → ProductFactsRevision  ACCEPTED 2026-09-18
→ M4 canonical Product DB + image pipeline + pricing/readiness foundations  ACCEPTED 2026-09-19
→ M5 SmartStore REGISTER idempotency/reconcile/read-back  CURRENT
→ M6 OPERATE read-back + stock + order ingest
→ M6.5 fulfillment record + tracking
→ FIRST VERTICAL two consecutive passes in fresh sessions
```

Only after the first vertical is accepted:

```text
Add suppliers one by one
→ Add Coupang
→ Add 11st
→ Bulk registration / automation
→ AI insight / analytics expansion
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
KM통상 CONNECT
→ real product COLLECT
→ canonical PRODUCT DB
→ SmartStore REGISTER
→ marketplace READ-BACK
→ OPERATE state sync
→ fulfillment record/tracking path when an order exists

2 consecutive complete PASS
```

Only then is the architecture considered proven.

---

# 14. Immediate next work

Accepted so far:

- M0 — fresh UI shell + Phase 0 foundation (Issue #1, `docs/acceptance/M0.md`).
- M1 — KM통상 CONNECT only (§4.1; Issue #7, ADR-0007, `docs/acceptance/M1.md`), verified against the real supplier in fresh sessions.
- M2 — SmartStore CONNECT (§4.2; Issue #46, closeout PR #51, `docs/acceptance/M2.md`), verified against the real provider in one budgeted campaign.
- M3 — one-product COLLECT → ProductFactsRevision (Issue #52, ADR-0010, `docs/acceptance/M3.md`), verified against the real supplier in one bounded campaign (`m3-accept-04`: two fresh-session passes and a closeout). The acceptance is bounded by M3.md §2: positive CONFIRMED option-axis/configuration support and quantity-tier values/source totals are not accepted.
- M4 — canonical Product DB + image pipeline + pricing/readiness foundations (Issue #80, ADR-0013, PR #81–#87, `docs/acceptance/M4.md`), accepted on the final offline acceptance run of the merged harness at exact main `57a6676` (146/146 checks, architect acceptance 5740024056). The acceptance is bounded by M4.md §2: no source SKUs, grouping/MERGE, M5 work, provider calls or quantity resale guidance.

Next, in order:

1. M5 — SmartStore REGISTER idempotency/reconcile/read-back (§12; Issue #89). Each PR was separately authorized in GitHub, and **all of them are merged**. This is implementation history, not acceptance:

   | PR | what it landed |
   | --- | --- |
   | PR-A #90 | the contract, `docs/adr/0014-smartstore-register-idempotency-readback.md` |
   | PR-B #91 | the registration foundation and migration 0016 |
   | PR-C #92 | the derived preflight and the deterministic Snapshot builder |
   | PR-D #93 | the two SmartStore product read-back adoptions, the typed REGISTER adapter and the read-back normalizer |
   | PR-E #94 | idempotent execution, reconcile, the durable job and the execution-scope send brake (migration 0017) |
   | PR-F #95 | the offline acceptance harness, the server-owned registration surface, derived canary readiness and the durable preparation owner (migration 0018) |
   | #96 | the bounded IMAGE UPLOAD adoption amendment (ADR-0014 §17.1) |

   **PR-F delivered the offline harness only.** The bounded real canary campaign it was once listed beside was not delivered and is not authorized. At the current main the canary is `BLOCKED`: product CREATE and the duplicate-lookup search stay `NOT_ADOPTED` after the provider-evidence review closed `INSUFFICIENT` (Issue #89 `5768312853`, `5768347233`), `product_registration.write` stays `UNVERIFIED`, execution stays `DRY_RUN`, and the measured outbound marketplace mutation count is 0.

   M5 is accepted only by an acceptance run on the exact merged main SHA, recorded in `docs/acceptance/M5.md` and accepted by the architect. That document stays `PENDING`.

2. **Gate 1 — the application path** (Issue #89 kickoff `5784108069`; contract `docs/adr/0015-gate1-registration-target-policy-and-category-metadata.md`). It closes the local owner and path gaps below, provider-zero, one separately authorized slice at a time: G1-A the durable target policy and its Settings write path, G1-B the operator-reviewed category metadata, then G1-C the product DB workflow and G1-E the COLLECT submit, and last G1-D the Draft command path. `ReviewItem` moves to Gate 2; `ComplianceGate` and the bounded LIVE authorization move to later pre-LIVE gates. **Gate 1 is accepted** (Issue #89 `5804516180`) on the exact-main closeout of `48129070`; that evidence is not M5 acceptance.
3. **Gate 2 — the human review path** (Issue #89 kickoff `5804605624`; contract `docs/adr/0016-gate2-human-review-path-and-review-item-owner.md`). It gives `ReviewItem` a durable owner that indexes review conditions production owners already derive, never a second truth, one separately authorized slice at a time: G2-A the owner, its migration and audited lifecycle, G2-B the COLLECT / M3 producer, G2-C the M4 and REGISTER indexing with durable counts and `NOT_WIRED` semantics. `ComplianceGate`, LIVE authorization and M6 stay out.
4. The rest of the first single-product vertical (M6 → M6.5, §12).

**No legacy patch recovery work and no #86 functional transplant are part of this roadmap.**

## 14.1 Gaps between the merged code and a runnable vertical

Merged owners are not an operable path. These gaps are recorded facts at the current main; each needs its own authorization. Rows marked **G1-A**, **G1-B**, **G1-C**, **G1-D** or **G1-E** record what that slice closed:

| gap | what exists | what is missing |
| --- | --- | --- |
| registration category metadata | **G1-B:** the durable operator-reviewed category-metadata owner (migration 0020), keyed by marketplace × taxonomy revision × category, its Settings review path and the production `RegistrationMetadataSource` over it | nothing for the owner; a category with no current revision still answers `CATEGORY_METADATA_MISSING`, an unreviewed current one `CATEGORY_METADATA_UNREVIEWED`; no provider category endpoint is adopted |
| registration target policy | **G1-A:** the durable, append-only target-policy owner (migration 0019), its Settings save path and the production `RegistrationPolicySource` over it | nothing for the owner; an account without a saved policy still blocks with `REGISTER_TARGET_POLICY_MISSING` |
| Draft creation from the product DB | **G1-D:** `POST /api/v1/register/drafts` and the 통합DB screen's 등록 초안 panel turn a server-revalidated Product selection into one `RegistrationDraft` for a bound canonical account with a current target policy (`GET /api/v1/register/draft-targets` names each account's verdict); a stale selection, an unbound or unknown account, a missing policy or a foreign pricing context creates nothing | nothing for the one-Draft path; the Draft opens in Registration Management, where the existing preparation authoring continues |
| pricing snapshot creation and pinning | **G1-D:** the same command prices every chosen Item through M4 `ProductPricingService.price` under the policy's pricing context and pins the exact `PricingSnapshot` M4 returned; an Item M4 does not price, or a Product that moves while it is priced, creates no Draft (no partial Draft), and no price is computed outside M4 | nothing for the pin; a frozen Snapshot still needs owner-held authoring revisions (ADR-0014 §27), which do not exist yet |
| product DB workflow | **G1-C:** the operator's read path over the canonical Product: the list, search and cursor pagination of ACTIVE Products, the detail with each member's source facts read through its current source revision, and the server-revalidated registration-target selection (`/api/v1/products`, the 통합DB screen) | nothing for the read path; the selection is a read-only handoff that creates no Draft, PricingSnapshot or registration row, and turning it into a Draft is the Draft-creation row above |
| COLLECT submission | **G1-E:** the COLLECT screen's one-product form submits through `POST /api/v1/collect/collections` for a supplier the server names as collection-capable, then only reads back: the durable run and job, the newest runs from the run store (so a reload or a return follows the same runs), and a RECORDED run's handoff into the Product DB | nothing for the one-product path; bulk and listing collection, and new suppliers, stay out of scope |
| Settings persistence | SmartStore credential, account and capability actions; **G1-A:** the target-policy surface and **G1-B:** the category-metadata review surface, each with its own save contract and named by the server as editable | the general common and platform fields and their save bar still reach no write contract and stay read-only; the historical `M0 · 미연결` copy of the Settings registry items is deferred to the Gate 1 UI cleanup |
| ReviewItem | the `ReviewKind` contract and the screen counters; the Gate 2 owner contract, ADR-0016 (G2-0); **G2-A:** the durable ReviewItem owner (migration 0021) with server-computed keys, the reconciliation-only lifecycle, audited transitions and owner-reconciled human resolution | no producer indexes any condition yet (G2-B, G2-C), so nothing is stored in production; `open_counts()` still returns zero for every kind although M3 already produces `REVIEW_REQUIRED` truth |
| ComplianceGate | the `PASS / REVIEW_REQUIRED / BLOCKED` contract (`docs/ARCHITECTURE.md` §7) | no production owner decides it; regulated categories may not be claimed as automatically registrable, and the first canary uses a non-regulated product |
| LIVE authorization | the two-mode execution contract, `DRY_RUN` and `LIVE` | the execution-mode owner still enforces `M0_DRY_RUN_ONLY` and denies LIVE with `M0_LIVE_FORBIDDEN`; the contract that would replace it does not exist |

## 14.2 Preconditions for the first LIVE write

Before any real marketplace write, and independently of endpoint adoption:

- a **bounded LIVE authorization contract** (§14.1) and the user's explicit scope approval;
- **provider evidence that resolves the CREATE and reconcile blockers** (Issue #89 `5768312853`): an official CREATE idempotency or ambiguous-outcome replay-safety guarantee, a deterministic account-scoped lookup with proven uniqueness and completeness, and read-after-write freshness that makes a zero-result lookup authoritative;
- a **ComplianceGate owner**, or a canary product outside every regulated category;
- a proven **backup and restore drill** of the canonical data root;
- a **complete evidence-retention policy**, end to end, over sanitized provider evidence;
- a **visual and responsive acceptance gate over populated UI state**. The current UI tests are functional wiring tests driven through Playwright; no visual-regression or responsive acceptance evidence over populated screens exists.

## 14.3 Before any horizontal supplier expansion

The implemented source and pricing schema is **KRW-only** (`docs/ARCHITECTURE.md` §6). A second-currency supplier such as 1688 or Rakuten first needs a currency and FX-snapshot schema extension decided in an ADR. No such extension is authorized, and no migration for it may be written before that decision.
