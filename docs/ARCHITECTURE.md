# ICBM-NEW Architecture

Status: **CANONICAL — v1 foundation**

## 1. Product spine

ICBM-NEW is one connected commerce workflow:

```text
CONNECT → COLLECT → PRODUCT DB → REGISTER → OPERATE
```

Supporting capabilities — AI, OCR, learning, pricing, compliance, jobs, audit, fulfillment, analytics — must attach to this spine. They are not separate top-level systems.

The AI runtime contract is `docs/adr/0012-ai-runtime-provider-contract.md` (Issue #8). It covers provider-neutral profiles, an optional local sidecar that ICBM never depends on, AI as a capability that never fails core readiness, and AI failure never blocking COLLECT, `ProductFactsRevision` or the canonical DB.

## 2. Runtime stack

```text
Python              3.12
Backend              FastAPI + Uvicorn
ORM / migrations     SQLAlchemy 2.x + Alembic
Database             SQLite WAL (local single-user v1)
Frontend             canonical standalone HTML/CSS/vanilla JS → ES modules
HTTP                 httpx
Browser automation   Playwright Chromium when required
Jobs                 durable DB-backed queue/scheduler; one worker owner initially
Secrets              OS-native secure credential store via keyring/Windows protection
Deployment            local Windows process; loopback-only by default
Tests                 pytest + contract/integration/E2E gates
CI                    GitHub Actions
```

No framework, DB engine, browser driver, secret store or queue may be swapped casually. A change requires an ADR.

ICBM-NEW keeps its local state under one canonical data root, `%USERPROFILE%\ICBM-NEW\data` (Issue #52 comment 5688854287):

- **Resolution.** Only `app/config.py` resolves the root, from `USERPROFILE` alone. There is no host detection and no AppData fallback, and resolution fails closed without an absolute `USERPROFILE`. `ICBM_DATA_DIR` names the data root explicitly, for tests and dedicated acceptance directories only.
- **Runtime.** `runtime/` belongs to the application and holds:
  - the one relational database, `runtime/icbm.db`;
  - the encrypted sessions;
  - the owner lock.
- **Reserved names.** `products/`, `api/`, `suppliers/`, `logs/` and `backups/` are reserved lowercase names for domain data.
- **Superseded path literals.** These runtime paths supersede the path literals of ADR-0006 (`.icbm-owner.lock`) and ADR-0007 (`sessions/`). The decisions of both ADRs are unchanged.
- **Scope.** This is the local desktop/CLI root only; a server deployment will have its own storage owner.

Exactly one ICBM process owns a data root at a time, through an OS lock on `<data root>/runtime/owner.lock` (`docs/adr/0006-single-data-directory-process-ownership.md`).

The supplier and marketplace logins live in the OS secret-store service `ICBM-NEW` (the same name as the data root), and CONNECT alone owns them together with the connection state and the sessions. COLLECT, and every harness that needs a session, borrows that owner through the canonical resolver and never defines a second one (Issue #52 comments 5687814715 and 5688150031).

## 3. Layering rule

Every functional path follows:

```text
UI action
→ application contract
→ domain/service owner
→ integration adapter
→ external read/write
→ read-back/reconcile
→ canonical state
→ UI refresh
```

Forbidden shortcuts:

- UI → marketplace/supplier directly
- UI-owned pricing/compliance/business truth
- supplier-specific rules inside global product services
- marketplace-specific payload fields leaking into ProductFacts
- AI writing ungrounded source facts

## 4. Core boundaries

### CONNECT
Owns supplier and marketplace connectivity only. Supplier CONNECT is supplier-generic: suppliers are site-knowledge definitions behind a policy-enforcing common transport, and READY is proven only by a protected read with its unauthenticated control (`docs/adr/0007-supplier-generic-connect.md`).

- supplier credentials/session/auth state
- marketplace accounts/API credentials/capabilities
- connection verification/read-back
- crawl/session policies

### COLLECT
Owns source evidence and source facts. Collection is supplier-generic (`docs/adr/0010-supplier-generic-collect-and-product-facts-revision.md`):
- every source read goes through a common collection gateway, separate from the CONNECT proof port;
- suppliers contribute pure collection definitions and parsers;
- COLLECT persists ProductFactsRevision source truth from M3, while the canonical Product arrives in M4.

The Adaptive Collector contract is `docs/adr/0017-adaptive-collector-profile-extraction-and-shadow-validation.md` (Issue #110). It authorizes no implementation by itself. Three production slices exist: the offline core (P1, `app/collect/adaptive/`), the profile and validation persistence owner (P2, `app/collect/adaptive_store/`, migration 0024) and the shadow foundation (P3, `app/collect/adaptive_shadow/`, migration 0025). P2 stores immutable EPR/PTR revisions with their DRAFT lint findings, the append-only lifecycle log, supplier-bound local-only validation samples and validation runs; `VALIDATED` is derived, never stored. P3 adds the append-only per-supplier shadow switch (the only way into `SHADOW`), freezes each run's shadow decision and exact bundle on the canonical run record at its first product-read reservation, runs the one-fetch shadow comparison after the canonical commit in its own write unit, and keeps the raw shadow records (90 days and 5,000 per supplier, whichever first), the evidence ledger and the evidence windows. Phase C stage C0 (`app/collect/adaptive_capture/`, migration 0027, and the stopped-app harness `scripts/phase_c.py`) adds an in-memory ValidationSample capture seam, off by default and frozen per run at its first reservation, and the only operator path for the Phase C actions; the evidence scaffold is `docs/acceptance/ADAPTIVE-PHASE-C.md` (PENDING). C1 PREP-0 (migration 0028) adds the durable accounting of every actual send of a Phase-C-accounted collection, reserved before transmission, with CONNECT authentication a hard zero; ordinary collections are unchanged. **No supplier's switch is on, so no run is shadowed or captured and no window is open**: each later stage needs its own authorization. There is no Adaptive `ProductFactsRevision` write and no `ACTIVE`, each of which needs its own authorized slice:
- it is a second implementation of the same parser seam: a generic engine interprets an immutable, digested `ExtractionProfileRevision` bundle, and the gateway, budget, image fetch, revision store and job owners stay as above;
- the access envelope (`CollectionProfile`) is never profile data and is never widened at run time;
- a shadow comparison reads the same one fetch, writes nothing canonical and makes zero AI/OCR calls; the canonical extractor stays the only revision writer until a separate cutover ADR.

- discover/open/extract
- raw evidence
- ProductFactsRevision creation
- stock evidence
- source image discovery/fetch/checksum
- ambiguity → REVIEW_REQUIRED

### PRODUCT DB
Owns canonical product identity and normalized accepted state.

- Product identity
- current source ProductFactsRevision per member source product
- atomic source SKU/option mapping
- enrichment proposals/accepted values
- pricing snapshots
- registration readiness inputs

### REGISTER
Owns platform conversion and listing creation.

- category mapping
- compliance gate
- marketplace publication-asset requirements, upload and read-back (binary transformation itself remains the M4 derived-image owner)
- readiness
- RegistrationAttempt/idempotency
- CREATE/reconcile/read-back
- MarketplaceRegistration

The M5 REGISTER contract is `docs/adr/0014-smartstore-register-idempotency-readback.md` (Issue #89):
- M4 keeps every product-side owner. Marketplace-sized binaries stay M4 derived artifacts; M5 owns the upload and the provider asset identity.
- Each provider-listing unit has one immutable `RegistrationSnapshot` and one CREATE `RegistrationIntent`. Read-back is compared to that Snapshot, never to current state.
- An unresolved `UNKNOWN` CREATE is reconciled before any resend — by evidence ADR-0014 §10 admits, never by a seller-side code alone or a zero-result lookup (§7) — and blocks every new CREATE Intent in its marketplace × account × group conflict scope.
- **The adopted provider surface is still narrow.** At this main only the two SmartStore product read-backs and the bounded image upload are `ADOPTED`; product CREATE and the duplicate-lookup search are `NOT_ADOPTED`, `product_registration.write` is `UNVERIFIED`, and execution is `DRY_RUN`, so no listing has been created. The state and what still blocks a bounded canary are recorded in `docs/acceptance/M5.md` §9 and `docs/platforms/smartstore/ENDPOINT_MATRIX.md` §4.

### OPERATE
Owns everything after publication.

- listing state/read-back
- stock/sold-out sync
- order/claim/inquiry sync
- source drift handling
- fulfillment record: supplier order → tracking → marketplace shipment update
- settlement and actual-margin inputs

## 5. Canonical truth model

### ProductFactsRevision
Append-oriented source truth. Every successful recollection creates a new immutable revision, even when the source is unchanged; facts are never mutated in place. ADR-0010 defines:
- provenance: revision ID, extractor revision and fingerprint, collection run/correlation, `facts_status`; a profile-interpreted revision also carries its profile provenance (ADR-0017 §5), and no existing revision is backfilled;
- the product-scoped evidence model;
- fingerprint rules.

Minimum identity:

```text
supplier_key
source_product_id
source_url
captured_at
currency
source prices
supplier shipping
minimum_sale_price
quantity tiers
atomic SKU/options
images
brand/manufacturer/origin
notice facts
stock evidence
source fingerprint + field fingerprints
```

### Product
ICBM canonical identity. References each member source product's current source facts revision, and downstream state.

The M4 product contract is `docs/adr/0013-m4-canonical-product-contract.md` (Issue #80):
- The canonical `Product` **is** the Canonical v3.1 `ProductGroup`: one entity and one identifier, with no second product root. `icbm_product_id` is the name this document uses for that identifier (`docs/GLOSSARY.md`).
- Each member source product keeps its own current source revision pointer. That pointer is never an "accepted" or confirmed revision: it may point to a `REVIEW_REQUIRED` revision, and readiness carries that ambiguity.
- A `PricingSnapshot` is per Item **and** per explicit pricing context (marketplace, account where it matters, fee and policy versions). Readiness is layered: base readiness, per-context pricing readiness, then M5 registration preflight.
- Sellable Items are `group identifier + composition_signature`.

### MarketplaceRegistration
Always includes:

```text
marketplace_key
marketplace_account_id
marketplace_product_id
seller_product_code
published_state
last_readback_at
```

`marketplace_account_id` is the canonical spelling of the account identity in every registration row and every new account-scoped owner. `account_id` is not an account identity: in M4 pricing it is the pre-existing pricing-context discriminator (the account, or `None` when account-invariant), and in the SmartStore token contract it is a provider request field (`docs/GLOSSARY.md` §1). `seller_product_code` holds the listing identity as sent (ADR-0014 §7).

The canonical product is reached **through the registered Items**: each `MarketplaceRegistrationItem` carries its `registration_item_key`, the frozen Item snapshot it registered and the group that Item belonged to. The registration row holds no second `icbm_product_id` copy of that relation.

No downstream module creates a second product truth.

## 6. Pricing

Only Pricing owns selling-price calculation.

Canonical rule:

```text
if minimum_sale_price exists:
    final_sale_price = minimum_sale_price
    price_basis = MINIMUM_SALE_PRICE
else:
    final_sale_price = target_margin_price
    price_basis = TARGET_MARGIN
```

Never restore `max(target_margin_price, minimum_sale_price)`.

Preserve quantity-tier original totals; never manufacture source values by dividing or multiplying minimum prices.

Pricing snapshots must also support:

- source currency + FX snapshot when needed
- platform commission
- settlement/payment fees
- VAT policy
- advertising policy
- shipping policy
- coupon/discount burden
- return/exchange costs
- estimated vs actual settled margin

**Today the implemented schema is KRW-only.** `product_facts_revisions.currency` is constrained to `KRW`, and the pricing owner computes in whole KRW with one rounding rule. That is sufficient for the first vertical (KM통상 → SmartStore, §15). A second-currency supplier — 1688, Rakuten or any other — first needs a currency and FX-snapshot extension of the source and pricing schema, decided in an ADR. **No such extension is authorized now**, and horizontal supplier expansion cannot start before it exists (`ROADMAP.md` §14).

## 7. Registration safety

### ComplianceGate

```text
PASS | REVIEW_REQUIRED | BLOCKED
```

BLOCKED cannot be bypassed by automation. Resolution requires changed/verified evidence and an audit trail.

**No production ComplianceGate owner exists yet.** The states above are the contract; no service decides them at this main, and the REGISTER preflight carries no compliance verdict of its own. Until that owner is implemented and accepted, regulated goods — 건강기능식품, KC certification, 식약처 notices, prohibited wording and every other regulated category — must not be claimed as automatically registrable, and **the first bounded canary uses a non-regulated product**: one whose reviewed category metadata proves it outside every regulated category — an eligibility restriction recorded for the canary, never a `COMPLIANCE PASS` (ADR-0018 §5). Gate 3 implements no ComplianceGate.

### RegistrationAttempt

CREATE is stateful and idempotent:

```text
PREPARED → SENT → CONFIRMED
                 ↘ UNKNOWN
                 ↘ FAILED
```

`UNKNOWN` is reconciled before any retry. Never blindly resend CREATE.

What may settle an `UNKNOWN` is fixed by ADR-0014 §10, which stays authoritative: `NOT_APPLIED_PROVEN` or remote absence needs evidence that satisfies the adopted, operation-specific proof contract — a provider read-back under the adopted contract, a provider lookup by the listing identity under an adopted lookup contract, transmission-precluded evidence (no transport handoff), or another explicitly reviewed machine or provider proof. An operator assertion is never the evidence.

The deterministic seller-side product code is **ICBM's own correlation identity** for a provider-listing unit (ADR-0014 §7). It is not a provider uniqueness guarantee, and it does not by itself make a lookup deterministic. **Remote absence via a provider lookup is what the current evidence does not support**: no lookup contract with proven key, uniqueness and completeness semantics is adopted, and **a lookup that returns nothing is not proof of absence** (ADR-0014 §17.2). So a CREATE that may have been transmitted, and whose ambiguity no admissible evidence resolves, stays `UNKNOWN` under its `REVIEW_REQUIRED` workflow overlay, keeps its conflict scope closed, and is never blindly replayed; the current evidence verdict is recorded in `docs/acceptance/M5.md` §9.

## 8. Jobs and sync

One durable Job contract is shared by collect/register/stock/order/inquiry/tracking operations.

Core fields:

```text
job_id
job_type
target_ref
state
attempt_count
next_attempt_at
correlation_id
last_error_class
last_error_code
created_at
started_at
finished_at
```

Core error classes — the aligned taxonomy of `docs/adr/0008-error-taxonomy-alignment.md`, which also records how it relates to `docs/architecture/CANONICAL-V3.1.md` §11.3:

```text
TRANSIENT
RATE_LIMITED       v3.1 RATE_LIMIT (same class, persisted spelling)
AUTH
VALIDATION
POLICY_BLOCKED     v3.1 POLICY (same class, persisted spelling)
NOT_FOUND          an ICBM-local addressed resource only; never a provider/wire classification
CONFLICT           added to the runtime enum by the ADR-0008 implementation stage
DUPLICATE          added to the runtime enum by the ADR-0008 implementation stage
REVIEW_REQUIRED    added to the runtime enum by the ADR-0008 implementation stage
FATAL              added to the runtime enum by the ADR-0008 implementation stage
UNKNOWN
```

A class is a cause. It is never a workflow state, retry permission or replay permission: `error_class=REVIEW_REQUIRED` is not `workflow_state=REVIEW_REQUIRED`. Only `TRANSIENT` and `RATE_LIMITED` are retried automatically (ADR-0004, ADR-0005). The taxonomy only grows: no member is removed or renamed while persisted rows can hold it.

UNKNOWN destructive/write outcomes are not automatically retried.

Job states, transitions and the per-attempt history are fixed by `docs/adr/0005-durable-job-state-and-attempt-history.md`.

## 9. Review queue

One ReviewItem model handles all human-required work:

```text
COLLECT_EVIDENCE
STOCK
SOURCE_CHANGE
COMPLIANCE
REGISTRATION_ERROR
FULFILLMENT
```

UI surfaces these items in the relevant existing screen plus dashboard counts. No separate top-level review application is required for v1.

**The ReviewItem owner exists** (G2-A: `app/review/owner.py`, migration 0021) **with the COLLECT / M3 producer** (G2-B: `app/review/collect_producer.py`, coverage watermark migration 0022). COLLECT's `REVIEW_REQUIRED` conditions on each source identity's current revision are indexed, reconciled at startup and periodically, and shown on the COLLECT screen's run focus with the coverage verdict. **G2-C** adds the M4 base-readiness producer (`app/review/products_producer.py`), the REGISTER execution producer (`app/review/register_producer.py`: an `UNKNOWN` Intent, a read-back `MISMATCH`, a PAUSED execution scope) and the REGISTER preparation producer (`app/review/preflight_producer.py`: each current durable preparation's candidate preflight, derived by the existing preflight owner and anchored on the preparation revision), each reconciled at startup and periodically like COLLECT. Each owner screen lists its own scope's items with its producers' coverage (`GET /api/v1/review/items`, `app/review/scopes.py`): 수집관리 a source product, 통합DB a Product or Item, 등록관리 an account, Draft, Intent or preparation. Only those exact canonical scope shapes are read; an incomplete or mixed shape is refused (`REVIEW_SCOPE_UNSUPPORTED`), since a partial scope would reach across scopes (ADR-0016 §8). Every watermark stores the owner truth token its pass was fenced on (migration 0023), and coverage compares it with the owner's token on every read, so an owner that moved since is not current at once. `ReviewService.open_counts()` counts durable OPEN rows per kind; a kind is `CURRENT` (the count is authoritative) only when **every** producer that can emit it is wired and current, `NOT_CURRENT` when one is wired but not current, and `NOT_WIRED` when one does not exist or never completed a full pass (`app/review/counts.py`). Neither of the last two carries a count. STOCK is therefore never authoritative on COLLECT's coverage alone, and the dashboard and 품절 are never EMPTY on a count that is not authoritative.

The owner's contract is `docs/adr/0016-gate2-human-review-path-and-review-item-owner.md` (Gate 2). A ReviewItem is a durable index of human work over a condition a production owner already derives, never a source of that owner's truth:
- it is deduplicated by server-computed condition and review keys;
- a changed owner revision supersedes the old item, and reconciliation alone decides whether an item is open;
- a human resolution changes no owner fact and leaves the item open while the owner still derives the condition;
- a kind without a fully reconciled producer is reported as `NOT_WIRED`, never as zero;
- missed indexing is recovered by a full reconciliation at process startup and periodically while running, never only by the next event for that scope, and a count is authoritative only while that coverage is current.

## 10. Images

Published images must not depend on supplier hotlinks.

```text
source URL
→ fetch
→ checksum
→ validate
→ dedupe
→ transform if platform requires
→ upload
→ save marketplace image identity/read-back
```

Detail-page embedded images used in published content are rehosted as part of the same pipeline.

COLLECT covers only the first three steps for *source* assets (ADR-0010 §9):
- It keeps the original bytes unmodified, with role/order, dimensions, MIME type, size and SHA-256.
- It never transforms them.

Derived marketplace variants (the transformed binary, its lineage and its QA) belong to the M4 derived-image owner. The marketplace adapter supplies only the target profile requirement; REGISTER uploads that exact artifact and owns the provider asset identity and read-back (ADR-0014 §5).

## 11. Source drift

Recollection compares facts fingerprints, only between revisions with equal `comparability_key` (ADR-0013 §3 as amended by ADR-0017 §5.3; for every revision without profile provenance that is the same `extractor_revision`).

```text
price changed    → new pricing proposal
option removed   → REVIEW_REQUIRED
option added     → proposed addition
image changed    → image pipeline
notice/detail    → REVIEW_REQUIRED when materially changed
stock changed    → stock workflow
```

Never silently remove a live marketplace option because a supplier page changed.

## 12. Fulfillment inside OPERATE

First vertical: **manual supplier order, canonically recorded**.

```text
Marketplace order
→ Product + atomic SKU mapping
→ SupplierOrder record
→ manual supplier purchase/reference
→ tracking captured
→ marketplace shipment/tracking write
→ delivery read-back
→ settlement
```

Supplier ordering automation is a later adapter capability; the canonical record exists from v1.

## 13. Execution safety

Global external-write mode:

```text
DRY_RUN | LIVE
```

Default is DRY_RUN. Official marketplace sandbox/test accounts are adapter/environment configuration, not a third global mode.

Protected/destructive actions are audit logged.

**LIVE is refused outright at this main.** The execution-mode owner still enforces the M0 policy (`M0_DRY_RUN_ONLY`): a request for LIVE is denied with `M0_LIVE_FORBIDDEN` and audited. That is the safe state and it is deliberate. Before any bounded real canary, the authorization contract that replaces it — who may enter LIVE, for which marketplace, account, endpoint group and time-bounded scope, on what recorded approval, and how it returns to DRY_RUN — must be decided and accepted. **That contract is ADR-0018 (Gate 3, G3-0). Its owners exist provider-zero since Gate 3 area 1 — the grant, the brake, the ASSET upload-attempt owner and the send-time stack, integrated deny-by-default into the CREATE and ASSET paths — and the stack's execution-mode layer refuses every mutation while `M0_DRY_RUN_ONLY` holds; Gate 3 area 2 adds the restore-drill and evidence-retention proofs as durable owners, and area 3 the reviewed populated visual acceptance record, bound to the exact accepted commit and running code (proofs, never permission)**: a bounded, audited, server-owned LIVE grant is the only authority for a marketplace mutation, a durable fail-closed protected-write brake stops every new mutation, and a mutation starts only when every layer of that safety stack allows it — beside, never instead of, the ADR-0014 §26 execution-scope brake. A canary has two mutation stages, the image upload (ASSET, before any Snapshot) and CREATE (after the freeze), each with its own exact grant, restore proof and send-time readiness. Until an implementation slice replaces `M0_DRY_RUN_ONLY` under it, LIVE stays refused. Even then, a canary stays `BLOCKED` while CREATE and SEARCH are `NOT_ADOPTED` (ADR-0018 §6).

## 14. Acceptance

A vertical is accepted only when the same real flow succeeds twice consecutively in fresh sessions:

```text
CONNECT
→ COLLECT
→ ProductFactsRevision
→ canonical Product
→ readiness/compliance
→ REGISTER
→ marketplace read-back
→ OPERATE sync
→ fulfillment record/tracking path when an order exists
```

Acceptance evidence belongs under `docs/acceptance/`, not in chat.

## 15. First vertical

```text
Supplier      KM통상 (supplier_key kmretail)
Marketplace   Naver SmartStore
Currency      KRW
Accounts      schema supports many; first run may use one
Fulfillment   manual-with-record
Writes        DRY_RUN until explicit LIVE verification
```

Expand only after the first vertical closes end-to-end.
